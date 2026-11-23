"""Smoke manual PFI-138: SPA, API, PostgreSQL y Hardhat aislados.

Requiere TEST_DATABASE_URL, PFI138_EVM_RPC, PFI138_EVM_CONTRACT,
PFI138_EVM_ARTIFACT y PFI138_EVM_KEY. Nunca usar credenciales de testnet.
Crea y elimina un esquema propio; restaura automine aunque falle una aserción.
"""

import asyncio
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from decimal import Decimal
from pathlib import Path

import httpx
import websockets
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from app.adaptadores.chain import Web3ChainClient
from app.adaptadores.database import (
    create_database_engine, create_session_factory, session_scope, upgrade_schema,
)
from app.adaptadores.local_storage import LocalEvidencePublisher
from app.adaptadores.models import Claim, OracleOperation, OracleWallet
from app.adaptadores.oracle_coordinator import OracleCoordinator
from app.dominio.claims import UploadedFile, current_configuration, register_claim

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND = BACKEND.parent / "frontend"

# La barra puede mostrar el token tanto en texto como en el value de un input.
CAPTURE_REDACTION_JS = """(() => {
    document.querySelectorAll('aside').forEach(el => { el.style.visibility = 'hidden'; });
    document.querySelectorAll('input, textarea').forEach(el => {
        el.value = '[oculta]';
        el.setAttribute('value', '[oculta]');
        el.style.visibility = 'hidden';
    });
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
        if (node.textContent.includes('Sesión:')) node.textContent = 'Sesión: [oculta]';
    }
})()"""


def free_port():
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        return bound.getsockname()[1]


def wait_http(process, url):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"Servidor cerrado antes de responder: {url}")
        try:
            if httpx.get(url, timeout=0.5).status_code == 200:
                return
        except httpx.RequestError:
            pass
        time.sleep(0.1)
    raise AssertionError(f"Servidor no respondió: {url}")


def stop(process):
    """Termina el grupo aislado para no dejar el hijo Vite de npm con vida."""
    if process is None:
        return
    if process.pid == os.getpgrp():
        raise RuntimeError("No se puede detener el grupo del proceso actual")
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def screenshot_path(scratch, label):
    """Una captura solo vive dentro del directorio temporal de la corrida."""
    if label not in ("blocked", "confirmed"):
        raise ValueError("Etiqueta de captura no admitida")
    return Path(scratch) / f"monitor-{label}.png"


def restore_automine(chain):
    """No oculta un fallo de RPC al restituir el estado del nodo local."""
    provider = chain.w3.provider
    if provider.make_request("evm_setAutomine", [True]).get("result") is not True:
        raise RuntimeError("No se pudo restaurar automine")
    if provider.make_request("hardhat_getAutomine", []).get("result") is not True:
        raise RuntimeError("No se pudo comprobar automine")
    if "error" in provider.make_request("evm_mine", []):
        raise RuntimeError("No se pudo minar tras restaurar automine")


async def drive_browser(ws_url, vite_url, number, chain, scratch):
    async with websockets.connect(ws_url, origin="http://localhost", max_size=8 * 1024 * 1024) as ws:
        serial = 0

        async def call(method, params=None):
            nonlocal serial
            serial += 1
            ident = serial
            await ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                if message.get("id") == ident:
                    if "error" in message:
                        raise RuntimeError(message["error"])
                    return message.get("result", {})

        async def evaluate(expression):
            result = await call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
            if "exceptionDetails" in result:
                raise RuntimeError(result["exceptionDetails"])
            return result.get("result", {}).get("value")

        async def screenshot(label):
            # Capturar solo el contenido principal, nunca la barra ni sus inputs.
            await evaluate(CAPTURE_REDACTION_JS)
            assert await evaluate("Array.from(document.querySelectorAll('aside')).every(el => getComputedStyle(el).visibility === 'hidden')")
            assert await evaluate("Array.from(document.querySelectorAll('input, textarea')).every(el => el.value === '[oculta]' && getComputedStyle(el).visibility === 'hidden')")
            assert "operador-op-02" not in (await evaluate("document.body.innerText") or "")
            bounds = json.loads(await evaluate("JSON.stringify((() => { const el = document.querySelector('main'); const rect = el.getBoundingClientRect(); return { x: rect.left, y: rect.top, width: rect.width, height: el.scrollHeight }; })())"))
            capture = await call("Page.captureScreenshot", {
                "format": "png", "captureBeyondViewport": True,
                "clip": {"x": bounds["x"], "y": bounds["y"],
                         "width": min(bounds["width"], 1200),
                         "height": min(bounds["height"], 2600), "scale": 1},
            })
            target = screenshot_path(scratch, label)
            target.write_bytes(base64.b64decode(capture["data"]))
            print("Captura temporal sin token:", target.name, flush=True)

        await call("Page.enable")
        await call("Runtime.enable")
        await call("Page.navigate", {"url": vite_url + "/"})
        await asyncio.sleep(0.6)
        await evaluate("localStorage.setItem('pfi.perfil', JSON.stringify('operador'))")
        await call("Page.navigate", {"url": vite_url + "/monitor"})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            body = await evaluate("document.body.innerText") or ""
            if "Billetera del oráculo bloqueada" in body and number in body and "Reconciliar" in body:
                break
            await asyncio.sleep(0.2)
        else:
            raise AssertionError(f"Cola integrada no renderizada: {body[:500]}")
        await screenshot("blocked")

        assert chain.w3.provider.make_request("evm_setAutomine", [True])["result"] is True
        chain.w3.provider.make_request("evm_mine", [])
        await evaluate("Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Reconciliar'))?.click()")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            body = await evaluate("document.body.innerText") or ""
            if "Reconciliación exitosa" in body and "Billetera del oráculo bloqueada" not in body:
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError(f"Reconciliación integrada no visible: {body[:650]}")
        await screenshot("confirmed")
        print("Chromium integrado: bloqueo → minado → reconciliación, éxito visible", flush=True)


def main():
    url = make_url(os.environ["TEST_DATABASE_URL"])
    rpc = os.environ["PFI138_EVM_RPC"]
    if (url.host, url.port, url.database) != ("127.0.0.1", 55439, "pfi138") or rpc != "http://127.0.0.1:8546":
        raise RuntimeError("Solo se admite PostgreSQL local aislado y Hardhat local PFI-138")
    admin = create_engine(url)
    schema = "pfi138_ui_" + uuid.uuid4().hex[:12]
    created = False
    engine = api = vite = chrome = chain = None
    with tempfile.TemporaryDirectory(prefix=".pfi138-integrated-", dir=FRONTEND) as scratch:
        path = Path(scratch)
        api_port, vite_port = free_port(), free_port()
        api_url, vite_url = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{vite_port}"
        try:
            with admin.begin() as conn:
                conn.execute(text(f"CREATE SCHEMA {schema}"))
            created = True
            db_url = url.set(query={"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
            engine = create_database_engine(db_url)
            upgrade_schema(engine)
            factory = create_session_factory(engine)
            publisher = LocalEvidencePublisher(root=str(path / "evidence"))
            with session_scope(factory) as session:
                claim = register_claim(
                    session, publisher, damage_description="El lateral quedó abollado tras el choque.",
                    files=[UploadedFile("lateral.png", "image/png",
                                        b"\x89PNG\r\n\x1a\nimagen aislada para PFI-138")],
                    declared_amount=Decimal("175000"),
                )
                # Hardhat vive más que el esquema temporal: evitar repetir SN-4101.
                claim.claim.tracking_number = f"SN-{uuid.uuid4().hex[:12]}"
                claim_id, number = claim.claim.id, claim.claim.tracking_number
                config_id = current_configuration(session).id
            artifact = json.loads(Path(os.environ["PFI138_EVM_ARTIFACT"]).read_text())
            artifact["registro"] = os.environ["PFI138_EVM_CONTRACT"]
            deployment = path / "deploy.json"
            deployment.write_text(json.dumps(artifact))
            chain = Web3ChainClient(
                rpc_url=rpc, contract_address=artifact["registro"],
                oracle_private_key=os.environ["PFI138_EVM_KEY"], abi=artifact["abi"], timeout=2,
            )
            assert chain.w3.eth.chain_id == 31337
            assert chain.w3.provider.make_request("evm_setAutomine", [False])["result"] is True
            try:
                OracleCoordinator(factory, chain).register_claim(
                    claim_id, number, "QmIntegratedBrowser", config_id,
                )
            except Exception as exc:
                print("Emisión pendiente:", type(exc).__name__, flush=True)
            with session_scope(factory) as session:
                operation = session.scalars(select(OracleOperation)).one()
                assert operation.transaction_hash and operation.status in ("enviada", "incierta")
                assert session.scalars(select(OracleWallet)).one().active_operation_id == operation.id
                transaction_hash = operation.transaction_hash
                print("Operación congelada:", operation.status, "nonce", operation.nonce,
                      "hash", transaction_hash[:12], flush=True)

            api_env = dict(os.environ, DATABASE_URL=db_url, CLIENTE_CADENA="web3",
                           CADENA_RPC=rpc, CADENA_CLAVE_ORACULO=os.environ["PFI138_EVM_KEY"],
                           CADENA_DESPLIEGUE=str(deployment), PUBLICADOR_EVIDENCIA="local",
                           RUTA_EVIDENCIA_LOCAL=str(path / "evidence"), ORIGENES_PERMITIDOS=vite_url)
            vite_env = dict(os.environ, VITE_API_URL=api_url)
            with (path / "api.log").open("w") as api_log, (path / "vite.log").open("w") as vite_log:
                api = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(api_port)],
                    cwd=BACKEND, env=api_env, stdout=api_log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                vite = subprocess.Popen(
                    ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(vite_port), "--strictPort"],
                    cwd=FRONTEND, env=vite_env, stdout=vite_log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                wait_http(api, api_url + "/health")
                wait_http(vite, vite_url + "/")
                queue = httpx.get(api_url + "/cola", headers={"Authorization": "Bearer operador-op-02"}).json()
                assert queue["oracleBlocked"] and queue["oracleOperations"][0]["transactionHash"] == transaction_hash
                chrome = subprocess.Popen(
                    ["chromium", "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                     "--disable-gpu", "--no-first-run", "--remote-allow-origins=*",
                     "--remote-debugging-port=0", f"--user-data-dir={path / 'browser'}", "about:blank"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                devtools = path / "browser" / "DevToolsActivePort"
                deadline = time.monotonic() + 8
                while not devtools.exists() and time.monotonic() < deadline:
                    if chrome.poll() is not None:
                        raise AssertionError("Chromium terminó antes de DevTools")
                    time.sleep(0.1)
                assert devtools.exists()
                port = devtools.read_text().splitlines()[0]
                pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
                ws_url = next(page["webSocketDebuggerUrl"] for page in pages if page["type"] == "page")
                asyncio.run(drive_browser(ws_url, vite_url, number, chain, path))
                result = httpx.get(api_url + "/cola", headers={"Authorization": "Bearer operador-op-02"}).json()
                assert not result["oracleBlocked"] and not result["oracleOperations"]
                with session_scope(factory) as session:
                    assert session.get(Claim, claim_id).onchain_claim_id is not None
                    assert session.scalars(select(OracleWallet)).one().active_operation_id is None
                print("API/PG confirmada, wallet liberada; solo Hardhat local", flush=True)
        finally:
            cleanup_errors = []
            if chain is not None:
                try:
                    restore_automine(chain)
                except Exception as exc:
                    cleanup_errors.append(exc)
            for process in (chrome, vite, api):
                try:
                    stop(process)
                except Exception as exc:
                    cleanup_errors.append(exc)
            if engine is not None:
                engine.dispose()
            if created:
                try:
                    with admin.begin() as conn:
                        conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
                except Exception as exc:
                    cleanup_errors.append(exc)
            admin.dispose()
            if cleanup_errors:
                raise RuntimeError("Limpieza incompleta del smoke aislado") from cleanup_errors[0]


if __name__ == "__main__":
    main()

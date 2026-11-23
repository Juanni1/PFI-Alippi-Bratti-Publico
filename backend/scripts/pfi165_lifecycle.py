"""Corrida acotada PFI-165: preparación pura y ejecución explícita aislada."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

REQUIRED_ARTIFACTS = ("backbone_mnv3_ft.onnx", "head_mnv3_ft.onnx", "model_r576.compiled",
                      "kzg_15.srs", "settings_r576.json")
DERIVED_ARTIFACTS = ("pk_r576.key", "vk_r576.key")
ARTIFACTS = REQUIRED_ARTIFACTS + DERIVED_ARTIFACTS
RECEIPTS = ("verificador", "registro", "umbral", "confirmacionHumana")
ROOT = Path(__file__).resolve().parents[1]
METADATA = ROOT / "tests/fixtures/pfi165-synthetic.json"
APPROVED_DEPLOYMENT = ROOT / "config/pfi165-approved-deployment.json"
# Ejecutado como archivo en scripts/, los imports de app necesitan la raíz explícita.
sys.path.insert(0, str(ROOT))


class InvalidPlan(Exception):
    """Error de preparación acotado; nunca incluye valores proporcionados por el usuario."""


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _require(condition):
    if not condition:
        raise ValueError("incompatible")


def _check_anchor(deployment: dict, network: dict) -> None:
    try:
        # Paridad con el contrato nativo; solo analiza datos, sin RPC ni clave.
        from app.adaptadores.chain import _validate_artifact
        _validate_artifact(deployment)
        _require("codigo" in deployment)
        chain = deployment["red"]["chainId"]
        _require(type(chain) is int and chain == network["chainId"])
        approved = _json(APPROVED_DEPLOYMENT)
        _require(approved["version"] == 1)
        if chain == approved["chainId"]:
            # El documento de red del llamador jamás reemplaza el ancla de Base.
            _require(deployment["red"]["nombre"] == approved["networkName"])
            expected = approved
        elif chain == approved["localTestChainId"]:
            _require(deployment["red"]["nombre"] == approved["localTestNetworkName"])
            expected = network.get("approvedLocalDeployment")
        else:
            raise ValueError("red no autorizada")
        _require(isinstance(expected, dict) and type(expected.get("chainId")) is int
                 and expected["chainId"] == chain)
        for key, expected_key in (("oraculo", "wallet"), ("registro", "registry"),
                                  ("verificador", "verifier")):
            _require(deployment[key].lower() == expected[expected_key].lower())
        expected_transactions = expected["transactions"]
        _require(isinstance(expected_transactions, list) and len(expected_transactions) == len(RECEIPTS))
        _require(deployment["nonceInicial"] == 0)
        _require(deployment["umbralDeDerivacionBps"] == 9000)
        _require(re.fullmatch(r"0x[0-9a-fA-F]{40}", network["gasPriceOracle"]))
        for key in ("oraculo", "registro", "verificador"):
            _require(re.fullmatch(r"0x[0-9a-fA-F]{40}", deployment[key]))
        for key in ("registroHash", "verificadorHash"):
            _require(re.fullmatch(r"0x[0-9a-fA-F]{64}", deployment["codigo"][key]))
        hashes = set()
        for index, key in enumerate(RECEIPTS):
            tx = deployment["transacciones"][key]
            receipt = tx["recibo"]
            _require(type(tx["nonce"]) is int and tx["nonce"] == index)
            _require(re.fullmatch(r"0x[0-9a-fA-F]{64}", tx["hash"]))
            _require(receipt["hash"].lower() == tx["hash"].lower() and receipt["estado"] == 1)
            pinned = expected_transactions[index]
            _require(pinned["stage"] == key and type(pinned["nonce"]) is int
                     and pinned["nonce"] == index and type(pinned["status"]) is int
                     and pinned["status"] == 1 and pinned["hash"].lower() == tx["hash"].lower())
            hashes.add(tx["hash"].lower())
        _require(len(hashes) == len(RECEIPTS))
        # El gate requiere estos cuatro selectores del ABI verdadero.
        names = {entry.get("name") for entry in deployment["abi"] if entry.get("type") == "function"}
        _require(names.issuperset({"registerClaim", "submitInference", "resolve", "settle"}))
    except (TypeError, ValueError, KeyError, AttributeError, IndexError):
        raise InvalidPlan("ancla de despliegue inválida.") from None


def _check_private(path: Path):
    if path.is_symlink() or path.exists():
        raise InvalidPlan("estado de corrida existente.")
    parent = path.parent
    if (not parent.is_dir() or parent.is_symlink() or parent.stat().st_uid != os.getuid()
            or parent.stat().st_mode & 0o077):
        raise InvalidPlan("directorio de corrida no privado.")


def prepare(args):
    metadata = _json(METADATA)
    image = Path(args.image)
    if (not image.is_file() or image.is_symlink() or image.stat().st_size != metadata["image_bytes"]
            or _digest(image) != metadata["image_sha256"]):
        raise InvalidPlan("imagen incompatible.")
    deployment = _json(Path(args.deployment))
    network = _json(Path(args.network_config))
    _check_anchor(deployment, network)
    expected = metadata["artifact_sha256"]
    artifacts = Path(args.artifacts)
    for filename in REQUIRED_ARTIFACTS:
        item = artifacts / filename
        if not item.is_file() or item.is_symlink() or _digest(item) != expected[filename]:
            raise InvalidPlan("artefactos incompatibles.")
    for filename in DERIVED_ARTIFACTS:
        item = artifacts / filename
        if item.exists() and (not item.is_file() or item.is_symlink() or _digest(item) != expected[filename]):
            raise InvalidPlan("artefactos incompatibles.")
    state, evidence = Path(args.state_dir), Path(args.evidence_dir)
    if state == evidence or state in evidence.parents or evidence in state.parents:
        raise InvalidPlan("directorios de corrida incompatibles.")
    _check_private(state)
    _check_private(evidence)
    return {"mode": "plan-only", "chainId": network["chainId"], "initialNonce": 4,
            "expectedTransactions": 6, "maxAdditionalTransactions": 16,
            "maxCumulativeEstimatedWei": 250_000_000_000_000,
            "controlledLostResponseAttempt": 6,
            "cases": [{"amount": "1850000.00", "path": "revisión humana"},
                      {"amount": "240000.00", "path": "liquidación manual"}],
            "imageSha256": metadata["image_sha256"],
            "note": "Preparación solamente; sin clientes, procesos, firmas ni emisiones."}


def _endpoint(value, scheme, *, default_port):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in scheme or parsed.hostname != "127.0.0.1" or
                not 0 < parsed.port < 65536 or parsed.port == default_port or
                parsed.fragment or parsed.query):
            raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise InvalidPlan("entorno aislado inválido.") from None


def _preflight_environment(deployment):
    """Lecturas locales sin key ni clientes de cadena; nunca migra ni inicializa servicios."""
    from sqlalchemy import inspect, text
    from sqlalchemy.engine import make_url
    from app.adaptadores.database import create_database_engine, verify_schema
    from redis import Redis
    from eth_account import Account

    db, redis_url, upstream, key = (os.environ.get(name, "") for name in
        ("DATABASE_URL", "REDIS_URL", "PFI165_UPSTREAM_RPC", "CADENA_CLAVE_ORACULO"))
    parsed = _endpoint(db, ("postgresql", "postgresql+psycopg", "postgres"), default_port=5432)
    redis_parsed = _endpoint(redis_url, ("redis",), default_port=6379)
    if (not re.fullmatch(r"pfi165_[a-zA-Z0-9_]+", parsed.path.lstrip("/")) or
            redis_parsed.path not in ("/0", "") or
            not upstream.startswith(("http://", "https://")) or
            not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", key)):
        raise InvalidPlan("entorno aislado inválido.")
    try:
        if Account.from_key(key).address.lower() != deployment["oraculo"].lower():
            raise InvalidPlan("identidad del oráculo incompatible.")
        engine = create_database_engine(db)
        try:
            verify_schema(engine)
            with engine.connect() as conn:
                owner = conn.scalar(text("SELECT pg_get_userbyid(datdba) = current_user FROM pg_database WHERE datname=current_database()"))
                others = conn.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"))
                tables = inspect(conn).get_table_names(schema="public")
                if not owner or others or not tables or any(
                    conn.scalar(text(f'SELECT count(*) FROM "{table}"')) != 0
                    for table in tables if table != "alembic_version"
                ):
                    raise InvalidPlan("base no vacía o no exclusiva.")
        finally:
            engine.dispose()
        client = Redis.from_url(redis_url, socket_timeout=3, socket_connect_timeout=3)
        try:
            if client.dbsize() != 0:
                raise InvalidPlan("Redis no vacío.")
        finally:
            client.close()
    except InvalidPlan:
        raise
    except Exception:
        raise InvalidPlan("entorno aislado inválido.") from None
    return db, redis_url, upstream, key


def _child_env(base, **values):
    # No heredar HOME real ni URL de upstream/proxy de la sesión interactiva.
    allowed = ("PATH", "VIRTUAL_ENV", "PYTHONPATH", "LANG", "LC_ALL", "TZ",
               "LD_LIBRARY_PATH", "PYTHONDONTWRITEBYTECODE")
    env = {name: base[name] for name in allowed if name in base}
    env.update(values)
    return env


def _spawn(argv, env, log, *, stdout=None):
    handle = log.open("ab", buffering=0)
    os.chmod(log, 0o600)
    try:
        process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                   stdout=stdout if stdout is not None else handle,
                                   stderr=handle, start_new_session=True)
    finally:
        handle.close()
    return process


def _stop(process):
    if process is None or process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http(base, method, path, *, body=None, token=None, content_type=None, timeout=45):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = content_type or "application/json"
    request = Request(base + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read(1_000_000))
    except HTTPError as error:
        try:
            return error.code, json.loads(error.read(1_000_000))
        except (ValueError, UnicodeError):
            raise InvalidPlan("respuesta HTTP inválida.") from None


def _expect(code, result, expected):
    if code != expected or not isinstance(result, dict):
        raise InvalidPlan(f"HTTP inesperado ({code}); corrida conservada.")
    return result


def _wait(check, processes, *, seconds=300):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if any(p.poll() is not None for p in processes):
            raise InvalidPlan("emisor detenido; corrida conservada.")
        try:
            result = check()
            if result is not None:
                return result
        except (OSError, TimeoutError):
            pass
        time.sleep(0.5)
    raise InvalidPlan("tiempo de espera excedido; corrida conservada.")


def _multipart(image, amount):
    boundary = "pfi165-synthetic-boundary"
    parts = [("declaredAmount", amount.encode()),
             ("damageDescription", "Prueba sintética PFI-165; no representa un daño real.".encode()),
             ("photos", image.read_bytes())]
    content = b""
    for name, value in parts:
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if name == "photos":
            disposition += '; filename="pfi165-synthetic.png"\r\nContent-Type: image/png'
        content += (f"--{boundary}\r\n{disposition}\r\n\r\n".encode() + value + b"\r\n")
    return content + f"--{boundary}--\r\n".encode(), "multipart/form-data; boundary=" + boundary


def _gate_rpc(gate, method, params):
    data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    code, result = _http(gate, "POST", "/", body=data)
    _expect(code, result, 200)
    if "error" in result or "result" not in result:
        raise InvalidPlan("lectura del gate rechazada; corrida conservada.")
    return result["result"]


def _check_onchain(gate, deployment):
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(gate, request_kwargs={"timeout": 20}))
    contract = w3.eth.contract(address=deployment["registro"], abi=deployment["abi"])
    if (w3.eth.chain_id != deployment["red"]["chainId"] or
            contract.functions.confidenceThresholdBps().call() != 9000 or
            contract.functions.humanConfirmationRequired().call() is not False or
            w3.eth.get_transaction_count(deployment["oraculo"], "latest") != 4 or
            w3.eth.get_transaction_count(deployment["oraculo"], "pending") != 4):
        raise InvalidPlan("estado on-chain incompatible; corrida conservada.")


def _check_config(base):
    config = _expect(*_http(base, "GET", "/configuracion", token="operador-op-02"), 200)
    def number(field):
        value = config[field]
        return value.get("value") if isinstance(value, dict) else value
    if (number("amountLimit") != 1_000_000 or number("derivationThreshold") != .90 or
            number("autoSettlementThreshold") != .95):
        raise InvalidPlan("configuración de aplicación incompatible; corrida conservada.")


def _queue(base):
    return _expect(*_http(base, "GET", "/cola", token="operador-op-02"), 200)


def _ledger(path, count):
    ledger = _json(path)
    attempts = ledger["attempts"]
    if (len(attempts) != count or ledger["max_transactions"] != 16 or
            ledger["max_wei"] != 250_000_000_000_000 or
            ledger["start_nonce"] != 4 or
            ledger["reserved_wei"] > ledger["max_wei"] or
            any(a["nonce"] != 4 + i for i, a in enumerate(attempts)) or
            len({a["hash"] for a in attempts}) != count):
        raise InvalidPlan("ledger incompatible; corrida conservada.")
    return ledger


def _case(base, gate, state, image, amount, expected, processes):
    body, kind = _multipart(image, amount)
    registered = _expect(*_http(base, "POST", "/reclamos", body=body, content_type=kind), 201)
    number, token = registered["number"], registered["accessToken"]
    initial = _http(base, "GET", f"/publico/reclamos/{number}/traza")
    if initial[0] not in (200, 409):
        raise InvalidPlan("traza inicial inválida; corrida conservada.")
    def finished():
        queue = _queue(base)
        if queue["counters"]["failed"]:
            raise InvalidPlan("job fallido; corrida conservada.")
        code, detail = _http(base, "GET", f"/reclamos/{number}", token="operador-op-02")
        _expect(code, detail, 200)
        if detail["statusId"] == expected and not queue["counters"]["running"] and not queue["counters"]["pending"]:
            return detail
    detail = _wait(finished, processes)
    trace = _expect(*_http(base, "GET", f"/publico/reclamos/{number}/traza"), 200)
    explanation = _expect(*_http(base, "GET", f"/reclamos/{number}/explicacion", token=token), 200)
    confidence = explanation["model"]["confidence"]
    proof_hash = trace["artifacts"]["proof"]["proofHash"]
    if (not 0.91 <= confidence <= 0.92 or not proof_hash or
            trace["statusId"] != expected or
            trace["artifacts"]["proof"]["circuitParameters"].get("simulated") or
            len(trace["featureVector"]) != 576 or
            not all(t["transactionHash"] for t in trace["transitions"] if t["to"] != "Registrado")):
        raise InvalidPlan("inferencia o prueba no acreditadas; corrida conservada.")
    return {"number": number, "statusId": expected, "confidence": confidence,
            "proofHash": proof_hash, "trace": trace, "detail": detail}


def execute(args, plan):
    deployment = _json(Path(args.deployment))
    db, redis_url, upstream, key = _preflight_environment(deployment)
    state, evidence = Path(args.state_dir), Path(args.evidence_dir)
    # No se intenta reiniciar una corrida ni recrear su ledger desde el nonce actual.
    _check_private(state)
    _check_private(evidence)
    state.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)
    children = []
    gate_process = api = worker = None
    root = Path(__file__).resolve().parents[1]
    try:
        home = state / "home"
        home.mkdir(mode=0o700)
        artifacts = state / "artifacts"
        artifacts.mkdir(mode=0o700)
        for filename in ARTIFACTS:
            src = Path(args.artifacts) / filename
            if src.is_file():
                shutil.copyfile(src, artifacts / filename)
                os.chmod(artifacts / filename, 0o400)
        gate_env = _child_env(os.environ, PFI165_UPSTREAM_RPC=upstream, HOME=str(home), TMPDIR=str(state))
        gate_command = [sys.executable, str(root / "scripts/pfi165_rpc_gate.py"),
                        "--deployment", args.deployment, "--network-config", args.network_config,
                        "--state", str(state / "ledger.json"), "--listen", "127.0.0.1:0",
                        "--max-transactions", "16", "--max-wei", "250000000000000",
                        "--expected-start-nonce", "4", "--lose-response-once-at", "6"]
        gate_process = _spawn(gate_command, gate_env, state / "gate.log", stdout=subprocess.PIPE)
        children.append(gate_process)
        with selectors.DefaultSelector() as selector:
            selector.register(gate_process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=40):
                raise InvalidPlan("gate no inició; corrida conservada.")
            line = gate_process.stdout.readline(4096)
        try:
            hello = json.loads(line)
            host, port = hello["listen"].split(":")
            if (host != "127.0.0.1" or not 0 < int(port) < 65536 or
                    hello["transactions"] != 0 or hello["reserved_wei"] != 0):
                raise ValueError()
        except (ValueError, KeyError):
            raise InvalidPlan("gate no inició; corrida conservada.") from None
        gate = "http://127.0.0.1:" + port
        _check_onchain(gate, deployment)
        child = _child_env(os.environ, DATABASE_URL=db, REDIS_URL=redis_url,
                           CADENA_RPC=gate, CADENA_DESPLIEGUE=args.deployment,
                           CADENA_CLAVE_ORACULO=key, MOTOR_INFERENCIA="onnx",
                           GENERADOR_PRUEBAS="ezkl", CLIENTE_CADENA="web3",
                           PUBLICADOR_EVIDENCIA="local", RUTA_ARTEFACTOS=str(artifacts),
                           RUTA_EVIDENCIA_LOCAL=str(state / "published"),
                           WORKER_CONCURRENCY="1", HOME=str(home), TMPDIR=str(state))
        with (state / "worker-preflight.log").open("wb") as log:
            preflight = subprocess.run([sys.executable, "-m", "app.queue.celery_app"],
                                      env=child, cwd=root, stdin=subprocess.DEVNULL,
                                      stdout=log, stderr=log, timeout=45)
        os.chmod(state / "worker-preflight.log", 0o600)
        if preflight.returncode != 0:
            raise InvalidPlan("preflight de worker rechazó la corrida; estado conservado.")
        api_port = _port()
        base = f"http://127.0.0.1:{api_port}"
        api_command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                       "--port", str(api_port), "--no-access-log"]
        worker_command = [sys.executable, "-m", "celery", "-A", "app.queue.celery_app:celery_app",
                          "worker", "--pool=prefork", "--concurrency=1", "--queues=pfi_jobs", "--loglevel=INFO"]
        api = _spawn(api_command, child, state / "api.log")
        children.append(api)
        _wait(lambda: True if _http(base, "GET", "/health")[0] == 200 else None, [api], seconds=60)
        _check_config(base)
        worker = _spawn(worker_command, child, state / "worker.log")
        children.append(worker)
        cases = []
        first = _case(base, gate, state, Path(args.image), "1850000.00", "RequiereRevision", [api, worker])
        cases.append(first)
        _ledger(state / "ledger.json", 2)
        response = _expect(*_http(base, "POST", f"/reclamos/{first['number']}/resolucion",
                                  body=json.dumps({"favorable": True, "justification":
                                                   "Resolución favorable de prueba sintética PFI-165; sin siniestro real."}).encode(),
                                  token="revisor-rev-04"), 200)
        if not response["transition"]["transactionHash"]:
            raise InvalidPlan("resolución no confirmada; corrida conservada.")
        _ledger(state / "ledger.json", 3)
        second = _case(base, gate, state, Path(args.image), "240000.00", "Aprobable", [api, worker])
        cases.append(second)
        queue = _queue(base)
        if queue["counters"]["pending"] or queue["counters"]["running"]:
            raise InvalidPlan("cola no drenada; corrida conservada.")
        _ledger(state / "ledger.json", 5)
        _stop(worker)
        worker = None
        status, uncertain = _http(base, "POST", f"/reclamos/{second['number']}/liquidacion",
                                  token="operador-op-02", body=b"", content_type="application/json")
        if status != 409 or uncertain.get("title") != "Operación del oráculo incierta.":
            raise InvalidPlan("respuesta incierta ausente; corrida conservada.")
        match = re.search(r"[0-9a-fA-F-]{36}", uncertain.get("detail", ""))
        if not match:
            raise InvalidPlan("UUID de recuperación ausente; corrida conservada.")
        operation_id = str(UUID(match.group()))
        ledger = _ledger(state / "ledger.json", 6)
        lost = ledger["attempts"][-1]
        before = _queue(base)
        operations = [item for item in before["oracleOperations"] if item["operationId"] == operation_id]
        if (not before["oracleBlocked"] or len(operations) != 1 or
                operations[0]["transactionHash"].lower() != lost["hash"]):
            raise InvalidPlan("bloqueo/identidad antes del reinicio ausente; corrida conservada.")
        _stop(api)
        api = _spawn(api_command, child, state / "api-restarted.log")
        children.append(api)
        _wait(lambda: True if _http(base, "GET", "/health")[0] == 200 else None, [api], seconds=60)
        def recovered():
            item = _expect(*_http(base, "GET", f"/reclamos/{second['number']}", token="operador-op-02"), 200)
            queue_now = _queue(base)
            if item["statusId"] == "Aprobado" and not queue_now["oracleBlocked"]:
                return item
        terminal = _wait(recovered, [api], seconds=120)
        before_cli = _ledger(state / "ledger.json", 6)
        with (state / "reconcile.log").open("wb") as log:
            reconciliation = subprocess.run([sys.executable, "-m", "app.adaptadores.reconcile",
                                             "--operation-id", operation_id], cwd=root, env=child,
                                            stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=60)
        os.chmod(state / "reconcile.log", 0o600)
        if reconciliation.returncode != 0:
            raise InvalidPlan("CLI de reconciliación falló; corrida conservada.")
        worker = _spawn(worker_command, child, state / "worker-restarted.log")
        children.append(worker)
        time.sleep(3)
        final = _ledger(state / "ledger.json", 6)
        if (final["attempts"] != before_cli["attempts"] or
                not all(a["status"] == "confirmed" and a["actual_wei"] is not None for a in final["attempts"]) or
                not final["fault_consumed"] or
                int(_gate_rpc(gate, "eth_getTransactionCount", [deployment["oraculo"], "latest"]), 16) != 10 or
                _queue(base)["oracleBlocked"]):
            raise InvalidPlan("reconciliación duplicada/incompleta; corrida conservada.")
        for attempt in final["attempts"]:
            receipt = _gate_rpc(gate, "eth_getTransactionReceipt", [attempt["hash"]])
            if receipt is None or int(receipt["status"], 16) != 1:
                raise InvalidPlan("receipt ausente; corrida conservada.")
        result = {"scope": "local-only" if plan["chainId"] != 84532 else "testnet-bound",
                  "imageSha256": plan["imageSha256"], "automaticCaseVerified": False,
                  "secondMachineRnf39Verified": False,
                  "cases": [{key: case[key] for key in ("number", "statusId", "confidence", "proofHash")}
                            for case in cases],
                  "recoveredOperationId": operation_id, "blockedBeforeRestart": True,
                  "recoveredStatusId": terminal["statusId"], "cliExit": reconciliation.returncode,
                  "attempts": [{key: attempt[key] for key in
                                ("hash", "nonce", "estimate_wei", "actual_wei", "block_number", "status")}
                               for attempt in final["attempts"]],
                  "reservedWei": final["reserved_wei"], "actualSpentWei": final["actual_spent_wei"]}
        target = evidence / "result.json"
        with target.open("x") as output:
            json.dump(result, output, indent=2, ensure_ascii=False)
        os.chmod(target, 0o600)
        print(json.dumps({"evidence": str(target), "transactions": 6, "recovered": True}))
    finally:
        for process in reversed(children):
            _stop(process)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Corrida PFI-165: plan puro por defecto; ejecución explícita aislada.")
    for name in ("deployment", "network-config", "image", "artifacts", "state-dir", "evidence-dir"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--execute", action="store_true", help="Ejecutar solo con servicios frescos y aislados")
    args = parser.parse_args(argv)
    try:
        plan = prepare(args)
        if args.execute:
            execute(args, plan)
        else:
            print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        return 0
    except InvalidPlan as error:
        print(f"Preparación rechazada: {error}", file=sys.stderr)
    except Exception as error:
        # Las excepciones de proveedores pueden incluir URL, key o calldata.
        print("Preparación rechazada: fallo interno (" + type(error).__name__ + "); conservar estado y consultar logs privados.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

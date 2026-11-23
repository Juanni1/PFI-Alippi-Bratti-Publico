"""Pruebas acotadas del aislamiento del smoke manual de PFI-138."""

import asyncio
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import quote
from pathlib import Path

import websockets

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "pfi138_browser_smoke.py"
spec = importlib.util.spec_from_file_location("pfi138_browser_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium no instalado")
def test_capture_redaction_hides_sidebar_and_input_token():
    with tempfile.TemporaryDirectory() as profile:
        browser = subprocess.Popen(
            ["chromium", "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-allow-origins=*", "--remote-debugging-port=0",
             f"--user-data-dir={profile}", "about:blank"],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            port_file = Path(profile) / "DevToolsActivePort"
            # El arranque del navegador tiene su propio plazo, no el de una llamada CDP.
            deadline = time.monotonic() + 30
            while not port_file.exists() and browser.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            assert port_file.exists(), (
                f"Chromium no publicó DevToolsActivePort en 30s (exit={browser.poll()})"
            )
            port = port_file.read_text().splitlines()[0]
            pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
            url = next(page["webSocketDebuggerUrl"] for page in pages if page["type"] == "page")

            async def check():
                async with websockets.connect(url, origin="http://localhost") as ws:
                    serial = 0

                    async def call(method, params=None):
                        nonlocal serial
                        serial += 1
                        ident = serial
                        await ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
                        while True:
                            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                            if message.get("id") == ident:
                                return message["result"]

                    async def evaluate(expr):
                        result = await call("Runtime.evaluate", {
                            "expression": expr, "returnByValue": True,
                        })
                        return result["result"].get("value")

                    await call("Page.enable")
                    html = "<aside><input value='operador-op-02'></aside><p>Sesión: operador-op-02</p>"
                    await call("Page.navigate", {"url": "data:text/html;charset=utf-8," + quote(html)})
                    deadline = time.monotonic() + 5
                    while not await evaluate("document.querySelector('aside') !== null"):
                        assert time.monotonic() < deadline
                        await asyncio.sleep(0.05)
                    await evaluate(smoke.CAPTURE_REDACTION_JS)
                    state = await evaluate("JSON.stringify({hidden: getComputedStyle(document.querySelector('aside')).visibility, value: document.querySelector('input').value, text: document.body.innerText})")
                    assert json.loads(state) == {
                        "hidden": "hidden", "value": "[oculta]", "text": "Sesión: [oculta]",
                    }

            asyncio.run(check())
        finally:
            smoke.stop(browser)


def test_screenshot_stays_in_temporary_directory_and_disappears():
    with tempfile.TemporaryDirectory() as scratch:
        target = smoke.screenshot_path(Path(scratch), "blocked")
        assert target.parent == Path(scratch)
        target.write_bytes(b"\x89PNG\r\n\x1a\n")
        assert target.exists()
    assert not target.exists()


def test_stop_terminates_descendant_server_too():
    root = subprocess.Popen(
        [sys.executable, "-c", "import subprocess,time; "
         "child=subprocess.Popen(['sleep','30']); print(child.pid,flush=True); time.sleep(30)"],
        start_new_session=True, stdout=subprocess.PIPE, text=True,
    )
    child_pid = int(root.stdout.readline())
    try:
        smoke.stop(root)
        assert root.poll() is not None
        try:
            state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
        except (FileNotFoundError, ProcessLookupError):
            state = "gone"
        assert state in ("gone", "Z"), "el servidor hijo continuó ejecutándose"
    finally:
        try:
            os.killpg(root.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        root.wait(timeout=3)


class _Provider:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def make_request(self, name, params):
        self.calls.append((name, params))
        return {"result": self.result}


class _Chain:
    def __init__(self, result):
        self.w3 = type("Web3", (), {"provider": _Provider(result)})()


def test_automine_restoration_fails_closed_when_rpc_rejects():
    chain = _Chain(False)
    with pytest.raises(RuntimeError, match="automine"):
        smoke.restore_automine(chain)
    assert chain.w3.provider.calls == [("evm_setAutomine", [True])]


def test_automine_restoration_checks_readback_before_mining():
    chain = _Chain(True)
    smoke.restore_automine(chain)
    assert chain.w3.provider.calls == [
        ("evm_setAutomine", [True]),
        ("hardhat_getAutomine", []),
        ("evm_mine", []),
    ]

"""Pruebas del límite público del gate con una RPC local aislada."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from hexbytes import HexBytes
from eth_utils import keccak, to_checksum_address

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/pfi165_rpc_gate.py"
ROOT = Path(tempfile.gettempdir())
KEY = "0x" + "01" * 32  # Clave pública de prueba; jamás se usa fuera de la RPC falsa.
WALLET = Account.from_key(KEY).address
REGISTRY = "0x" + "ab" * 20
VERIFIER = "0x" + "cd" * 20
ORACLE = "0x" + "ef" * 20
CHAIN = 84532
ABI = [{"type": "function", "name": name, "inputs": inputs} for name, inputs in (
    ("registerClaim", [{"type": "bytes32"}, {"type": "bytes32"}]),
    ("submitInference", [{"type": "bytes32"}, {"type": "bytes"}]),
    ("resolve", [{"type": "bytes32"}, {"type": "bool"}, {"type": "bytes32"}]),
    ("settle", [{"type": "bytes32"}, {"type": "bool"}]),
)]


def selector(name):
    from eth_utils.abi import function_signature_to_4byte_selector
    item = next(i for i in ABI if i["name"] == name)
    return "0x" + function_signature_to_4byte_selector(
        name + "(" + ",".join(v["type"] for v in item["inputs"]) + ")").hex()


@pytest.fixture
def environment():
    ROOT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT) as directory:
        path = Path(directory)
        class Upstream(BaseHTTPRequestHandler):
            sends = []
            snapshot = None
            nonce = 4
            receipt = None
            transaction = None
            l1 = 11
            operator = 7
            error_method = None
            operator_calls = []
            def log_message(self, *_):
                pass
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method, params = request["method"], request.get("params", [])
                result = {
                    "eth_chainId": hex(CHAIN), "eth_getTransactionCount": hex(self.nonce),
                    "eth_getBalance": hex(10**18), "eth_getCode": "0x6000",
                    "web3_clientVersion": "local-test-provider",
                    "eth_getTransactionReceipt": self.receipt, "eth_getTransactionByHash": self.transaction,
                }.get(method)
                if method == "eth_call":
                    data = params[0]["data"]
                    if data == "0x" + keccak(text="getL1FeeUpperBound(uint256)")[:4].hex():
                        result = hex(self.l1) if self.l1 is not None else None
                    elif data.startswith("0x" + keccak(text="getL1FeeUpperBound(uint256)")[:4].hex()):
                        result = hex(self.l1) if self.l1 is not None else None
                    elif data.startswith("0x" + keccak(text="getOperatorFee(uint256)")[:4].hex()):
                        self.operator_calls.append(params[1])
                        result = hex(self.operator) if self.operator is not None else None
                    elif data == "0x" + keccak(text="oracle()")[:4].hex():
                        result = "0x" + WALLET[2:].lower().rjust(64, "0")
                    elif data == "0x" + keccak(text="verifier()")[:4].hex():
                        result = "0x" + VERIFIER[2:].lower().rjust(64, "0")
                if method == "eth_sendRawTransaction":
                    type(self).snapshot = (path / "ledger.json").read_bytes()
                    self.sends.append(params[0]); result = "0x" + keccak(bytes.fromhex(params[0][2:])).hex()
                if method == self.error_method:
                    body = json.dumps({"jsonrpc": "2.0", "id": request["id"],
                        "error": {"code": -32000, "message": "private-rpc-token " + str(params)}}).encode()
                else:
                    body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        artifact = {"red": {"chainId": CHAIN}, "oraculo": WALLET, "registro": REGISTRY,
                    "verificador": VERIFIER, "abi": ABI,
                    "codigo": {"registroHash": "0x" + keccak(bytes.fromhex("6000")).hex(),
                               "verificadorHash": "0x" + keccak(bytes.fromhex("6000")).hex()}}
        network = {"chainId": CHAIN, "gasPriceOracle": ORACLE}
        (path / "artifact.json").write_text(json.dumps(artifact))
        (path / "network.json").write_text(json.dumps(network))
        spec = importlib.util.spec_from_file_location("pfi165_rpc_gate", SCRIPT)
        gate_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate_module)
        try:
            yield gate_module, Upstream, f"http://127.0.0.1:{server.server_port}", path
        finally:
            server.shutdown(); server.server_close(); thread.join()


def signed(nonce=4, *, to=REGISTRY, value=0, data=None):
    return "0x" + Account.sign_transaction({"chainId": CHAIN, "nonce": nonce, "to": to_checksum_address(to),
        "value": value, "data": data or selector("settle") + "00" * 64,
        "gas": 210000, "maxFeePerGas": 100, "maxPriorityFeePerGas": 0, "type": 2}, KEY).raw_transaction.hex()


def gate_for(environment, *, limit=1, budget=21000018, fault=None):
    module, _, url, path = environment
    return module.Gate(path / "artifact.json", path / "network.json", path / "ledger.json",
                       url, limit, budget, 4, fault)


def receipt_for(upstream, raw, digest, *, actual=True, block="0x5"):
    decoded = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
    upstream.transaction = {"hash": digest, "from": WALLET, "to": REGISTRY,
        "input": "0x" + decoded["data"].hex(), "nonce": hex(decoded["nonce"]),
        "chainId": hex(CHAIN), "value": "0x0"}
    upstream.receipt = {"transactionHash": digest, "from": WALLET, "to": REGISTRY,
        "status": "0x1", "blockNumber": block}
    if actual:
        upstream.receipt.update({"gasUsed": hex(210000), "effectiveGasPrice": hex(100),
                                 "l1Fee": hex(11), "operatorFee": hex(7)})


def test_guard_and_durable_reservation(environment):
    module, upstream, url, path = environment
    gate = module.Gate(path / "artifact.json", path / "network.json", path / "ledger.json",
                       url, 1, 21000018, 4, None)
    bad = signed(value=1)
    before = (path / "ledger.json").read_bytes()
    with pytest.raises(module.Denied):
        gate.handle("eth_sendRawTransaction", [bad])
    assert (path / "ledger.json").read_bytes() == before
    assert upstream.sends == []
    raw = signed()
    assert gate.handle("eth_sendRawTransaction", [raw]) == "0x" + keccak(bytes.fromhex(raw[2:])).hex()
    ledger = json.loads((path / "ledger.json").read_text())
    assert ledger["reserved_wei"] == 21000018
    assert ledger["attempts"][0]["nonce"] == 4
    assert raw not in (path / "ledger.json").read_text()
    assert len(upstream.sends) == 1
    assert json.loads(upstream.snapshot)["attempts"][0]["hash"] == ledger["attempts"][0]["hash"]
    with pytest.raises(module.Denied):
        gate.handle("eth_sendRawTransaction", [raw])
    assert len(upstream.sends) == 1


def test_methods_quotes_and_restart(environment):
    module, upstream, _, path = environment
    gate = gate_for(environment, limit=2, budget=42000036)
    baseline = (path / "ledger.json").read_bytes()
    for method in ("eth_sendTransaction", "personal_sign", "wallet_sendTransaction", "eth_sendBundle", "unknown"):
        with pytest.raises(module.Denied):
            gate.handle(method, [])
    for raw in (signed(to=VERIFIER), signed(nonce=5), signed(data="0x12345678"), signed(value=1)):
        with pytest.raises(module.Denied):
            gate.handle("eth_sendRawTransaction", [raw])
    upstream.l1 = None
    with pytest.raises(module.Denied):
        gate.handle("eth_sendRawTransaction", [signed()])
    assert (path / "ledger.json").read_bytes() == baseline
    assert upstream.sends == []
    upstream.l1 = 11
    assert gate.handle("eth_chainId", []) == hex(CHAIN)
    assert gate.handle("web3_clientVersion", []) == "local-test-provider"
    gate.handle("eth_sendRawTransaction", [signed()])
    with pytest.raises(BlockingIOError):
        gate_for(environment, limit=2, budget=42000036)
    os.close(gate.owner)
    with pytest.raises(module.Denied):
        gate_for(environment, limit=3, budget=42000036)
    with pytest.raises(module.Denied):
        gate_for(environment, limit=2, budget=42000037)
    resumed = gate_for(environment, limit=2, budget=42000036)
    assert resumed.ledger["attempts"][0]["status"] == "uncertain"
    assert len(upstream.sends) == 1


def test_concurrent_calls_reserve_only_once(environment):
    import concurrent.futures
    upstream = environment[1]
    gate = gate_for(environment, limit=2, budget=42000036)
    def attempt(_):
        try:
            gate.handle("eth_sendRawTransaction", [signed()])
            return "sent"
        except environment[0].Denied:
            return "rejected"
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    assert results.count("sent") == 1
    assert len(upstream.sends) == 1
    assert len(json.loads((environment[3] / "ledger.json").read_text())["attempts"]) == 1


def test_missing_ledger_cannot_reset_previous_run(environment):
    import os
    module, upstream, _, path = environment
    gate = gate_for(environment)
    os.close(gate.owner)
    (path / "ledger.json").rename(path / "previous-ledger.json")
    with pytest.raises(module.Denied):
        gate_for(environment)
    assert not (path / "ledger.json").exists()
    assert upstream.sends == []


def test_grant_limits_cannot_be_raised_at_initialization(environment):
    module, upstream, _, path = environment
    with pytest.raises(module.Denied):
        gate_for(environment, limit=17, budget=250000000000000)
    with pytest.raises(module.Denied):
        gate_for(environment, limit=16, budget=250000000000001)
    assert not (path / "ledger.json").exists()
    assert upstream.sends == []


def test_budget_and_operator_quote_rejection(environment):
    import os
    module, upstream, _, path = environment
    gate = gate_for(environment, budget=21000017)
    before = (path / "ledger.json").read_bytes()
    with pytest.raises(module.Denied):
        gate.handle("eth_sendRawTransaction", [signed()])
    assert upstream.sends == []
    assert (path / "ledger.json").read_bytes() == before
    os.close(gate.owner)
    # Un ledger previo con presupuesto más bajo no puede ampliarse.
    with pytest.raises(module.Denied):
        gate_for(environment, budget=21000018)


def test_atomic_failure_preserves_previous_record(environment, monkeypatch):
    module, upstream, _, path = environment
    gate = gate_for(environment)
    previous = (path / "ledger.json").read_bytes()
    def fail(*_):
        raise OSError("private-rpc-token")
    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(module.Denied):
        gate.handle("eth_sendRawTransaction", [signed()])
    assert (path / "ledger.json").read_bytes() == previous
    assert upstream.sends == []
    assert gate.ledger["reserved_wei"] == 0


def test_reject_corrupt_restart(environment):
    import os
    module, upstream, _, path = environment
    gate = gate_for(environment)
    os.close(gate.owner)
    ledger = json.loads((path / "ledger.json").read_text())
    ledger["attempts"] = [{"hash": "0xdead", "nonce": 4, "estimate_wei": 0, "status": "confirmed"}]
    (path / "ledger.json").write_text(json.dumps(ledger))
    upstream.nonce = 5
    with pytest.raises(module.Denied):
        gate_for(environment)
    assert upstream.sends == []


def test_verified_receipt_recovery_and_no_rebroadcast(environment):
    module, upstream, _, path = environment
    gate = gate_for(environment, limit=2, budget=42000036)
    raw = signed()
    identity = gate.handle("eth_sendRawTransaction", [raw])
    receipt_for(upstream, raw, identity)
    assert gate.handle("eth_sendRawTransaction", [raw]) == identity
    assert gate.ledger["attempts"][0]["status"] == "confirmed"
    assert len(upstream.sends) == 1
    upstream.nonce = 5
    os.close(gate.owner)
    resumed = gate_for(environment, limit=2, budget=42000036)
    assert resumed.ledger["attempts"][0]["block_number"] == 5
    with pytest.raises(module.Denied):
        resumed.handle("eth_sendRawTransaction", [raw])
    assert len(upstream.sends) == 1
    resumed.handle("eth_sendRawTransaction", [signed(nonce=5)])
    assert len(upstream.sends) == 2
    assert json.loads((path / "ledger.json").read_text())["reserved_wei"] == 42000036


def test_cli_rejects_non_loopback_without_creating_ledger(environment):
    _, upstream, url, path = environment
    result = subprocess.run([sys.executable, str(SCRIPT), "--deployment", str(path / "artifact.json"),
        "--network-config", str(path / "network.json"), "--state", str(path / "ledger.json"),
        "--listen", "0.0.0.0:0", "--max-transactions", "1", "--max-wei", "21000018",
        "--expected-start-nonce", "4"], env={"PFI165_UPSTREAM_RPC": url,
        "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=15)
    assert (result.returncode, result.stdout, result.stderr) == (
        1, "", "Error: gate no disponible; verifique configuración, ledger y RPC.\n")
    assert not (path / "ledger.json").exists()
    assert upstream.sends == []


def test_cli_readiness_on_dynamic_local_port(environment):
    _, upstream, url, path = environment
    process = subprocess.Popen([sys.executable, str(SCRIPT), "--deployment", str(path / "artifact.json"),
        "--network-config", str(path / "network.json"), "--state", str(path / "ledger.json"),
        "--listen", "127.0.0.1:0", "--max-transactions", "1", "--max-wei", "21000018",
        "--expected-start-nonce", "4"], env={"PFI165_UPSTREAM_RPC": url,
        "PYTHONDONTWRITEBYTECODE": "1"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = process.stdout.readline()
        readiness = json.loads(line)
        assert readiness == {"listen": readiness["listen"], "transactions": 0, "reserved_wei": 0}
        assert readiness["listen"].startswith("127.0.0.1:")
        assert int(readiness["listen"].rsplit(":", 1)[1]) > 0
        payload = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "eth_chainId", "params": []}).encode()
        with urlopen(Request("http://" + readiness["listen"], payload)) as response:
            assert json.load(response) == {"jsonrpc": "2.0", "id": 2, "result": hex(CHAIN)}
        from web3 import Web3
        assert Web3(Web3.HTTPProvider("http://" + readiness["listen"])).is_connected()
        assert upstream.sends == []
    finally:
        process.terminate(); process.communicate(timeout=15)


def test_upstream_failure_redacts_credentials_and_signed_bytes(environment):
    module, upstream, _, path = environment
    gate = gate_for(environment)
    before = (path / "ledger.json").read_bytes()
    upstream.error_method = "eth_getBalance"
    raw = signed()
    with pytest.raises(module.Denied) as failure:
        gate.handle("eth_sendRawTransaction", [raw])
    assert "private-rpc-token" not in str(failure.value)
    assert raw not in str(failure.value)
    assert (path / "ledger.json").read_bytes() == before
    assert upstream.sends == []


def test_http_error_has_request_id_and_no_side_effect(environment):
    module, upstream, _, path = environment
    gate = gate_for(environment)
    server = module.LocalServer(("127.0.0.1", 0), gate)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        before = (path / "ledger.json").read_bytes()
        payload = json.dumps({"jsonrpc": "2.0", "id": 17, "method": "eth_sendTransaction", "params": []}).encode()
        request = Request(f"http://127.0.0.1:{server.server_port}", payload, {"Content-Type": "application/json"})
        with urlopen(request) as response:
            body = json.load(response)
        assert body == {"jsonrpc": "2.0", "id": 17,
                        "error": {"code": -32000, "message": "Solicitud rechazada; consulte el ledger."}}
        assert upstream.sends == []
        assert (path / "ledger.json").read_bytes() == before
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_restart_rejects_missing_confirmed_receipt(environment):
    import os
    module, upstream, _, path = environment
    gate = gate_for(environment, limit=2, budget=42000036)
    raw = signed()
    identity = gate.handle("eth_sendRawTransaction", [raw])
    receipt_for(upstream, raw, identity)
    assert gate.handle("eth_sendRawTransaction", [raw]) == identity
    upstream.nonce = 5
    os.close(gate.owner)
    upstream.receipt = None
    before = (path / "ledger.json").read_bytes()
    with pytest.raises(module.Denied):
        gate_for(environment, limit=2, budget=42000036)
    assert (path / "ledger.json").read_bytes() == before
    assert len(upstream.sends) == 1


def test_controlled_fault_is_durable(environment):
    module, upstream, _, path = environment
    gate = gate_for(environment, limit=2, budget=42000036, fault=1)
    with pytest.raises(module.Denied):
        gate.handle("eth_sendRawTransaction", [signed()])
    ledger = json.loads((path / "ledger.json").read_text())
    assert ledger["fault_consumed"] is True
    assert ledger["attempts"][0]["status"] == "uncertain"
    assert len(upstream.sends) == 1
    with pytest.raises(module.Denied):
        gate.handle("eth_sendRawTransaction", [signed(nonce=5)])
    assert len(upstream.sends) == 1


def test_queued_after_real_persistence_failure(environment, monkeypatch):
    module, upstream, _, path = environment
    gate = gate_for(environment)
    original_bytes = (path / "ledger.json").read_bytes()
    entered, release, queued = threading.Event(), threading.Event(), threading.Event()
    original_replace = module.os.replace
    calls = [0]
    def fail_once(*args):
        calls[0] += 1
        if calls[0] == 1:
            entered.set()
            assert release.wait(5)
            raise OSError("disk full")
        return original_replace(*args)
    monkeypatch.setattr(module.os, "replace", fail_once)
    original_lock = gate.mutex
    class TrackedLock:
        def __enter__(self):
            if entered.is_set():
                queued.set()
            return original_lock.__enter__()
        def __exit__(self, *args):
            return original_lock.__exit__(*args)
    gate.mutex = TrackedLock()
    outcomes = []
    def send():
        try:
            gate.handle("eth_sendRawTransaction", [signed()])
            outcomes.append("sent")
        except module.Denied as exc:
            outcomes.append(str(exc))
    one = threading.Thread(target=send)
    two = threading.Thread(target=send)
    one.start()
    assert entered.wait(5)
    two.start()
    assert queued.wait(5)
    release.set()
    one.join(5)
    two.join(5)
    assert not one.is_alive() and not two.is_alive()
    assert len(outcomes) == 2 and all("Persistencia" in result for result in outcomes)
    assert gate.poisoned is True
    assert gate.ledger["attempts"] == [] and gate.ledger["reserved_wei"] == 0
    assert (path / "ledger.json").read_bytes() == original_bytes
    assert upstream.sends == []


def test_actual_fee_over_budget_before_next_send(environment):
    module, upstream, _, path = environment
    gate = gate_for(environment, limit=2, budget=42000036)
    raw = signed()
    identity = gate.handle("eth_sendRawTransaction", [raw])
    receipt_for(upstream, raw, identity)
    upstream.receipt.update({"gasUsed": "0x33450", "effectiveGasPrice": "0x64",
                             "l1Fee": hex(42000036), "operatorFee": "0x64"})
    assert gate.handle("eth_getTransactionReceipt", [identity]) == upstream.receipt
    upstream.nonce = 5
    before = (path / "ledger.json").read_bytes()
    with pytest.raises(module.Denied):
        gate.handle("eth_sendRawTransaction", [signed(nonce=5)])
    ledger = json.loads(before)
    assert ledger["reserved_wei"] == 21000018
    assert ledger["actual_spent_wei"] > ledger["max_wei"]
    assert ledger["attempts"][0]["hash"] == identity
    assert ledger["attempts"][0]["actual_wei"] == ledger["actual_spent_wei"]
    assert (path / "ledger.json").read_bytes() == before
    assert len(upstream.sends) == 1


def test_version_one_ledger_cannot_silently_migrate(environment):
    module, upstream, _, path = environment
    gate = gate_for(environment, limit=2, budget=42000036)
    os.close(gate.owner)
    ledger = json.loads((path / "ledger.json").read_text())
    ledger["version"] = 1
    ledger.pop("actual_spent_wei")
    previous = json.dumps(ledger)
    (path / "ledger.json").write_text(previous)
    with pytest.raises(module.Denied):
        gate_for(environment, limit=2, budget=42000036)
    assert (path / "ledger.json").read_text() == previous
    assert upstream.sends == []


def test_restart_blocks_unknown_actual_fee(environment):
    module, upstream, _, path = environment
    gate = gate_for(environment, limit=2, budget=42000036)
    raw = signed()
    identity = gate.handle("eth_sendRawTransaction", [raw])
    receipt_for(upstream, raw, identity, actual=False)
    assert gate.handle("eth_getTransactionReceipt", [identity]) == upstream.receipt
    upstream.nonce = 5
    os.close(gate.owner)
    resumed = gate_for(environment, limit=2, budget=42000036)
    before = (path / "ledger.json").read_bytes()
    with pytest.raises(module.Denied):
        resumed.handle("eth_sendRawTransaction", [signed(nonce=5)])
    ledger = json.loads(before)
    assert ledger["attempts"][0]["hash"] == identity
    assert ledger["attempts"][0]["status"] == "confirmed"
    assert ledger["attempts"][0]["actual_wei"] is None
    assert ledger["actual_spent_wei"] == 0
    assert (path / "ledger.json").read_bytes() == before
    assert len(upstream.sends) == 1


def test_missing_operator_quote_preserves_unknown_cost(environment):
    module, upstream, _, path = environment
    gate = gate_for(environment, limit=2, budget=42000036)
    raw = signed()
    identity = gate.handle("eth_sendRawTransaction", [raw])
    receipt_for(upstream, raw, identity)
    upstream.receipt.pop("operatorFee")
    upstream.operator = None
    assert gate.handle("eth_getTransactionReceipt", [identity]) == upstream.receipt
    upstream.nonce = 5
    before = (path / "ledger.json").read_bytes()
    assert json.loads(before)["attempts"][0]["actual_wei"] is None
    with pytest.raises(module.Denied):
        gate.handle("eth_sendRawTransaction", [signed(nonce=5)])
    assert (path / "ledger.json").read_bytes() == before
    assert len(upstream.sends) == 1


def test_zero_operator_fee_valid_at_receipt_block(environment):
    module, upstream, _, path = environment
    gate = gate_for(environment, limit=2, budget=42000036)
    raw = signed()
    identity = gate.handle("eth_sendRawTransaction", [raw])
    receipt_for(upstream, raw, identity)
    upstream.receipt.pop("operatorFee")
    upstream.operator = 0
    assert gate.handle("eth_getTransactionReceipt", [identity]) == upstream.receipt
    ledger = json.loads((path / "ledger.json").read_text())
    assert ledger["attempts"][0]["actual_wei"] == 21000011
    assert ledger["actual_spent_wei"] == 21000011
    assert len(upstream.sends) == 1


def test_operator_fee_fallback_uses_receipt_block(environment):
    module, upstream, _, path = environment
    gate = gate_for(environment, limit=2, budget=42000036)
    raw = signed()
    identity = gate.handle("eth_sendRawTransaction", [raw])
    receipt_for(upstream, raw, identity, block="0x9")
    upstream.receipt.pop("operatorFee")
    upstream.operator_calls.clear()
    assert gate.handle("eth_getTransactionReceipt", [identity]) == upstream.receipt
    ledger = json.loads((path / "ledger.json").read_text())
    assert upstream.operator_calls == ["0x9"]
    assert ledger["attempts"][0]["actual_wei"] == 21000007 + 11
    assert ledger["actual_spent_wei"] == ledger["attempts"][0]["actual_wei"]
    assert len(upstream.sends) == 1


def test_actual_spent_plus_next_estimate_blocks_before_reservation(environment):
    module, upstream, _, path = environment
    gate = gate_for(environment, limit=2, budget=42000036)
    raw = signed()
    identity = gate.handle("eth_sendRawTransaction", [raw])
    receipt_for(upstream, raw, identity)
    upstream.receipt["l1Fee"] = hex(4_000_000)
    assert gate.handle("eth_getTransactionReceipt", [identity]) == upstream.receipt
    upstream.nonce = 5
    before = (path / "ledger.json").read_bytes()
    assert json.loads(before)["actual_spent_wei"] < 42000036
    with pytest.raises(module.Denied):
        gate.handle("eth_sendRawTransaction", [signed(nonce=5)])
    assert (path / "ledger.json").read_bytes() == before
    assert len(upstream.sends) == 1


def test_restart_blocks_observed_overrun(environment):
    module, upstream, _, path = environment
    gate = gate_for(environment, limit=2, budget=42000036)
    raw = signed()
    identity = gate.handle("eth_sendRawTransaction", [raw])
    receipt_for(upstream, raw, identity)
    upstream.receipt.update({"gasUsed": "0x33450", "effectiveGasPrice": "0x64",
                             "l1Fee": hex(42000036), "operatorFee": "0x64"})
    assert gate.handle("eth_getTransactionReceipt", [identity]) == upstream.receipt
    upstream.nonce = 5
    os.close(gate.owner)
    resumed = gate_for(environment, limit=2, budget=42000036)
    before = (path / "ledger.json").read_bytes()
    with pytest.raises(module.Denied):
        resumed.handle("eth_sendRawTransaction", [signed(nonce=5)])
    ledger = json.loads(before)
    assert ledger["attempts"][0]["hash"] == identity
    assert ledger["actual_spent_wei"] > ledger["max_wei"]
    assert (path / "ledger.json").read_bytes() == before
    assert len(upstream.sends) == 1

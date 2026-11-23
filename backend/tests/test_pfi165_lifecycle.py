"""Contrato público de preparación local: ningún fallo de entrada crea ni usa emisores."""
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, urlopen
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
IMAGE = ROOT / "tests/fixtures/pfi165-synthetic.png"
METADATA = ROOT / "tests/fixtures/pfi165-synthetic.json"
SCRIPT = ROOT / "scripts/pfi165_lifecycle.py"
STAGES = ("verificador", "registro", "umbral", "confirmacionHumana")
PUBLIC_DEPLOYMENT = ROOT / "tests/fixtures/pfi165-base-sepolia-deployment.json"
BASE_NETWORK = ROOT / "tests/fixtures/pfi165-base-sepolia-network.json"


def inputs(tmp_path):
    artifact = {
        "red": {"nombre": "localhost", "chainId": 31337}, "oraculo": "0x" + "11" * 20,
        "registro": "0x" + "22" * 20, "verificador": "0x" + "33" * 20,
        "umbralDeDerivacionBps": 9000,
        "bytecodeDelVerificador": {"bytes": 100, "limiteEip170": 24576}, "abi": [
            {"type": "function", "name": name} for name in
            ("registerClaim", "submitInference", "resolve", "settle")],
        "codigo": {"registroHash": "0x" + "aa" * 32,
                   "registroEsperadoHash": "0x" + "aa" * 32,
                   "verificadorHash": "0x" + "bb" * 32,
                   "verificadorEsperadoHash": "0x" + "bb" * 32,
                   "registroBytes": 100},
        "nonceInicial": 0,
        "transacciones": {key: {"nonce": index, "hash": "0x" + f"{index + 1:064x}",
                               "recibo": {"hash": "0x" + f"{index + 1:064x}", "estado": 1}}
                         for index, key in enumerate(("verificador", "registro", "umbral", "confirmacionHumana"))},
    }
    config = {"chainId": 31337, "gasPriceOracle": "0x" + "44" * 20,
              "approvedLocalDeployment": {
                  "chainId": 31337, "wallet": "0x" + "11" * 20,
                  "registry": "0x" + "22" * 20, "verifier": "0x" + "33" * 20,
                  "transactions": [{"stage": stage, "nonce": index,
                                    "hash": "0x" + f"{index + 1:064x}", "status": 1}
                                   for index, stage in enumerate(STAGES)]}}
    deployment = tmp_path / "deployment.json"
    network = tmp_path / "network.json"
    deployment.write_text(json.dumps(artifact))
    network.write_text(json.dumps(config))
    args = ["--deployment", str(deployment), "--network-config", str(network),
            "--image", str(IMAGE), "--artifacts", str(ROOT / "artefactos"),
            "--state-dir", str(tmp_path / "state"), "--evidence-dir", str(tmp_path / "evidence")]
    return args, deployment, tmp_path


def invoke(args):
    env = os.environ.copy()
    env.update({"PFI165_UPSTREAM_RPC": "SECRET_UPSTREAM_SENTINEL",
                "CADENA_CLAVE_ORACULO": "SECRET_WALLET_SENTINEL"})
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=15)


def test_plan_is_read_only_and_secret_free(tmp_path):
    args, _, base = inputs(tmp_path)
    outcome = invoke(args)
    assert outcome.returncode == 0, outcome.stderr
    assert outcome.stderr == ""
    assert json.loads(outcome.stdout)["expectedTransactions"] == 6
    assert "SECRET_" not in outcome.stdout + outcome.stderr
    assert not (base / "state").exists() and not (base / "evidence").exists()


def test_invalid_image_hash_is_rejected_without_effects(tmp_path):
    args, _, base = inputs(tmp_path)
    changed = base / "changed.png"
    changed.write_bytes(IMAGE.read_bytes() + b"x")
    args[args.index("--image") + 1] = str(changed)
    outcome = invoke(args)
    assert outcome.returncode == 1
    assert outcome.stdout == ""
    assert outcome.stderr == "Preparación rechazada: imagen incompatible.\n"
    assert not (base / "state").exists() and not (base / "evidence").exists()


@pytest.mark.parametrize("alter", [
    lambda a: a.update(nonceInicial=1),
    lambda a: a["transacciones"]["umbral"].update(nonce=4),
    lambda a: a["transacciones"]["registro"]["recibo"].update(estado=0),
    lambda a: a.pop("codigo"),
    lambda a: a["codigo"].pop("registroEsperadoHash"),
    lambda a: a["codigo"].pop("verificadorEsperadoHash"),
    lambda a: a["codigo"].update(registroEsperadoHash="0x" + "cc" * 32),
    lambda a: a.pop("bytecodeDelVerificador"),
    lambda a: a["red"].pop("nombre"),
])
def test_invalid_deployment_anchor_is_rejected(tmp_path, alter):
    args, deployment, base = inputs(tmp_path)
    artifact = json.loads(deployment.read_text())
    alter(artifact)
    deployment.write_text(json.dumps(artifact))
    outcome = invoke(args)
    assert outcome.returncode == 1
    assert outcome.stdout == ""
    assert outcome.stderr == "Preparación rechazada: ancla de despliegue inválida.\n"
    assert not (base / "state").exists() and not (base / "evidence").exists()


@pytest.mark.parametrize("stage", STAGES)
def test_paired_replacement_cannot_change_approved_local_anchor(tmp_path, stage):
    args, deployment, base = inputs(tmp_path)
    artifact = json.loads(deployment.read_text())
    altered = "0x" + "ff" * 32
    artifact["transacciones"][stage]["hash"] = altered
    artifact["transacciones"][stage]["recibo"]["hash"] = altered
    deployment.write_text(json.dumps(artifact))
    for command in (args, args + ["--execute"]):
        outcome = invoke(command)
        assert (outcome.returncode, outcome.stdout, outcome.stderr) == (
            1, "", "Preparación rechazada: ancla de despliegue inválida.\n"
        )
        assert not (base / "state").exists() and not (base / "evidence").exists()


@pytest.mark.parametrize("stage", STAGES)
def test_paired_replacement_cannot_change_bundled_base_anchor(tmp_path, stage):
    args, deployment, base = inputs(tmp_path)
    artifact = json.loads(PUBLIC_DEPLOYMENT.read_text())
    altered = "0x" + "ff" * 32
    artifact["transacciones"][stage]["hash"] = altered
    artifact["transacciones"][stage]["recibo"]["hash"] = altered
    deployment.write_text(json.dumps(artifact))
    args[args.index("--network-config") + 1] = str(BASE_NETWORK)
    outcome = invoke(args)
    assert (outcome.returncode, outcome.stdout, outcome.stderr) == (
        1, "", "Preparación rechazada: ancla de despliegue inválida.\n"
    )
    assert not (base / "state").exists() and not (base / "evidence").exists()


def test_base_network_cannot_override_approved_identity(tmp_path):
    args, deployment, base = inputs(tmp_path)
    artifact = json.loads(PUBLIC_DEPLOYMENT.read_text())
    artifact["transacciones"]["umbral"]["hash"] = "0x" + "ff" * 32
    artifact["transacciones"]["umbral"]["recibo"]["hash"] = "0x" + "ff" * 32
    deployment.write_text(json.dumps(artifact))
    config = json.loads(BASE_NETWORK.read_text())
    config["approvedLocalDeployment"] = {
        "chainId": config["chainId"], "wallet": artifact["oraculo"],
        "registry": artifact["registro"], "verifier": artifact["verificador"],
        "transactions": [{"stage": key, "nonce": i,
                          "hash": artifact["transacciones"][key]["hash"], "status": 1}
                         for i, key in enumerate(STAGES)]}
    network = base / "spoofed-network.json"
    network.write_text(json.dumps(config))
    args[args.index("--network-config") + 1] = str(network)
    outcome = invoke(args)
    assert (outcome.returncode, outcome.stdout, outcome.stderr) == (
        1, "", "Preparación rechazada: ancla de despliegue inválida.\n"
    )
    assert not (base / "state").exists() and not (base / "evidence").exists()


@pytest.mark.parametrize("field", ("oraculo", "registro", "verificador"))
def test_bundled_base_refuses_replaced_identity(tmp_path, field):
    args, deployment, base = inputs(tmp_path)
    artifact = json.loads(PUBLIC_DEPLOYMENT.read_text())
    artifact[field] = "0x" + "ff" * 20
    deployment.write_text(json.dumps(artifact))
    args[args.index("--network-config") + 1] = str(BASE_NETWORK)
    outcome = invoke(args)
    assert (outcome.returncode, outcome.stdout, outcome.stderr) == (
        1, "", "Preparación rechazada: ancla de despliegue inválida.\n"
    )
    assert not (base / "state").exists() and not (base / "evidence").exists()


def test_local_approval_required_and_unsupported_chain_rejected(tmp_path):
    args, deployment, base = inputs(tmp_path)
    network = base / "network.json"
    config = json.loads(network.read_text())
    config.pop("approvedLocalDeployment")
    network.write_text(json.dumps(config))
    outcome = invoke(args)
    assert (outcome.returncode, outcome.stdout, outcome.stderr) == (
        1, "", "Preparación rechazada: ancla de despliegue inválida.\n"
    )
    assert not (base / "state").exists() and not (base / "evidence").exists()
    config["chainId"] += 1
    config["approvedLocalDeployment"] = {"chainId": config["chainId"]}
    network.write_text(json.dumps(config))
    artifact = json.loads(deployment.read_text())
    artifact["red"]["chainId"] = config["chainId"]
    deployment.write_text(json.dumps(artifact))
    outcome = invoke(args)
    assert (outcome.returncode, outcome.stdout, outcome.stderr) == (
        1, "", "Preparación rechazada: ancla de despliegue inválida.\n"
    )
    assert not (base / "state").exists() and not (base / "evidence").exists()


def test_approved_base_plan_has_no_effects(tmp_path):
    args, _, base = inputs(tmp_path)
    args[args.index("--deployment") + 1] = str(PUBLIC_DEPLOYMENT)
    args[args.index("--network-config") + 1] = str(BASE_NETWORK)
    outcome = invoke(args)
    assert outcome.returncode == 0 and outcome.stderr == ""
    assert json.loads(outcome.stdout)["initialNonce"] == 4
    assert not (base / "state").exists() and not (base / "evidence").exists()


def test_missing_artifact_rejected_without_effects(tmp_path):
    args, _, base = inputs(tmp_path)
    artifacts = base / "incomplete-artifacts"
    artifacts.mkdir()
    args[args.index("--artifacts") + 1] = str(artifacts)
    outcome = invoke(args)
    assert outcome.returncode == 1 and outcome.stdout == ""
    assert outcome.stderr == "Preparación rechazada: artefactos incompatibles.\n"
    assert not (base / "state").exists() and not (base / "evidence").exists()


def test_mismatched_network_rejected_without_effects(tmp_path):
    args, _, base = inputs(tmp_path)
    config = base / "network.json"
    network = json.loads(config.read_text())
    network["chainId"] += 1
    config.write_text(json.dumps(network))
    outcome = invoke(args)
    assert outcome.returncode == 1 and outcome.stdout == ""
    assert outcome.stderr == "Preparación rechazada: ancla de despliegue inválida.\n"
    assert not (base / "state").exists() and not (base / "evidence").exists()


def test_world_readable_parent_rejected_without_effects(tmp_path):
    args, _, base = inputs(tmp_path)
    public = base / "public"
    public.mkdir(mode=0o755)
    args[args.index("--state-dir") + 1] = str(public / "state")
    outcome = invoke(args)
    assert outcome.returncode == 1 and outcome.stdout == ""
    assert outcome.stderr == "Preparación rechazada: directorio de corrida no privado.\n"
    assert not (public / "state").exists() and not (base / "evidence").exists()


def test_existing_run_never_resets_ledger(tmp_path):
    args, _, base = inputs(tmp_path)
    state = base / "state"
    state.mkdir(mode=0o700)
    ledger = state / "ledger.json"
    ledger.write_text('{"attempts":[]}')
    outcome = invoke(args + ["--execute"])
    assert outcome.returncode == 1
    assert outcome.stdout == ""
    assert outcome.stderr == "Preparación rechazada: estado de corrida existente.\n"
    assert ledger.read_text() == '{"attempts":[]}'
    assert not (base / "evidence").exists()


def test_execute_without_isolated_services_is_rejected_without_effects(tmp_path):
    args, _, base = inputs(tmp_path)
    outcome = invoke(args + ["--execute"])
    assert outcome.returncode == 1
    assert outcome.stdout == ""
    assert outcome.stderr == "Preparación rechazada: entorno aislado inválido.\n"
    assert not (base / "state").exists() and not (base / "evidence").exists()


def test_help_has_public_flags_and_no_side_effects(tmp_path):
    outcome = invoke(["--help"])
    assert outcome.returncode == 0 and outcome.stderr == ""
    for flag in ("--deployment", "--network-config", "--image", "--artifacts",
                 "--state-dir", "--evidence-dir", "--execute"):
        assert flag in outcome.stdout
    assert not list(tmp_path.iterdir())


def assert_compiled_runtime_identity(actual, expected_hex):
    assert bytes(actual) == bytes.fromhex(expected_hex.removeprefix("0x"))


def test_runtime_identity_accepts_prefixed_compiler_bytecode():
    assert_compiled_runtime_identity(b"\x60\x00", "0x6000")


def test_runtime_identity_rejects_different_bytecode():
    with pytest.raises(AssertionError):
        assert_compiled_runtime_identity(b"\x60\x01", "0x6000")


def test_actual_isolated_lifecycle():
    if os.environ.get("PFI165_LOCAL_INTEGRATION") != "1":
        pytest.skip("Requiere opt-in explícito de servicios locales desechables")
    import shutil
    import signal
    from web3 import Web3

    contracts = Path(__file__).resolve().parents[2] / "contracts"
    scratch = Path(tempfile.mkdtemp(prefix="pfi165-integration-"))
    deployment_dir = scratch / "contracts"
    deployment_dir.mkdir(mode=0o700)
    (deployment_dir / "scripts").mkdir()
    shutil.copyfile(contracts / "hardhat.config.js", deployment_dir / "hardhat.config.js")
    config = deployment_dir / "hardhat.config.js"
    config.write_text(config.read_text() +
                      "\nmodule.exports.networks = {localhost: {url: process.env.HARDHAT_LOCALHOST_URL}};\n")
    shutil.copyfile(contracts / "scripts/deploy-local.js", deployment_dir / "scripts/deploy-local.js")
    (deployment_dir / "artifacts").symlink_to(contracts / "artifacts", target_is_directory=True)
    (deployment_dir / "node_modules").symlink_to(contracts / "node_modules", target_is_directory=True)
    ports = []
    containers = []
    node = None
    wrapper = None
    t = None
    env = {"PATH": os.environ["PATH"], "HOME": str(scratch),
           "HARDHAT_DISABLE_TELEMETRY_PROMPT": "true"}

    def docker(*args):
        return subprocess.run(["docker", *args], text=True, capture_output=True,
                              check=True, timeout=90).stdout.strip()

    def port():
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            value = sock.getsockname()[1]
            ports.append(value)
            return value

    try:
        label = "pfi165-" + uuid.uuid4().hex[:12]
        pg_name = "pfi165_" + uuid.uuid4().hex[:12]
        pg_id = docker("run", "-d", "--pull=never", "--name", label + "-pg",
                       "--label", "pfi165-owned=" + label, "--tmpfs", "/var/lib/postgresql/data:rw",
                       "-e", "POSTGRES_PASSWORD=local_public_test_only",
                       "-e", "POSTGRES_DB=" + pg_name,
                       "-p", "127.0.0.1::5432", "postgres:17.6-bookworm")
        containers.append(pg_id)
        redis_id = docker("run", "-d", "--pull=never", "--name", label + "-redis",
                          "--label", "pfi165-owned=" + label, "--tmpfs", "/data:rw",
                          "-p", "127.0.0.1::6379", "redis:7-alpine")
        containers.append(redis_id)
        pg_port = int(docker("port", pg_id, "5432/tcp").rsplit(":", 1)[1])
        redis_port = int(docker("port", redis_id, "6379/tcp").rsplit(":", 1)[1])
        ports.extend((pg_port, redis_port))
        chain_port = port()
        env["HARDHAT_LOCALHOST_URL"] = f"http://127.0.0.1:{chain_port}"
        node_log = (scratch / "hardhat.log").open("wb")
        try:
            node = subprocess.Popen(["node", "node_modules/hardhat/internal/cli/cli.js",
                                     "node", "--hostname", "127.0.0.1", "--port", str(chain_port)],
                                    cwd=deployment_dir, env=env, stdout=node_log, stderr=node_log,
                                    start_new_session=True)
        finally:
            node_log.close()
        url = f"http://127.0.0.1:{chain_port}"
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 5}))
        for _ in range(100):
            if node.poll() is not None:
                pytest.fail("Hardhat local stopped; see private log " + str(scratch))
            try:
                if w3.eth.chain_id == 31337:
                    break
            except Exception:
                time.sleep(.2)
        else:
            pytest.fail("Hardhat local not ready")
        # Solo a partir de aquí el nodo confirmado puede recibir el setup local.
        deploy_log = (scratch / "deploy.log").open("wb")
        try:
            deployed = subprocess.run(["node", "node_modules/hardhat/internal/cli/cli.js",
                                       "run", "scripts/deploy-local.js", "--network", "localhost",
                                       "--no-compile"], cwd=deployment_dir,
                                      env={**env, "HARDHAT_NETWORK": "localhost",
                                           "HARDHAT_LOCALHOST_URL": url},
                                      stdout=deploy_log, stderr=deploy_log, timeout=120)
        finally:
            deploy_log.close()
        assert deployed.returncode == 0, str(scratch / "deploy.log")
        artifact = json.loads((deployment_dir / "despliegue-local.json").read_text())
        assert w3.eth.chain_id == 31337
        assert w3.eth.get_transaction_count(artifact["oraculo"]) == 4
        # El ancla local procede del nodo propio, antes de construir el JSON del runner.
        local_transactions = []
        deployed_addresses = {}
        local_sender = None
        for index, name in enumerate(STAGES):
            tx_hash = w3.eth.get_block(index + 1)["transactions"][0].hex()
            tx_hash = tx_hash if tx_hash.startswith("0x") else "0x" + tx_hash
            transaction = w3.eth.get_transaction(tx_hash)
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            assert transaction["nonce"] == index and receipt["status"] == 1
            if local_sender is None:
                local_sender = transaction["from"]
            assert transaction["from"].lower() == local_sender.lower()
            if name in ("verificador", "registro"):
                deployed_addresses[name] = receipt["contractAddress"]
            local_transactions.append({"stage": name, "nonce": transaction["nonce"],
                                       "hash": tx_hash, "status": receipt["status"]})
            artifact.setdefault("transacciones", {})[name] = {
                "nonce": index, "hash": tx_hash,
                "recibo": {"hash": tx_hash, "estado": receipt["status"]}}
        approved_local = {"chainId": w3.eth.chain_id, "wallet": local_sender,
                          "registry": deployed_addresses["registro"],
                          "verifier": deployed_addresses["verificador"],
                          "transactions": local_transactions}
        artifact["nonceInicial"] = 0
        verifier_artifact = json.loads((contracts / "artifacts/contracts/verifier/Halo2Verifier.sol/Halo2Verifier.json").read_text())
        registry_path = contracts / "artifacts/contracts/ClaimRegistry.sol/ClaimRegistry.json"
        registry_artifact = json.loads(registry_path.read_text())
        debug_path = registry_path.with_suffix(".dbg.json")
        build_info = json.loads((debug_path.parent / json.loads(debug_path.read_text())["buildInfo"]).read_text())
        references = build_info["output"]["contracts"]["contracts/ClaimRegistry.sol"]["ClaimRegistry"]["evm"]["deployedBytecode"]["immutableReferences"]
        assert references and all(ref["length"] == 32 for refs in references.values() for ref in refs)
        expected_registry = registry_artifact["deployedBytecode"][2:]
        for refs in references.values():
            for ref in refs:
                start, length = ref["start"] * 2, ref["length"] * 2
                word = artifact["oraculo"].lower()[2:].rjust(length, "0")
                expected_registry = expected_registry[:start] + word + expected_registry[start + length:]
        expected_registry = "0x" + expected_registry
        expected_verifier = verifier_artifact["deployedBytecode"]
        verifier_code = w3.eth.get_code(artifact["verificador"])
        registry_code = w3.eth.get_code(artifact["registro"])
        assert_compiled_runtime_identity(verifier_code, expected_verifier)
        assert_compiled_runtime_identity(registry_code, expected_registry)
        artifact["bytecodeDelVerificador"] = {"bytes": len(verifier_code), "limiteEip170": 24576}
        artifact["codigo"] = {
            "verificadorHash": "0x" + Web3.keccak(verifier_code).hex().removeprefix("0x"),
            "verificadorEsperadoHash": "0x" + Web3.keccak(hexstr=expected_verifier).hex().removeprefix("0x"),
            "registroHash": "0x" + Web3.keccak(registry_code).hex().removeprefix("0x"),
            "registroEsperadoHash": "0x" + Web3.keccak(hexstr=expected_registry).hex().removeprefix("0x"),
            "registroBytes": len(registry_code)}
        deployment = scratch / "deployment.json"
        deployment.write_text(json.dumps(artifact))
        oracle = "0x0000000000000000000000000000000000001001"
        response = w3.provider.make_request("hardhat_setCode", [oracle, "0x600060005260206000f3"])
        assert response.get("result") is True
        response = w3.provider.make_request("hardhat_setNextBlockBaseFeePerGas", ["0x1"])
        assert response.get("result") is True
        network = scratch / "network.json"
        network.write_text(json.dumps({"chainId": 31337, "gasPriceOracle": oracle,
                                       "approvedLocalDeployment": approved_local}))
        from eth_utils import keccak
        from hexbytes import HexBytes
        sends = []
        ledger_path = scratch / "state/ledger.json"

        class Wrapper(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(length))
                method = request["method"]
                if method == "eth_maxPriorityFeePerGas":
                    response = {"jsonrpc": "2.0", "id": request["id"], "result": "0x1"}
                elif method == "eth_gasPrice":
                    response = {"jsonrpc": "2.0", "id": request["id"], "result": "0x2"}
                elif method == "eth_getTransactionReceipt":
                    response = w3.provider.make_request(method, request["params"])
                    if response.get("result") is not None:
                        response["result"]["l1Fee"] = "0x0"
                        response["result"]["operatorFee"] = "0x0"
                else:
                    if method == "eth_sendRawTransaction":
                        raw = request["params"][0]
                        tx_hash = "0x" + keccak(HexBytes(raw)).hex()
                        attempts = json.loads(ledger_path.read_text())["attempts"]
                        assert attempts[-1]["hash"] == tx_hash
                        sends.append(tx_hash)
                    response = w3.provider.make_request(method, request["params"])
                response["id"] = request["id"]
                data = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        wrapper = ThreadingHTTPServer(("127.0.0.1", 0), Wrapper)
        t = Thread(target=wrapper.serve_forever, daemon=True)
        t.start()
        wrapper_port = wrapper.server_port
        db = f"postgresql+psycopg://postgres:local_public_test_only@127.0.0.1:{pg_port}/{pg_name}"
        for _ in range(100):
            try:
                from sqlalchemy import create_engine
                probe = create_engine(db)
                try:
                    with probe.connect() as connection:
                        connection.exec_driver_sql("SELECT 1")
                finally:
                    probe.dispose()
                break
            except Exception:
                time.sleep(.2)
        else:
            pytest.fail("isolated DB not ready")
        migrated = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                                  cwd=ROOT, env={**env, "DATABASE_URL": db},
                                  capture_output=True, text=True, timeout=120)
        assert migrated.returncode == 0, migrated.stderr
        args = ["--deployment", str(deployment), "--network-config", str(network),
                "--image", str(IMAGE), "--artifacts", str(ROOT / "artefactos"),
                "--state-dir", str(scratch / "state"), "--evidence-dir", str(scratch / "evidence"),
                "--execute"]
        runtime_env = {**env, "DATABASE_URL": db, "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
                       "PFI165_UPSTREAM_RPC": f"http://127.0.0.1:{wrapper_port}",
                       "CADENA_CLAVE_ORACULO":
                           "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"}
        result = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, env=runtime_env,
                                capture_output=True, text=True, timeout=900)
        (scratch / "runner.out").write_text(result.stdout + "\n" + result.stderr)
        assert result.returncode == 0, f"local runner failed; private evidence {scratch}"
        evidence = json.loads((scratch / "evidence/result.json").read_text())
        assert len(sends) == 6
        assert sends == [item["hash"] for item in evidence["attempts"]]
        assert [item["nonce"] for item in evidence["attempts"]] == list(range(4, 10))
        assert evidence["recoveredStatusId"] == "Aprobado"
        assert all(.91 <= case["confidence"] <= .92 for case in evidence["cases"])
    finally:
        if wrapper is not None:
            wrapper.shutdown()
            wrapper.server_close()
        if t is not None:
            t.join(timeout=5)
        if node is not None and node.poll() is None:
            os.killpg(node.pid, signal.SIGTERM)
            try:
                node.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(node.pid, signal.SIGKILL)
                node.wait(timeout=5)
        for container in reversed(containers):
            docker("stop", "--time", "10", container)
        (scratch / "resources.json").write_text(json.dumps({"containers_stopped": containers,
                                                               "ports_closed": ports}))

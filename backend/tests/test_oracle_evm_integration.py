"""Isolated real-EVM regression probes for the durable oracle coordinator.

Opt in with PFI138_EVM_RPC, PFI138_EVM_CONTRACT and PFI138_EVM_KEY against
an isolated local node only. PostgreSQL fixtures create separate schemas.
"""

import hashlib
import json
import multiprocessing
import os
import socket
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.dominio.ports import ProofResult
from app.dominio.vocabulary import ClaimStatus, TransitionCause

from app.adaptadores.chain import Web3ChainClient
from app.adaptadores.database import create_database_engine, create_session_factory, session_scope
from app.adaptadores.models import Claim, OracleOperation, OracleWallet
from app.adaptadores.oracle_coordinator import OracleCoordinator, OracleOperationUncertainError
from app.dominio.claims import current_configuration
from app.dominio.vocabulary import OracleOperationStatus


@pytest.fixture
def evm_client():
    rpc = os.environ.get("PFI138_EVM_RPC")
    address = os.environ.get("PFI138_EVM_CONTRACT")
    key = os.environ.get("PFI138_EVM_KEY")
    artifact = os.environ.get("PFI138_EVM_ARTIFACT")
    if not all((rpc, address, key, artifact)):
        pytest.skip("Isolated local EVM configuration not supplied")
    abi = json.loads(Path(artifact).read_text())["abi"]
    client = Web3ChainClient(
        rpc_url=rpc, contract_address=address, oracle_private_key=key, abi=abi,
        timeout=5,
    )
    assert client.w3.eth.chain_id == 31337, "Never run local-key tests on testnet"
    return client


@pytest.fixture
def evm_mock_client(evm_client):
    """Deploy a fresh registry with an isolated controllable verifier."""
    w3 = evm_client.w3
    oracle = evm_client.cuenta.address
    registry_artifact = json.loads(Path(os.environ["PFI138_EVM_ARTIFACT"]).read_text())
    mock_artifact = json.loads((
        Path(__file__).resolve().parents[2]
        / "contracts/artifacts/contracts/mocks/MockVerifier.sol/MockVerifier.json"
    ).read_text())
    mock_deploy = w3.eth.contract(
        abi=mock_artifact["abi"], bytecode=mock_artifact["bytecode"]
    ).constructor(0, b"\x00").transact({"from": oracle})
    mock_address = w3.eth.wait_for_transaction_receipt(mock_deploy, timeout=5)["contractAddress"]
    registry_deploy = w3.eth.contract(
        abi=registry_artifact["abi"], bytecode=registry_artifact["bytecode"]
    ).constructor(oracle, mock_address).transact({"from": oracle})
    registry_address = w3.eth.wait_for_transaction_receipt(registry_deploy, timeout=5)["contractAddress"]
    mock = w3.eth.contract(address=mock_address, abi=mock_artifact["abi"])
    registry = w3.eth.contract(address=registry_address, abi=registry_artifact["abi"])
    w3.eth.wait_for_transaction_receipt(
        registry.functions.setConfidenceThreshold(9000).transact({"from": oracle}), timeout=5
    )
    return (
        Web3ChainClient(
            rpc_url=os.environ["PFI138_EVM_RPC"], contract_address=registry_address,
            oracle_private_key=os.environ["PFI138_EVM_KEY"], abi=registry_artifact["abi"],
            timeout=5,
        ),
        mock,
    )


def _api_deployment_artifact(client):
    """Construye metadatos de despliegue desde el contrato local confirmado."""
    contract = client.contrato
    verifier = contract.functions.verifier().call()
    compiled = json.loads(Path(os.environ["PFI138_EVM_ARTIFACT"]).read_text())
    return {
        "red": {"nombre": "hardhat", "chainId": client.w3.eth.chain_id},
        "oraculo": contract.functions.oracle().call(),
        "verificador": verifier,
        "registro": contract.address,
        "abi": compiled["abi"],
        "umbralDeDerivacionBps": contract.functions.confidenceThresholdBps().call(),
        "bytecodeDelVerificador": {
            "bytes": len(client.w3.eth.get_code(verifier)),
            "limiteEip170": 24576,
        },
    }


def _register_in_separate_process(
    db_url, rpc, contract, artifact, key, claim_id, number, config_id,
    started, reached_signature, release, result, pause,
):
    """Open entirely new database and RPC connections in a spawned process."""
    engine = None
    try:
        abi = json.loads(Path(artifact).read_text())["abi"]
        client = Web3ChainClient(
            rpc_url=rpc, contract_address=contract, oracle_private_key=key,
            abi=abi, timeout=5,
        )
        assert client.w3.eth.chain_id == 31337
        engine = create_database_engine(db_url)
        factory = create_session_factory(engine)

        def after_signing(_operation):
            reached_signature.set()
            if pause and not release.wait(12):
                raise RuntimeError("coordinator process was not released")

        started.set()
        receipt = OracleCoordinator(
            factory, client, before_broadcast_hook=after_signing,
        ).register_claim(claim_id, number, "QmIndependentProcess", config_id)
        result.put(("ok", receipt.status.value, receipt.transaction_hash))
    except Exception as exc:
        result.put(("error", type(exc).__name__))
    finally:
        if engine is not None:
            engine.dispose()


def test_independent_processes_share_one_wallet_nonce(
    sesiones, publicador, reclamo_registrado, evm_mock_client
):
    """Two OS processes cannot sign and broadcast concurrently for one wallet."""
    from app.dominio.claims import UploadedFile, register_claim

    client, _mock = evm_mock_client
    with session_scope(sesiones) as session:
        config_id = current_configuration(session).id
        first = session.get(Claim, reclamo_registrado["id"])
        first.tracking_number = f"SN-{uuid4().hex[:12]}"
        first_id, first_number = first.id, first.tracking_number
        second = register_claim(
            session, publicador, damage_description="El lateral quedó abollado.",
            files=[UploadedFile("lateral.png", "image/png", b"\x89PNG\r\n\x1a\nsecond claim")],
            declared_amount=Decimal("180000.00"),
        )
        second_id, second_number = second.claim.id, second.claim.tracking_number
    ctx = multiprocessing.get_context("spawn")
    entered_a, entered_b, reached_a, reached_b = (ctx.Event() for _ in range(4))
    release_a, release_b = ctx.Event(), ctx.Event()
    result_a, result_b = ctx.Queue(), ctx.Queue()
    db_url = sesiones.kw["bind"].url.render_as_string(hide_password=False)
    common = (
        db_url, os.environ["PFI138_EVM_RPC"], client.contrato.address,
        os.environ["PFI138_EVM_ARTIFACT"], os.environ["PFI138_EVM_KEY"],
    )
    a = ctx.Process(
        target=_register_in_separate_process,
        args=(*common, first_id, first_number, config_id,
              entered_a, reached_a, release_a, result_a, True),
    )
    b = ctx.Process(
        target=_register_in_separate_process,
        args=(*common, second_id, second_number, config_id,
              entered_b, reached_b, release_b, result_b, False),
    )
    try:
        a.start()
        assert entered_a.wait(8)
        assert reached_a.wait(8)
        b.start()
        assert entered_b.wait(8)
        assert not reached_b.wait(0.5), "another process signed while wallet A was locked"
        with session_scope(sesiones) as session:
            reserved = session.scalars(select(OracleWallet)).one()
            assert reserved.active_operation_id is not None
            signed = [op for op in session.scalars(select(OracleOperation)).all()
                      if op.raw_signed_transaction]
            assert len(signed) == 1
        release_a.set()
        a.join(timeout=12)
        b.join(timeout=12)
        assert not a.is_alive() and not b.is_alive()
        assert a.exitcode == b.exitcode == 0
        assert result_a.get(timeout=1)[0] == "ok"
        assert result_b.get(timeout=1)[0] == "ok"
        with session_scope(sesiones) as session:
            ops = session.scalars(select(OracleOperation)).all()
            assert len(ops) == 2
            assert all(op.status == OracleOperationStatus.CONFIRMADA.value for op in ops)
            nonces = sorted(op.nonce for op in ops)
            assert nonces[1] == nonces[0] + 1
            assert ops[0].transaction_hash != ops[1].transaction_hash
            assert session.scalars(select(OracleWallet)).one().active_operation_id is None
            assert all(session.get(Claim, claim_id).onchain_claim_id is not None
                       for claim_id in (first_id, second_id))
    finally:
        release_a.set()
        release_b.set()
        for process in (a, b):
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)


class _WorkerEvidence:
    def __init__(self, cid, content):
        self.cid, self.content = cid, content

    def retrieve(self, cid):
        if cid != self.cid:
            raise ValueError("Unexpected evidence identifier")
        return self.content


class _WorkerProof:
    def artifacts_identity(self):
        return "isolated-evm-proof:v1"

    def prove(self, inference):
        raw = b'{"hex_proof":"0x1234","instances":[]}'
        return ProofResult(
            proof=raw, public_inputs=b"", proof_hash="0x" + hashlib.sha256(raw).hexdigest(),
            circuit_parameters={"simulated": True}, declared_visibility={}, verified_locally=True,
        )


def _run_worker_in_separate_process(
    db_url, rpc, contract, artifact, key, cid, content, entered, release, result,
):
    """Execute an actual queue job in a new process with separate adapters."""
    from app.adaptadores.inference import StubInferenceEngine
    from app.dominio import worker

    engine = None
    try:
        abi = json.loads(Path(artifact).read_text())["abi"]
        chain = Web3ChainClient(
            rpc_url=rpc, contract_address=contract, oracle_private_key=key,
            abi=abi, timeout=5,
        )
        assert chain.w3.eth.chain_id == 31337
        engine = create_database_engine(db_url)
        factory = create_session_factory(engine)

        def pause_before_send(_operation):
            entered.set()
            if not release.wait(15):
                raise RuntimeError("Worker wallet gate was not released")

        coordinator = OracleCoordinator(factory, chain, before_broadcast_hook=pause_before_send)
        processed = worker.run_next(
            factory, publisher=_WorkerEvidence(cid, content),
            engine=StubInferenceEngine(), prover=_WorkerProof(), chain=coordinator,
        )
        result.put(("ok", processed.job_id if processed else None))
    except Exception as exc:
        result.put(("error", type(exc).__name__))
    finally:
        if engine is not None:
            engine.dispose()


def test_api_operator_competes_with_real_worker_process(
    sesiones, publicador, reclamo_registrado, evm_mock_client, tmp_path
):
    """The API refuses a human send while a separate worker owns the wallet."""
    from app.dominio.claims import UploadedFile, register_claim

    chain, _mock = evm_mock_client
    image = b"\x89PNG\r\n\x1a\ncontenido de prueba"
    with session_scope(sesiones) as session:
        worker_claim = session.get(Claim, reclamo_registrado["id"])
        worker_claim.tracking_number = f"SN-{uuid4().hex[:12]}"
        cid = worker_claim.evidence[0].cid
        operator_claim = register_claim(
            session, publicador, damage_description="El paragolpes quedó dañado.",
            files=[UploadedFile("frente.png", "image/png", image + b"operator")],
            declared_amount=Decimal("180000.00"),
        )
        operator_id, operator_number = operator_claim.claim.id, operator_claim.claim.tracking_number
        configuration_id = current_configuration(session).id
    coordinator = OracleCoordinator(sesiones, chain)
    coordinator.register_claim(operator_id, operator_number, "QmOperatorClaim", configuration_id)
    assert coordinator.submit_inference(
        operator_id, operator_number, _proof(), confidence=0.96,
        amount_above_threshold=False, rules_consistent=True,
        configuration_id=configuration_id,
    ).status == ClaimStatus.APROBABLE

    deployment = tmp_path / "isolated-api-worker.json"
    deployment.write_text(json.dumps(_api_deployment_artifact(chain)))
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": sesiones.kw["bind"].url.render_as_string(hide_password=False),
        "CLIENTE_CADENA": "web3", "CADENA_RPC": os.environ["PFI138_EVM_RPC"],
        "CADENA_CLAVE_ORACULO": os.environ["PFI138_EVM_KEY"],
        "CADENA_DESPLIEGUE": str(deployment), "PUBLICADOR_EVIDENCIA": "local",
        "RUTA_EVIDENCIA_LOCAL": str(tmp_path / "evidence"),
    })
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log = (tmp_path / "api-worker.log").open("w")
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port)],
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    ctx = multiprocessing.get_context("spawn")
    entered, release = ctx.Event(), ctx.Event()
    result = ctx.Queue()
    child = ctx.Process(
        target=_run_worker_in_separate_process,
        args=(env["DATABASE_URL"], env["CADENA_RPC"], chain.contrato.address,
              os.environ["PFI138_EVM_ARTIFACT"], env["CADENA_CLAVE_ORACULO"],
              cid, image, entered, release, result),
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if api.poll() is not None:
                raise AssertionError("Isolated API exited before readiness")
            try:
                if httpx.get(base + "/health", timeout=0.5).status_code == 200:
                    break
            except httpx.RequestError:
                pass
            time.sleep(0.1)
        else:
            raise AssertionError("Isolated API did not become ready")

        child.start()
        assert entered.wait(10), "worker never reached the signed-wallet gate"
        url = base + f"/reclamos/{operator_number}/liquidacion"
        refused = httpx.post(url, headers={"Authorization": "Bearer operador-op-02"}, timeout=5)
        assert refused.status_code == 409
        assert httpx.get(base + "/cola", headers={
            "Authorization": "Bearer operador-op-02"
        }).json()["oracleBlocked"] is True
        release.set()
        child.join(timeout=18)
        assert not child.is_alive() and child.exitcode == 0
        assert result.get(timeout=1)[0] == "ok"
        accepted = httpx.post(url, headers={"Authorization": "Bearer operador-op-02"}, timeout=10)
        assert accepted.status_code == 200
        assert accepted.json()["statusId"] == ClaimStatus.APROBADO.value
        with session_scope(sesiones) as session:
            assert session.get(Claim, operator_id).status == ClaimStatus.APROBADO
            assert session.scalars(select(OracleWallet)).one().active_operation_id is None
            nonces = [op.nonce for op in session.scalars(select(OracleOperation)).all()]
            assert len(nonces) == len(set(nonces))
    finally:
        release.set()
        if child.is_alive():
            child.terminate()
            child.join(timeout=3)
        api.terminate()
        try:
            api.wait(timeout=5)
        except subprocess.TimeoutExpired:
            api.kill()
            api.wait(timeout=5)
        log.close()


def test_real_http_operator_reconciles_mined_gap_and_restart(
    sesiones, publicador, reclamo_registrado, evm_mock_client, tmp_path
):
    """A separate API process exposes safe status and recovers a mined gap on restart."""
    client, _mock = evm_mock_client
    deployment = tmp_path / "isolated-local-deployment.json"
    deployment.write_text(json.dumps(_api_deployment_artifact(client)))
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": sesiones.kw["bind"].url.render_as_string(hide_password=False),
        "CLIENTE_CADENA": "web3", "CADENA_RPC": os.environ["PFI138_EVM_RPC"],
        "CADENA_CLAVE_ORACULO": os.environ["PFI138_EVM_KEY"],
        "CADENA_DESPLIEGUE": str(deployment),
        "PUBLICADOR_EVIDENCIA": "local",
        "RUTA_EVIDENCIA_LOCAL": str(tmp_path / "evidence"),
    })
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    operator = {"Authorization": "Bearer operador-op-02"}
    reviewer = {"Authorization": "Bearer revisor-rev-04"}
    log = (tmp_path / "api.log").open("w")
    server = None

    def stop_server():
        nonlocal server
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            server = None

    def start_server():
        nonlocal server
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port)],
            env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise AssertionError("isolated API exited before readiness")
            try:
                response = httpx.get(base + "/health", timeout=0.5)
                if response.status_code == 200:
                    assert response.json() == {"status": "ok"}
                    return
            except httpx.RequestError:
                pass
            time.sleep(0.1)
        raise AssertionError("isolated API did not become ready")

    try:
        with session_scope(sesiones) as session:
            config_id = current_configuration(session).id
            number = f"SN-{uuid4().hex[:12]}"
            session.get(Claim, reclamo_registrado["id"]).tracking_number = number
        start_server()

        def interrupt_after_mining(_operation, _receipt):
            raise RuntimeError("injected crash before domain commit")

        with pytest.raises(RuntimeError, match="injected crash before domain commit"):
            OracleCoordinator(
                sesiones, client, before_confirm_hook=interrupt_after_mining
            ).register_claim(reclamo_registrado["id"], number, "QmHttpSmoke", config_id)
        with session_scope(sesiones) as session:
            operation_id = str(session.scalars(select(OracleOperation)).one().id)
        blocked = httpx.get(base + "/cola", headers=operator)
        assert blocked.status_code == 200
        assert blocked.json()["oracleBlocked"] is True
        assert blocked.json()["oracleOperations"][0]["operationId"] == operation_id
        assert httpx.get(base + "/cola").status_code == 401
        assert httpx.get(base + "/cola", headers=reviewer).status_code == 403
        url = base + "/cola/reconciliacion"
        assert httpx.post(url, json={"operationId": operation_id}).status_code == 401
        assert httpx.post(url, headers=reviewer, json={"operationId": operation_id}).status_code == 403
        assert httpx.post(url, headers=operator, json={"operationId": str(uuid4())}).status_code == 404
        assert httpx.post(url, headers=operator, json={"operationId": operation_id, "nonce": 1}).status_code == 422
        assert httpx.get(base + "/cola", headers=operator).json()["oracleBlocked"] is True
        response = httpx.post(url, headers=operator, json={"operationId": operation_id})
        assert response.status_code == 200
        assert response.json()["status"] == "confirmada"
        assert set(response.json()) == {
            "operationId", "number", "operation", "status", "transactionHash",
            "blockNumber", "lastErrorCode",
        }
        assert httpx.get(base + "/cola", headers=operator).json()["oracleOperations"] == []
        with session_scope(sesiones) as session:
            assert session.get(Claim, reclamo_registrado["id"]).onchain_claim_id is not None
            assert session.scalars(select(OracleWallet)).one().active_operation_id is None

        stop_server()
        from app.dominio.claims import UploadedFile, register_claim
        with session_scope(sesiones) as session:
            next_claim = register_claim(
                session, publicador, damage_description="El otro lateral quedó abollado.",
                files=[UploadedFile("lateral.png", "image/png",
                                    b"\x89PNG\r\n\x1a\nanother restart claim")],
                declared_amount=Decimal("175000.00"),
            )
            next_id, next_number = next_claim.claim.id, next_claim.claim.tracking_number
        with pytest.raises(RuntimeError, match="injected crash before domain commit"):
            OracleCoordinator(
                sesiones, client, before_confirm_hook=interrupt_after_mining
            ).register_claim(next_id, next_number, "QmRestartRecovery", config_id)
        with session_scope(sesiones) as session:
            next_op = session.scalars(select(OracleOperation).where(
                OracleOperation.claim_id == next_id
            )).one()
            assert next_op.status == OracleOperationStatus.ENVIADA.value
            assert session.scalars(select(OracleWallet)).one().active_operation_id == next_op.id
        # Startup must answer health while its one-shot thread applies the mined receipt.
        start_server()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            queue = httpx.get(base + "/cola", headers=operator, timeout=1)
            if queue.status_code == 200 and not queue.json()["oracleBlocked"]:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("API restart did not reconcile mined operation")
        assert queue.json()["oracleOperations"] == []
        assert httpx.get(base + f"/reclamos/{number}", headers=reviewer).status_code == 200
        with session_scope(sesiones) as session:
            assert session.get(Claim, next_id).onchain_claim_id is not None
            assert session.scalars(select(OracleWallet)).one().active_operation_id is None
    finally:
        stop_server()
        log.close()


def _proof() -> ProofResult:
    return ProofResult(
        proof=b'{"hex_proof":"0x1234","instances":[]}', public_inputs=b"",
        proof_hash="0x" + "a" * 64, circuit_parameters={},
        declared_visibility={}, verified_locally=True,
    )


def test_invalid_proof_is_confirmed_failure_on_real_evm(
    sesiones, reclamo_registrado, evm_mock_client
):
    """A real mined VerificationFailed event is not a reverted receipt."""
    client, mock = evm_mock_client
    with session_scope(sesiones) as session:
        number = f"SN-{uuid4().hex[:12]}"
        session.get(Claim, reclamo_registrado["id"]).tracking_number = number
        config_id = current_configuration(session).id
    coordinator = OracleCoordinator(sesiones, client)
    coordinator.register_claim(reclamo_registrado["id"], number, "QmInvalidProof", config_id)
    client.w3.eth.wait_for_transaction_receipt(
        mock.functions.setMode(1).transact({"from": client.cuenta.address}), timeout=5
    )
    receipt = coordinator.submit_inference(
        reclamo_registrado["id"], number, _proof(), confidence=0.96,
        amount_above_threshold=False, rules_consistent=True,
        configuration_id=config_id,
    )
    assert receipt.status == ClaimStatus.VERIFICACION_FALLIDA
    assert receipt.cause == TransitionCause.FALLO_DE_VERIFICACION
    assert receipt.transaction_hash is not None
    with session_scope(sesiones) as session:
        claim = session.get(Claim, reclamo_registrado["id"])
        assert claim.status == ClaimStatus.VERIFICACION_FALLIDA
        assert session.scalars(select(OracleWallet)).one().active_operation_id is None
        assert [op.status for op in session.scalars(select(OracleOperation)).all()] == [
            OracleOperationStatus.CONFIRMADA.value,
            OracleOperationStatus.CONFIRMADA.value,
        ]


@pytest.mark.parametrize("automatic", [True, False])
def test_real_evm_inference_and_settlement_both_origins(
    sesiones, reclamo_registrado, evm_mock_client, automatic
):
    """Both settlement origins share the same durable effect identity."""
    client, _mock = evm_mock_client
    with session_scope(sesiones) as session:
        number = f"SN-{uuid4().hex[:12]}"
        session.get(Claim, reclamo_registrado["id"]).tracking_number = number
        config_id = current_configuration(session).id
    coordinator = OracleCoordinator(sesiones, client)
    coordinator.register_claim(reclamo_registrado["id"], number, "QmSettlement", config_id)
    classified = coordinator.submit_inference(
        reclamo_registrado["id"], number, _proof(), confidence=0.96,
        amount_above_threshold=False, rules_consistent=True, configuration_id=config_id,
    )
    assert classified.status == ClaimStatus.APROBABLE
    settled = coordinator.settle(
        reclamo_registrado["id"], number, automatic=automatic, configuration_id=config_id,
    )
    assert settled.status == ClaimStatus.APROBADO
    assert settled.cause == (
        TransitionCause.LIQUIDACION_AUTOMATICA if automatic
        else TransitionCause.LIQUIDACION_DEL_OPERADOR
    )
    with session_scope(sesiones) as session:
        assert session.get(Claim, reclamo_registrado["id"]).status == ClaimStatus.APROBADO
        assert session.scalars(select(OracleWallet)).one().active_operation_id is None
        assert len(session.scalars(select(OracleOperation)).all()) == 3


def test_real_evm_human_resolution_preserves_decision(
    sesiones, reclamo_registrado, evm_mock_client
):
    """ReviewRequired resolves through the real signed oracle path."""
    from app.dominio.claims import record_resolution
    from app.adaptadores.models import Review
    client, _mock = evm_mock_client
    with session_scope(sesiones) as session:
        number = f"SN-{uuid4().hex[:12]}"
        session.get(Claim, reclamo_registrado["id"]).tracking_number = number
        config_id = current_configuration(session).id
    coordinator = OracleCoordinator(sesiones, client)
    coordinator.register_claim(reclamo_registrado["id"], number, "QmHumanResolution", config_id)
    classified = coordinator.submit_inference(
        reclamo_registrado["id"], number, _proof(), confidence=0.80,
        amount_above_threshold=False, rules_consistent=True, configuration_id=config_id,
    )
    assert classified.status == ClaimStatus.REQUIERE_REVISION
    with session_scope(sesiones) as session:
        transition = record_resolution(
            session, number, favorable=False, justification="Decision with evidence",
            reviewer_identity="reviewer-test", chain=client,
        )
        assert transition.to_status == ClaimStatus.RECHAZADO
    with session_scope(sesiones) as session:
        review = session.scalars(select(Review)).one()
        assert review.transition_id is not None
        assert session.get(Claim, reclamo_registrado["id"]).status == ClaimStatus.RECHAZADO
        assert session.scalars(select(OracleWallet)).one().active_operation_id is None


def test_rpc_exception_never_exposes_provider_details(
    sesiones, reclamo_registrado, evm_client, monkeypatch
):
    """An uncertain broadcast is visible without returning raw RPC diagnostics."""
    with session_scope(sesiones) as session:
        configuration_id = current_configuration(session).id
        number = f"SN-{uuid4().hex[:12]}"
        session.get(Claim, reclamo_registrado["id"]).tracking_number = number

    def unavailable(_raw):
        raise RuntimeError("private-rpc-credential-marker")

    monkeypatch.setattr(evm_client.w3.eth, "send_raw_transaction", unavailable)
    with pytest.raises(OracleOperationUncertainError) as caught:
        OracleCoordinator(sesiones, evm_client).register_claim(
            reclamo_registrado["id"], number, "QmRpcUnavailable", configuration_id,
        )
    assert "private-rpc-credential-marker" not in str(caught.value)
    assert caught.value.last_error_code == "BROADCAST_FAILED"
    with session_scope(sesiones) as session:
        operation = session.scalars(select(OracleOperation)).one()
        assert operation.status == OracleOperationStatus.INCIERTA.value
        assert operation.last_error_code == "BROADCAST_FAILED"
        assert session.scalars(select(OracleWallet)).one().active_operation_id == operation.id
        assert session.get(Claim, reclamo_registrado["id"]).onchain_claim_id is None


def test_real_registration_receipt_confirms_domain_and_releases_wallet(
    sesiones, reclamo_registrado, evm_client
):
    """A matching real-EVM receipt applies registration to the domain once."""
    with session_scope(sesiones) as session:
        configuration_id = current_configuration(session).id
        number = f"SN-{uuid4().hex[:12]}"
        session.get(Claim, reclamo_registrado["id"]).tracking_number = number

    receipt = OracleCoordinator(sesiones, evm_client).register_claim(
        reclamo_registrado["id"], number,
        "QmTestEvidenceForIsolatedOracle", configuration_id,
    )
    assert receipt.status.value == "Registrado"
    assert receipt.transaction_hash is not None
    assert receipt.block_number is not None
    with session_scope(sesiones) as session:
        operation = session.scalars(select(OracleOperation)).one()
        assert operation.status == OracleOperationStatus.CONFIRMADA.value
        assert operation.transaction_hash == receipt.transaction_hash
        assert session.scalars(select(OracleWallet)).one().active_operation_id is None
        assert session.get(Claim, reclamo_registrado["id"]).onchain_claim_id is not None


@pytest.mark.parametrize("interruption", ["before_broadcast", "after_broadcast", "before_confirm"])
def test_interrupted_real_evm_registration_resumes_exact_signed_effect(
    sesiones, reclamo_registrado, evm_mock_client, interruption
):
    """Signed intent survives pre-send, lost control, and mined-before-commit crashes."""
    client, _mock = evm_mock_client
    with session_scope(sesiones) as session:
        config_id = current_configuration(session).id
        number = f"SN-{uuid4().hex[:12]}"
        session.get(Claim, reclamo_registrado["id"]).tracking_number = number

    def interrupt(*_args):
        raise RuntimeError("injected process interruption")

    hooks = {
        "before_broadcast_hook": interrupt if interruption == "before_broadcast" else None,
        "after_broadcast_hook": interrupt if interruption == "after_broadcast" else None,
        "before_confirm_hook": interrupt if interruption == "before_confirm" else None,
    }
    with pytest.raises(RuntimeError, match="injected process interruption"):
        OracleCoordinator(sesiones, client, **hooks).register_claim(
            reclamo_registrado["id"], number, "QmInterrupted", config_id,
        )
    with session_scope(sesiones) as session:
        op = session.scalars(select(OracleOperation)).one()
        operation_id = op.id
        signed_bytes, tx_hash, nonce = op.raw_signed_transaction, op.transaction_hash, op.nonce
        assert signed_bytes and tx_hash and nonce is not None
        assert op.status == (
            OracleOperationStatus.FIRMADA.value if interruption == "before_broadcast"
            else OracleOperationStatus.ENVIADA.value
        )
        assert session.scalars(select(OracleWallet)).one().active_operation_id == operation_id
        assert session.get(Claim, reclamo_registrado["id"]).onchain_claim_id is None

    reconciled = OracleCoordinator(sesiones, client).reconcile_operation(operation_id)
    assert reconciled.status == OracleOperationStatus.CONFIRMADA.value
    with session_scope(sesiones) as session:
        op = session.get(OracleOperation, operation_id)
        assert (op.raw_signed_transaction, op.transaction_hash, op.nonce) == (
            signed_bytes, tx_hash, nonce
        )
        assert session.scalars(select(OracleWallet)).one().active_operation_id is None
        assert session.get(Claim, reclamo_registrado["id"]).onchain_claim_id is not None


def test_lost_broadcast_response_recovers_mined_effect(
    sesiones, reclamo_registrado, evm_mock_client, monkeypatch
):
    """The raw bytes are persisted before a provider loses its send response."""
    client, _mock = evm_mock_client
    with session_scope(sesiones) as session:
        config_id = current_configuration(session).id
        number = f"SN-{uuid4().hex[:12]}"
        session.get(Claim, reclamo_registrado["id"]).tracking_number = number
    original_send = client.w3.eth.send_raw_transaction

    def send_then_lose_response(raw):
        original_send(raw)
        raise ConnectionError("lost broadcast response")

    monkeypatch.setattr(client.w3.eth, "send_raw_transaction", send_then_lose_response)
    with pytest.raises(OracleOperationUncertainError):
        OracleCoordinator(sesiones, client).register_claim(
            reclamo_registrado["id"], number, "QmLostReply", config_id,
        )
    monkeypatch.setattr(client.w3.eth, "send_raw_transaction", original_send)
    with session_scope(sesiones) as session:
        op = session.scalars(select(OracleOperation)).one()
        operation_id = op.id
        tx_hash, nonce = op.transaction_hash, op.nonce
        assert op.status == OracleOperationStatus.INCIERTA.value
        assert session.scalars(select(OracleWallet)).one().active_operation_id == operation_id
    reconciled = OracleCoordinator(sesiones, client).reconcile_operation(operation_id)
    assert reconciled.status == OracleOperationStatus.CONFIRMADA.value
    with session_scope(sesiones) as session:
        op = session.get(OracleOperation, operation_id)
        assert (op.transaction_hash, op.nonce) == (tx_hash, nonce)
        assert session.scalars(select(OracleWallet)).one().active_operation_id is None
        assert session.get(Claim, reclamo_registrado["id"]).onchain_claim_id is not None


def test_pending_nonce_timeout_retains_wallet_until_mined(
    sesiones, reclamo_registrado, evm_mock_client, monkeypatch
):
    """A pending transaction cannot be retried, renonced or treated as failure."""
    client, _mock = evm_mock_client
    with session_scope(sesiones) as session:
        config_id = current_configuration(session).id
        number = f"SN-{uuid4().hex[:12]}"
        session.get(Claim, reclamo_registrado["id"]).tracking_number = number

    coordinator = OracleCoordinator(sesiones, client)
    coordinator.timeout = 1
    set_automine = client.w3.provider.make_request
    assert set_automine("evm_setAutomine", [False])["result"] is True
    try:
        with pytest.raises(OracleOperationUncertainError) as caught:
            coordinator.register_claim(
                reclamo_registrado["id"], number, "QmPendingNonce", config_id,
            )
        assert caught.value.last_error_code == "RPC_TIMEOUT"
        with session_scope(sesiones) as session:
            operation = session.scalars(select(OracleOperation)).one()
            operation_id = operation.id
            original_identity = (
                operation.nonce, operation.raw_signed_transaction, operation.transaction_hash
            )
            assert operation.status == OracleOperationStatus.INCIERTA.value
            assert session.scalars(select(OracleWallet)).one().active_operation_id == operation_id
        assert client.w3.eth.get_transaction_count(client.cuenta.address, "pending") > original_identity[0]

        def forbidden_rebroadcast(_raw):
            raise AssertionError("pending transaction was retransmitted")

        monkeypatch.setattr(client.w3.eth, "send_raw_transaction", forbidden_rebroadcast)
        unresolved = OracleCoordinator(sesiones, client).reconcile_operation(operation_id)
        assert unresolved.status == OracleOperationStatus.INCIERTA.value
        assert unresolved.last_error_code == "PENDING_RECEIPT"
        with session_scope(sesiones) as session:
            operation = session.get(OracleOperation, operation_id)
            assert (operation.nonce, operation.raw_signed_transaction, operation.transaction_hash) == original_identity
            assert session.scalars(select(OracleWallet)).one().active_operation_id == operation_id
            assert session.get(Claim, reclamo_registrado["id"]).onchain_claim_id is None
        set_automine("evm_mine", [])
    finally:
        set_automine("evm_setAutomine", [True])

    confirmed = OracleCoordinator(sesiones, client).reconcile_operation(operation_id)
    assert confirmed.status == OracleOperationStatus.CONFIRMADA.value
    with session_scope(sesiones) as session:
        assert session.scalars(select(OracleWallet)).one().active_operation_id is None
        assert session.get(Claim, reclamo_registrado["id"]).onchain_claim_id is not None


def test_receipt_without_expected_event_keeps_wallet_blocked(
    sesiones, reclamo_registrado, evm_client, monkeypatch
):
    """Even a successful matching transaction must emit the expected claim event."""
    with session_scope(sesiones) as session:
        configuration_id = current_configuration(session).id
        number = f"SN-{uuid4().hex[:12]}"
        session.get(Claim, reclamo_registrado["id"]).tracking_number = number

    def interrupt_after_mining(_operation, _receipt):
        raise RuntimeError("injected crash before domain commit")

    with pytest.raises(RuntimeError, match="injected crash before domain commit"):
        OracleCoordinator(
            sesiones, evm_client, before_confirm_hook=interrupt_after_mining
        ).register_claim(
            reclamo_registrado["id"], number, "QmEventCheck", configuration_id,
        )

    with session_scope(sesiones) as session:
        op = session.scalars(select(OracleOperation)).one()
        operation_id = op.id
        tx_hash = op.transaction_hash
    original_get_receipt = evm_client.w3.eth.get_transaction_receipt

    def receipt_without_event(hash_):
        receipt = dict(original_get_receipt(hash_))
        receipt["logs"] = []
        return receipt

    monkeypatch.setattr(evm_client.w3.eth, "get_transaction_receipt", receipt_without_event)
    reconciled = OracleCoordinator(sesiones, evm_client).reconcile_operation(operation_id)
    assert reconciled.status == OracleOperationStatus.INCIERTA.value
    assert reconciled.last_error_code == "RECEIPT_UNVERIFIED"
    with session_scope(sesiones) as session:
        assert session.get(OracleOperation, operation_id).transaction_hash == tx_hash
        assert session.scalars(select(OracleWallet)).one().active_operation_id == operation_id
        claim = session.get(Claim, reclamo_registrado["id"])
        assert claim.onchain_claim_id is None
        assert claim.pending_chain_action == "register_claim"


def test_unrelated_mined_hash_never_confirms_registration(
    sesiones, reclamo_registrado, evm_client
):
    """A receipt for another signed transaction cannot prove the reserved effect."""
    with session_scope(sesiones) as session:
        configuration_id = current_configuration(session).id
        number = f"SN-{uuid4().hex[:12]}"
        session.get(Claim, reclamo_registrado["id"]).tracking_number = number

    def interrupt_after_mining(_operation, _receipt):
        raise RuntimeError("injected crash before domain commit")

    coordinator = OracleCoordinator(
        sesiones, evm_client, before_confirm_hook=interrupt_after_mining
    )
    with pytest.raises(RuntimeError, match="injected crash before domain commit"):
        coordinator.register_claim(
            reclamo_registrado["id"], number,
            "QmTestEvidenceForIsolatedOracle", configuration_id,
        )

    with session_scope(sesiones) as session:
        op = session.scalars(select(OracleOperation)).one()
        assert op.raw_signed_transaction and op.nonce is not None
        assert op.status == OracleOperationStatus.ENVIADA.value
        original_hash = op.transaction_hash
        operation_id = op.id
        assert session.scalars(select(OracleWallet)).one().active_operation_id == operation_id
        assert session.get(Claim, reclamo_registrado["id"]).onchain_claim_id is None

    # Mine a different transaction and corrupt only the indexed hash. The raw
    # signed bytes still prove which transaction the application actually sent.
    other_hash = evm_client.w3.eth.send_transaction({
        "from": evm_client.cuenta.address,
        "to": evm_client.cuenta.address, "value": 0,
    })
    evm_client.w3.eth.wait_for_transaction_receipt(other_hash, timeout=5)
    with session_scope(sesiones) as session:
        session.get(OracleOperation, operation_id).transaction_hash = other_hash.hex()

    reconciled = OracleCoordinator(sesiones, evm_client).reconcile_operation(operation_id)
    assert reconciled.status == OracleOperationStatus.INCIERTA.value
    with session_scope(sesiones) as session:
        op = session.get(OracleOperation, operation_id)
        assert op.transaction_hash == other_hash.hex()
        assert op.transaction_hash != original_hash
        assert session.scalars(select(OracleWallet)).one().active_operation_id == operation_id
        assert session.get(Claim, reclamo_registrado["id"]).onchain_claim_id is None

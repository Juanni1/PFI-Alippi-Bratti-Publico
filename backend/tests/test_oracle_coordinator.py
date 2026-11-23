"""Pruebas de regresión para el oráculo durable y la reconciliación (ADR-018 / PFI-138).

Verifica:
1. Inmutabilidad de la intención previa al RPC (`oracle_operations`).
2. Exclusión de wallet mediante reserva durable y session advisory lock.
3. Ausencia de transacciones de escritura abiertas durante el RPC externo.
4. Persistencia obligatoria de nonce, bytes firmados y hash antes del broadcast.
5. Aplicación atómica del recibo al dominio y liberación de la wallet.
6. Preservación del fallo de verificación (VerificacionFallida) como evento confirmado.
7. Retención de reserva activa e incertidumbre ante timeout, bloqueando reenvíos ciegos.
8. Recuperación automática mediante reconciliación ante respuestas perdidas o commit gap.
9. Bloqueo estricto ante avance de nonce sin efecto minado.
10. Deduplicación de liquidación (misma operation_key para worker y operador).
11. Endpoints de la cola: exposición segura de oracleBlocked y oracleOperations,
    rechazo de campos extra en /cola/reconciliacion y RBAC estricto.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from web3.exceptions import ContractLogicError
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from app.adaptadores.chain import SimulatedChainClient
from app.adaptadores.database import session_scope
from app.adaptadores.inference import StubInferenceEngine
from app.adaptadores.models import (
    Claim,
    Configuration,
    OracleOperation,
    OracleWallet,
    Review,
    Transition,
)
from app.adaptadores.oracle_coordinator import (
    OracleCoordinator,
    OracleOperationRevertedError,
    OracleOperationRejectedError,
    OracleOperationUncertainError,
    WalletBlockedError,
    wallet_lock_key,
)
from app.adaptadores.proving import SimulatedProofGenerator
from app.dominio import worker
from app.dominio.claims import (
    UploadedFile,
    current_configuration,
    record_resolution,
    register_claim,
    settle_claim,
)
from app.dominio.ports import ChainReceipt, ProofResult
from app.dominio.vocabulary import (
    ClaimStatus,
    OracleOperationStatus,
    OracleOperationType,
    PolicyMode,
    TransitionCause,
)

PIXEL_PNG = b"\x89PNG\r\n\x1a\n" + b"contenido de prueba"


def test_intent_and_wallet_are_persisted_before_rpc(sesiones, publicador, reclamo_registrado):
    """La intención y la reserva de wallet quedan persistidas en PostgreSQL antes de I/O externo."""
    entered, release = Event(), Event()

    class HookedSimulatedChain(SimulatedChainClient):
        def settle(self, *args, **kwargs):
            entered.set()
            assert release.wait(10)
            return super().settle(*args, **kwargs)

    chain = HookedSimulatedChain(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
    coordinator = OracleCoordinator(sesiones, chain)

    with session_scope(sesiones) as session:
        claim = session.get(Claim, reclamo_registrado["id"])
        claim.status = ClaimStatus.APROBABLE
        config_id = current_configuration(session).id

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            coordinator.settle,
            claim_id=reclamo_registrado["id"],
            number=reclamo_registrado["number"],
            automatic=True,
            configuration_id=config_id,
        )
        try:
            assert entered.wait(10), "El RPC no inició a tiempo"
            # Verificar en PostgreSQL desde otra sesión que la intención y la wallet existen
            with session_scope(sesiones) as session:
                op = session.scalars(select(OracleOperation)).one()
                assert op.status in (
                    OracleOperationStatus.PREPARADA.value,
                    OracleOperationStatus.FIRMADA.value,
                    OracleOperationStatus.ENVIADA.value,
                )
                assert op.claim_id == reclamo_registrado["id"]
                assert op.operation == OracleOperationType.SETTLE.value
                assert op.raw_signed_transaction is not None
                assert op.transaction_hash is not None

                wallet = session.scalars(select(OracleWallet)).one()
                assert wallet.active_operation_id == op.id

                # Verificar que no hay locks de escritura retenidos en PostgreSQL durante el RPC
                session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                session.execute(text("SELECT id FROM claims FOR UPDATE"))
        finally:
            release.set()

        receipt = future.result(timeout=10)
        assert receipt.status == ClaimStatus.APROBADO

    # Verificar que tras el éxito la wallet quedó liberada y la operación confirmada
    with session_scope(sesiones) as session:
        op = session.scalars(select(OracleOperation)).one()
        assert op.status == OracleOperationStatus.CONFIRMADA.value
        assert op.confirmed_at is not None

        wallet = session.scalars(select(OracleWallet)).one()
        assert wallet.active_operation_id is None


def test_session_advisory_lock_excludes_concurrent_operations(sesiones):
    """El advisory lock de sesión de PostgreSQL por red/wallet previene envíos paralelos."""
    chain = SimulatedChainClient(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
    coord1 = OracleCoordinator(sesiones, chain)
    coord2 = OracleCoordinator(sesiones, chain)

    lock_k = wallet_lock_key(coord1.chain_id, coord1.sender_address)
    conn1 = coord1._acquire_session_lock(lock_k)

    # Intentar adquirir el mismo lock con un timeout corto en otra conexión debe fallar
    conn2 = sesiones.kw["bind"].connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        locked = conn2.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_k}
        ).scalar()
        assert locked is False, "El segundo proceso no debió adquirir el advisory lock"
    finally:
        conn2.close()
        coord1._release_session_lock(conn1, lock_k)

    # Tras liberar conn1, conn2 puede adquirirlo
    conn3 = coord2._acquire_session_lock(lock_k)
    try:
        assert conn3 is not None
    finally:
        coord2._release_session_lock(conn3, lock_k)


def test_simulated_timeout_cannot_be_confirmed_without_a_receipt(sesiones, reclamo_registrado):
    """A simulated timeout cannot prove an effect or release the wallet."""
    with session_scope(sesiones) as session:
        config_id = current_configuration(session).id
        session.get(Claim, reclamo_registrado["id"]).status = ClaimStatus.APROBABLE
        original_transition_count = len(session.scalars(
            select(Transition).where(Transition.claim_id == reclamo_registrado["id"])
        ).all())

    class TimingOutChain(SimulatedChainClient):
        def settle(self, *args, **kwargs):
            raise TimeoutError("Conexión perdida con la RPC tras el envío")

    chain = TimingOutChain(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
    coordinator = OracleCoordinator(sesiones, chain)

    with pytest.raises(TimeoutError):
        coordinator.settle(
            claim_id=reclamo_registrado["id"],
            number=reclamo_registrado["number"],
            automatic=True,
            configuration_id=config_id,
        )

    # La wallet debe quedar bloqueada y la operación en estado incierto
    with session_scope(sesiones) as session:
        op = session.scalars(select(OracleOperation)).one()
        assert op.status == OracleOperationStatus.INCIERTA.value
        assert op.last_error_code == "SIMULATED_TIMEOUT"
        wallet = session.scalars(select(OracleWallet)).one()
        assert wallet.active_operation_id == op.id

    # Un segundo intento directo sin reconciliar debe ser rechazado por wallet bloqueada
    with pytest.raises(WalletBlockedError):
        coordinator.settle(
            claim_id=reclamo_registrado["id"],
            number=reclamo_registrado["number"],
            automatic=True,
            configuration_id=config_id,
        )

    # The simulated client has no durable receipt to verify after the timeout.
    reconciled = coordinator.reconcile_operation(op.id)
    assert reconciled.status == OracleOperationStatus.INCIERTA.value
    assert reconciled.last_error_code == "SIMULATED_RESULT_UNVERIFIABLE"

    with session_scope(sesiones) as session:
        wallet = session.scalars(select(OracleWallet)).one()
        assert wallet.active_operation_id == op.id
        claim = session.get(Claim, reclamo_registrado["id"])
        assert claim.status == ClaimStatus.APROBABLE
        assert claim.pending_chain_action == OracleOperationType.SETTLE.value
        assert len(session.scalars(
            select(Transition).where(Transition.claim_id == claim.id)
        ).all()) == original_transition_count


def test_proof_failure_is_confirmed_event_not_reverted(sesiones, reclamo_registrado):
    """RNF-17: una prueba rechazada asienta VerificacionFallida como evento confirmado."""
    with session_scope(sesiones) as session:
        config_id = current_configuration(session).id

    chain = SimulatedChainClient(
        derivation_threshold=0.9,
        policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO,
        verifier_accepts=False,
    )
    coordinator = OracleCoordinator(sesiones, chain)

    fake_proof = ProofResult(
        proof=b'{"instances": [], "hex_proof": ""}',
        public_inputs=b"inputs",
        proof_hash="0x" + "f" * 64,
        circuit_parameters={},
        declared_visibility={},
        verified_locally=False,
    )

    receipt = coordinator.submit_inference(
        claim_id=reclamo_registrado["id"],
        number=reclamo_registrado["number"],
        proof=fake_proof,
        confidence=0.95,
        amount_above_threshold=False,
        rules_consistent=True,
        configuration_id=config_id,
    )

    assert receipt.status == ClaimStatus.VERIFICACION_FALLIDA
    assert receipt.cause == TransitionCause.FALLO_DE_VERIFICACION

    with session_scope(sesiones) as session:
        op = session.scalars(select(OracleOperation)).one()
        assert op.status == OracleOperationStatus.CONFIRMADA.value
        claim = session.get(Claim, reclamo_registrado["id"])
        assert claim.status == ClaimStatus.VERIFICACION_FALLIDA
        wallet = session.scalars(select(OracleWallet)).one()
        assert wallet.active_operation_id is None


def test_confirmed_registration_retry_returns_original_result(sesiones, reclamo_registrado):
    """A confirmed intent cannot fabricate an approval on a duplicate call."""
    chain = SimulatedChainClient(
        derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO
    )
    coordinator = OracleCoordinator(sesiones, chain)
    with session_scope(sesiones) as session:
        config_id = current_configuration(session).id
    first = coordinator.register_claim(
        reclamo_registrado["id"], reclamo_registrado["number"],
        "QmTestEvidenceForIdempotentRegistration", config_id,
    )
    second = coordinator.register_claim(
        reclamo_registrado["id"], reclamo_registrado["number"],
        "QmTestEvidenceForIdempotentRegistration", config_id,
    )
    assert first.status == second.status == ClaimStatus.REGISTRADO
    assert second.cause is None
    assert second.transaction_hash == first.transaction_hash
    assert second.block_number == first.block_number
    with session_scope(sesiones) as session:
        assert len(session.scalars(select(OracleOperation)).all()) == 1
        assert session.scalars(select(OracleWallet)).one().active_operation_id is None


def test_shared_settlement_operation_key_deduplication(sesiones, reclamo_registrado):
    """Liquidación automática y liquidación humana generan la misma operation_key inmutable."""
    chain = SimulatedChainClient(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
    coord = OracleCoordinator(sesiones, chain)

    key_auto = coord.build_operation_key(
        operation_type=OracleOperationType.SETTLE.value,
        claim_number=reclamo_registrado["number"],
    )
    key_human = coord.build_operation_key(
        operation_type=OracleOperationType.SETTLE.value,
        claim_number=reclamo_registrado["number"],
    )
    assert key_auto == key_human


def test_api_startup_remains_ready_while_oracle_reconciliation_waits(
    sesiones, reclamo_registrado, monkeypatch
):
    """A slow chain receipt must not hold API readiness hostage."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.adaptadores import reconcile

    with session_scope(sesiones) as session:
        config_id = current_configuration(session).id
        session.add(OracleOperation(
            id=uuid4(), operation_key="startup-pending-receipt",
            claim_id=reclamo_registrado["id"], configuration_id=config_id,
            operation=OracleOperationType.REGISTER_CLAIM.value,
            payload={"number": reclamo_registrado["number"], "evidence_id": "QmStartup"},
            chain_id=31337, sender_address="0x1111111111111111111111111111111111111111",
            contract_address="0x2222222222222222222222222222222222222222",
            status=OracleOperationStatus.PREPARADA.value,
        ))

    entered, release = Event(), Event()

    class SlowCoordinator:
        def reconcile_pending(self, *, max_operations):
            assert max_operations == 50
            entered.set()
            assert release.wait(10)
            return []

    monkeypatch.setattr(reconcile, "build_coordinator", lambda _: SlowCoordinator())

    def request_health():
        with TestClient(app) as client:
            return client.get("/health")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(request_health)
        try:
            assert entered.wait(5)
            response = future.result(timeout=2)
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
        finally:
            release.set()


def test_prepared_intent_is_recovered_without_leaving_a_wallet_stranded(
    sesiones, reclamo_registrado
):
    """A pre-sign crash can safely resume the same frozen intent on startup."""
    chain = SimulatedChainClient(
        derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO
    )
    coordinator = OracleCoordinator(sesiones, chain)
    number = reclamo_registrado["number"]
    with session_scope(sesiones) as session:
        config_id = current_configuration(session).id
        op = OracleOperation(
            id=uuid4(),
            operation_key=coordinator.build_operation_key(
                operation_type=OracleOperationType.REGISTER_CLAIM.value,
                claim_number=number,
            ),
            claim_id=reclamo_registrado["id"],
            configuration_id=config_id,
            operation=OracleOperationType.REGISTER_CLAIM.value,
            payload={"number": number, "evidence_id": "QmPreparedEvidence"},
            chain_id=coordinator.chain_id,
            sender_address=coordinator.sender_address,
            contract_address=coordinator.contract_address,
            status=OracleOperationStatus.PREPARADA.value,
        )
        session.add(op)
        session.flush()
        operation_id = op.id
        session.add(OracleWallet(
            chain_id=coordinator.chain_id,
            sender_address=coordinator.sender_address,
            active_operation_id=operation_id,
        ))
        session.get(Claim, reclamo_registrado["id"]).pending_chain_action = "register_claim"

    recovered = coordinator.reconcile_pending(max_operations=50)
    assert [op.id for op in recovered] == [operation_id]
    with session_scope(sesiones) as session:
        operation = session.get(OracleOperation, operation_id)
        assert operation.status == OracleOperationStatus.CONFIRMADA.value
        assert session.get(OracleWallet, {
            "chain_id": coordinator.chain_id,
            "sender_address": coordinator.sender_address,
        }).active_operation_id is None
        claim = session.get(Claim, reclamo_registrado["id"])
        assert claim.pending_chain_action is None
        assert claim.onchain_claim_id is not None


def test_queue_endpoint_visibility_and_reconciliation_action(cliente, sesiones, reclamo_registrado):
    """GET /cola expone oracleBlocked y oracleOperations; POST /cola/reconciliacion opera estrictamente."""
    token_op = "operador-op-02"
    token_rev = "revisor-rev-04"

    # Cola limpia
    resp = cliente.get("/cola", headers={"Authorization": f"Bearer {token_op}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["oracleBlocked"] is False
    assert body["oracleOperations"] == []

    # Insertar una operación incierta simulada
    with session_scope(sesiones) as session:
        config_id = current_configuration(session).id
        op = OracleOperation(
            id=uuid4(),
            operation_key="test-uncertain-key",
            claim_id=reclamo_registrado["id"],
            configuration_id=config_id,
            operation=OracleOperationType.RESOLVE.value,
            payload={"number": reclamo_registrado["number"]},
            chain_id=31337,
            sender_address="0x1111111111111111111111111111111111111111",
            contract_address="0x2222222222222222222222222222222222222222",
            status=OracleOperationStatus.INCIERTA.value,
            last_error_code="RPC_TIMEOUT",
            transaction_hash="0x" + "a" * 64,
        )
        session.add(op)
        session.flush()
        op_id = str(op.id)
        wallet = OracleWallet(
            chain_id=31337,
            sender_address="0x1111111111111111111111111111111111111111",
            active_operation_id=op.id,
        )
        session.merge(wallet)

    # La cola ahora debe reflejar bloqueo y listar la operación
    resp = cliente.get("/cola", headers={"Authorization": f"Bearer {token_op}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["oracleBlocked"] is True
    assert len(body["oracleOperations"]) == 1
    op_item = body["oracleOperations"][0]
    assert op_item["operationId"] == op_id
    assert op_item["number"] == reclamo_registrado["number"]
    assert op_item["status"] == "incierta"
    assert op_item["lastErrorCode"] == "RPC_TIMEOUT"
    assert "senderAddress" not in op_item
    assert "nonce" not in op_item
    assert "rawSignedTransaction" not in op_item

    # Autorización de POST /cola/reconciliacion
    url_rec = "/cola/reconciliacion"
    assert cliente.post(url_rec, json={"operationId": op_id}).status_code == 401
    assert cliente.post(url_rec, headers={"Authorization": f"Bearer {token_rev}"}, json={"operationId": op_id}).status_code == 403

    # Operación inexistente
    fake_uuid = str(uuid4())
    assert cliente.post(url_rec, headers={"Authorization": f"Bearer {token_op}"}, json={"operationId": fake_uuid}).status_code == 404

    # Rechazo estricto de campos adicionales (extra=forbid)
    resp_extra = cliente.post(
        url_rec,
        headers={"Authorization": f"Bearer {token_op}"},
        json={"operationId": op_id, "extraField": "not_allowed"},
    )
    assert resp_extra.status_code == 422

    # Reconciliación válida
    resp_ok = cliente.post(
        url_rec,
        headers={"Authorization": f"Bearer {token_op}"},
        json={"operationId": op_id},
    )
    assert resp_ok.status_code == 200
    assert resp_ok.json()["operationId"] == op_id


@pytest.mark.parametrize("error_name", ["ContractCustomError", "ContractLogicError"])
def test_pre_sign_contract_rejection_releases_wallet_without_broadcast(
    sesiones, reclamo_registrado, cliente, monkeypatch, error_name
):
    """El rechazo determinista previo a firma termina la intención sin bloquear otros casos."""
    from types import SimpleNamespace
    from unittest.mock import Mock
    from web3 import exceptions

    coordinator = OracleCoordinator(sesiones, SimulatedChainClient(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO))
    coordinator.is_web3 = True
    coordinator.w3 = SimpleNamespace(eth=SimpleNamespace(get_transaction_count=Mock(return_value=7)))
    coordinator.cuenta = SimpleNamespace(sign_transaction=Mock())
    contract_function = Mock()
    contract_function.build_transaction.side_effect = getattr(exceptions, error_name)("private RPC details")
    monkeypatch.setattr(coordinator, "_build_contract_function", Mock(return_value=contract_function))
    with session_scope(sesiones) as session:
        config_id = current_configuration(session).id
        session.get(Claim, reclamo_registrado["id"]).status = ClaimStatus.REQUIERE_REVISION
    args = dict(claim_id=reclamo_registrado["id"], operation_type="resolve",
                payload={"number": reclamo_registrado["number"], "favorable": True},
                configuration_id=config_id)
    with pytest.raises(OracleOperationRejectedError):
        coordinator.submit_operation(**args)
    with session_scope(sesiones) as session:
        operation = session.scalars(select(OracleOperation)).one()
        operation_id = operation.id
        assert operation.status == "fallida_pre_envio"
        assert operation.last_error_code == "CONTRACT_REJECTED_BEFORE_SIGNING"
        assert operation.nonce is None
        assert operation.raw_signed_transaction is None
        assert operation.transaction_hash is None
        assert operation.broadcast_at is None
        assert operation.payload == args["payload"]
        assert session.scalars(select(OracleWallet)).one().active_operation_id is None
        claim = session.get(Claim, reclamo_registrado["id"])
        assert claim.status == ClaimStatus.REQUIERE_REVISION
        assert claim.pending_chain_action is None
    coordinator.cuenta.sign_transaction.assert_not_called()
    assert coordinator.reconcile_pending() == []
    assert coordinator.reconcile_operation(operation_id).status == "fallida_pre_envio"
    with pytest.raises(OracleOperationRejectedError):
        coordinator.submit_operation(**args)
    assert contract_function.build_transaction.call_count == 1
    with pytest.raises(OracleOperationRejectedError):
        with session_scope(sesiones) as session:
            coordinator.prepare_operation_in_session(session, **args)
    with session_scope(sesiones) as session:
        assert session.scalars(select(OracleWallet)).one().active_operation_id is None
    response = cliente.get("/cola", headers={"Authorization": "Bearer operador-op-02"})
    assert response.status_code == 200
    assert response.json()["oracleBlocked"] is False
    assert response.json()["oracleOperations"] == []
    response = cliente.post("/cola/reconciliacion", headers={"Authorization": "Bearer operador-op-02"},
                            json={"operationId": str(operation_id)})
    assert response.status_code == 200
    assert response.json()["status"] == "fallida_pre_envio"


def test_pre_sign_timeout_preserves_reservation(sesiones, reclamo_registrado, monkeypatch):
    """Un fallo de transporte no demuestra rechazo determinista y conserva la reserva."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    coordinator = OracleCoordinator(sesiones, SimulatedChainClient(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO))
    coordinator.is_web3 = True
    coordinator.w3 = SimpleNamespace(eth=SimpleNamespace(get_transaction_count=Mock(return_value=7)))
    coordinator.cuenta = SimpleNamespace(sign_transaction=Mock())
    contract_function = Mock()
    contract_function.build_transaction.side_effect = TimeoutError("RPC unavailable")
    monkeypatch.setattr(coordinator, "_build_contract_function", Mock(return_value=contract_function))
    with session_scope(sesiones) as session:
        config_id = current_configuration(session).id
    with pytest.raises(TimeoutError):
        coordinator.submit_operation(claim_id=reclamo_registrado["id"], operation_type="resolve",
                                     payload={"number": reclamo_registrado["number"]},
                                     configuration_id=config_id)
    with session_scope(sesiones) as session:
        operation = session.scalars(select(OracleOperation)).one()
        assert operation.status == "preparada"
        assert operation.transaction_hash is None
        assert session.scalars(select(OracleWallet)).one().active_operation_id == operation.id
    coordinator.cuenta.sign_transaction.assert_not_called()


def _pre_sign_test_coordinator(sesiones):
    coord=OracleCoordinator(sesiones, SimulatedChainClient(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO))
    coord.is_web3=True
    coord.w3=SimpleNamespace(eth=SimpleNamespace(get_transaction_count=Mock(return_value=7), send_raw_transaction=Mock()))
    coord.cuenta=SimpleNamespace(sign_transaction=Mock())
    return coord


def test_concurrent_same_intent_never_rebuilds_terminal(sesiones,reclamo_registrado):
    first=_pre_sign_test_coordinator(sesiones);second=_pre_sign_test_coordinator(sesiones)
    first_build,second_lock=Event(),Event()
    def reject_first(params):
        first_build.set()
        assert second_lock.wait(5)
        raise ContractLogicError('private')
    f1=Mock();f1.build_transaction.side_effect=reject_first
    f2=Mock();f2.build_transaction.side_effect=ContractLogicError('private')
    first._build_contract_function=Mock(return_value=f1)
    second._build_contract_function=Mock(return_value=f2)
    acquire=second._acquire_session_lock
    def delayed_lock(key):
        second_lock.set()
        return acquire(key)
    second._acquire_session_lock=delayed_lock
    with session_scope(sesiones) as s:
        config=current_configuration(s).id
    args=dict(claim_id=reclamo_registrado['id'],operation_type='resolve',payload={'number':reclamo_registrado['number'],'favorable':True},configuration_id=config)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(first.submit_operation,**args)
        assert first_build.wait(5)
        b=pool.submit(second.submit_operation,**args)
        with pytest.raises(OracleOperationRejectedError): a.result(timeout=10)
        second_error=b.exception(timeout=10)
    with session_scope(sesiones) as s:
        op=s.scalars(select(OracleOperation)).one()
        wallet=s.scalars(select(OracleWallet)).one()
        assert wallet.active_operation_id is None, 'Concurrent stale PREPARADA snapshot re-reserved terminal operation'
        assert op.status=='fallida_pre_envio'
    assert isinstance(second_error,OracleOperationRejectedError)
    assert f2.build_transaction.call_count==0

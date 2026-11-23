"""Salida pública del CLI frente a una operación ORM ya desacoplada."""

import json
import os
import re
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import func, select

from app.adaptadores.database import session_scope
from app.adaptadores.models import Claim, Job, OracleOperation, OracleWallet, Transition
from app.dominio.claims import current_configuration
from app.dominio.vocabulary import OracleOperationStatus


def _terminal_operation(sesiones, claim):
    with session_scope(sesiones) as session:
        config = current_configuration(session)
        operation = OracleOperation(
            id=uuid4(), operation_key="cli-terminal-" + str(uuid4()),
            claim_id=claim["id"], configuration_id=config.id,
            operation="settle", payload={"number": claim["number"]},
            chain_id=31337, sender_address="0x" + "11" * 20,
            contract_address="0x" + "22" * 20,
            transaction_hash="0x" + "ab" * 32, block_number=17,
            status=OracleOperationStatus.CONFIRMADA.value,
        )
        session.add(operation)
        session.flush()
        return operation.id


def _snapshot(sesiones, operation_id):
    with session_scope(sesiones) as session:
        operation = session.get(OracleOperation, operation_id)
        claim = session.get(Claim, operation.claim_id)
        return (
            tuple(session.scalar(select(func.count(model.__table__.c[next(iter(model.__table__.primary_key.columns)).name])))
                  for model in (Claim, Job, OracleOperation, OracleWallet, Transition)),
            (operation.status, operation.transaction_hash, operation.nonce, operation.block_number,
             claim.status, claim.pending_chain_action),
        )


def test_cli_operation_id_serializes_detached_confirmed_operation(postgres_url, sesiones, reclamo_registrado):
    operation_id = _terminal_operation(sesiones, reclamo_registrado)
    before = _snapshot(sesiones, operation_id)
    env = {"PATH": os.environ["PATH"], "DATABASE_URL": postgres_url,
           "CLIENTE_CADENA": "simulado", "PYTHONDONTWRITEBYTECODE": "1"}
    outcome = subprocess.run(
        [sys.executable, "-m", "app.adaptadores.reconcile", "--operation-id", str(operation_id)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    expected = {
        "operationId": str(operation_id), "number": reclamo_registrado["number"],
        "operation": "settle", "status": OracleOperationStatus.CONFIRMADA.value,
        "transactionHash": "0x" + "ab" * 32, "blockNumber": 17, "lastErrorCode": None,
    }
    assert outcome.returncode == 0, outcome.stderr
    assert outcome.stdout == json.dumps(expected, indent=2) + "\n"
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \[INFO\] "
                        rf"Reconciliando operación {operation_id}\.\.\.\n", outcome.stderr)
    assert _snapshot(sesiones, operation_id) == before


def test_cli_preserves_loaded_operation_output(monkeypatch, capsys, sesiones, reclamo_registrado):
    from app.adaptadores import reconcile

    operation_id = _terminal_operation(sesiones, reclamo_registrado)
    with session_scope(sesiones) as session:
        loaded = session.get(OracleOperation, operation_id)
        assert loaded.claim.tracking_number == reclamo_registrado["number"]
        session.expunge_all()

    class LoadedCoordinator:
        def reconcile_operation(self, identifier):
            assert identifier == operation_id
            return loaded

    monkeypatch.setenv("CLIENTE_CADENA", "simulado")
    monkeypatch.setattr(reconcile, "build_coordinator", lambda _: LoadedCoordinator())
    before = _snapshot(sesiones, operation_id)
    capsys.readouterr()  # Descartar únicamente la salida de migración del fixture.
    assert reconcile.main(["--operation-id", str(operation_id)]) == 0
    captured = capsys.readouterr()
    assert captured.out == json.dumps({
        "operationId": str(operation_id), "number": reclamo_registrado["number"],
        "operation": "settle", "status": OracleOperationStatus.CONFIRMADA.value,
        "transactionHash": "0x" + "ab" * 32, "blockNumber": 17, "lastErrorCode": None,
    }, indent=2) + "\n"
    assert captured.err == ""
    assert _snapshot(sesiones, operation_id) == before

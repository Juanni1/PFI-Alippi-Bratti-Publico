"""La validación del operador no devuelve secretos rechazados en HTTP 422."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.adaptadores.database import session_scope
from app.adaptadores.models import OracleOperation, OracleWallet


@pytest.mark.parametrize(
    "payload,secret",
    [
        ({"operationId": str(uuid4()), "rawSignedTransaction": "SECRET_SIGNED_BYTES"}, "SECRET_SIGNED_BYTES"),
        ({"operationId": "SECRET_OPERATION_ID"}, "SECRET_OPERATION_ID"),
    ],
)
def test_reconciliation_422_never_echoes_submitted_values_or_mutates_queue(
    cliente, sesiones, payload, secret
):
    headers = {"Authorization": "Bearer operador-op-02"}
    before = cliente.get("/cola", headers=headers)
    assert before.status_code == 200
    with session_scope(sesiones) as session:
        operations_before = session.scalar(select(func.count()).select_from(OracleOperation))
        wallets_before = session.scalar(select(func.count()).select_from(OracleWallet))

    response = cliente.post("/cola/reconciliacion", headers=headers, json=payload)
    assert response.status_code == 422
    assert response.json() == {"title": "La solicitud de reconciliación es inválida."}
    assert secret not in response.text
    after = cliente.get("/cola", headers=headers)
    assert after.status_code == 200
    assert after.json() == before.json()
    with session_scope(sesiones) as session:
        assert session.scalar(select(func.count()).select_from(OracleOperation)) == operations_before
        assert session.scalar(select(func.count()).select_from(OracleWallet)) == wallets_before


def test_existing_resolution_422_keeps_previous_response_shape(cliente, reclamo_registrado):
    response = cliente.post(
        f"/reclamos/{reclamo_registrado['number']}/resolucion",
        headers={"Authorization": "Bearer revisor-rev-04"},
        json={"favorable": True, "justification": ""},
    )
    assert response.status_code == 422
    assert isinstance(response.json().get("detail"), list)
    assert response.json()["detail"][0]["input"] == ""

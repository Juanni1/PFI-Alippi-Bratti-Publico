"""Entrega autorizada de originales: permisos, integridad y lectura sin efectos."""

from decimal import Decimal
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import event, select

from app.adaptadores.ipfs_client import IpfsError
from app.adaptadores.models import Evidence
from app.dominio.claims import UploadedFile, register_claim
from tests.conftest import PIXEL_PNG

REVIEWER = {"Authorization": "Bearer revisor-rev-04"}
OPERATOR = {"Authorization": "Bearer operador-op-02"}
FAILED = "No se pudo recuperar la fotografía."


@pytest.fixture()
def photo_claim(cliente, sesiones, publicador):
    with sesiones() as session:
        claims = [register_claim(
            session, publicador, damage_description="Daño sintético.",
            files=[UploadedFile("frente.png", "image/png", PIXEL_PNG)],
            declared_amount=Decimal("100"),
        ) for _ in range(2)]
        session.commit()
        first, other = claims
        return {
            "number": first.claim.tracking_number,
            "token": first.access_token,
            "foreign_token": other.access_token,
            "file_id": first.evidence[0].id,
            "foreign_id": other.evidence[0].id,
            "cid": first.evidence[0].cid,
        }


def photo_url(claim, file_id=None):
    return f'/reclamos/{claim["number"]}/evidencia/{file_id or claim["file_id"]}'


def snapshot(cliente, claim):
    return [cliente.get(path, headers=headers).content for path, headers in [
        (f'/reclamos/{claim["number"]}', {"Authorization": f'Bearer {claim["token"]}'}),
        (f'/publico/reclamos/{claim["number"]}/traza', {}),
        ("/cola", OPERATOR),
    ]]


def test_retrieval_releases_database_connection(cliente, photo_claim, sesiones, publicador):
    engine = sesiones.kw["bind"]
    checked_out = []

    def checkout(*args):
        checked_out.append(True)

    def checkin(*args):
        checked_out.pop()

    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    retrieve = publicador.retrieve

    def retrieve_without_connection(cid, *, max_bytes=None):
        assert not checked_out
        return retrieve(cid, max_bytes=max_bytes)

    publicador.retrieve = retrieve_without_connection
    try:
        response = cliente.get(photo_url(photo_claim), headers=REVIEWER)
        assert response.status_code == 200
        assert response.content == PIXEL_PNG
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


@pytest.mark.parametrize("headers", [REVIEWER, OPERATOR])
def test_original_photo(cliente, photo_claim, publicador, headers):
    before = snapshot(cliente, photo_claim)
    retrieve = Mock(wraps=publicador.retrieve)
    publicador.retrieve = retrieve
    detail = cliente.get(f'/reclamos/{photo_claim["number"]}', headers=headers).json()
    assert detail["evidence"]["files"][0]["url"] == photo_url(photo_claim)
    assert detail["evidence"]["evidenceId"] == photo_claim["cid"]
    response = cliente.get(photo_url(photo_claim), headers=headers)
    assert response.status_code == 200
    assert response.content == PIXEL_PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    retrieve.assert_called_once_with(photo_claim["cid"], max_bytes=len(PIXEL_PNG))
    assert snapshot(cliente, photo_claim) == before


@pytest.mark.parametrize("token,status,body", [
    (None, 401, {"title": "No se pudo acreditar el acceso a este reclamo."}),
    ("invalid", 401, {"title": "No se pudo acreditar el acceso a este reclamo."}),
    ("token", 403, {"title": "El perfil no admite esta operación.",
                    "detail": "el perfil reclamante no admite la operación"}),
    ("foreign_token", 401, {"title": "No se pudo acreditar el acceso a este reclamo."}),
])
def test_photo_authorization(cliente, photo_claim, publicador, token, status, body):
    before = snapshot(cliente, photo_claim)
    publicador.retrieve = Mock(side_effect=AssertionError("no debe recuperar"))
    value = photo_claim.get(token, token)
    response = cliente.get(photo_url(photo_claim), headers={
        "Authorization": f"Bearer {value}"
    } if token else {})
    assert response.status_code == status
    assert response.json() == body
    publicador.retrieve.assert_not_called()
    assert snapshot(cliente, photo_claim) == before


def test_file_selector_is_scoped_to_claim(cliente, photo_claim, publicador):
    before = snapshot(cliente, photo_claim)
    publicador.retrieve = Mock(side_effect=AssertionError("no debe recuperar"))
    response = cliente.get(photo_url(photo_claim, photo_claim["foreign_id"]), headers=REVIEWER)
    assert response.status_code == 404
    assert response.json() == {"detail": "Fotografía no encontrada en el reclamo."}
    publicador.retrieve.assert_not_called()
    assert snapshot(cliente, photo_claim) == before


@pytest.mark.parametrize("error,status,detail", [
    (IpfsError("private CID"), 502, FAILED),
    (httpx.ConnectError("private URL"), 502, FAILED),
    (OSError("private path"), 502, FAILED),
    (httpx.ReadTimeout("private URL"), 504, "Se agotó el tiempo para recuperar la fotografía."),
])
def test_retrieval_failure_is_local(cliente, photo_claim, publicador, error, status, detail):
    before = snapshot(cliente, photo_claim)
    publicador.retrieve = Mock(side_effect=error)
    response = cliente.get(photo_url(photo_claim), headers=REVIEWER)
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    publicador.retrieve.assert_called_once_with(photo_claim["cid"], max_bytes=len(PIXEL_PNG))
    assert snapshot(cliente, photo_claim) == before


@pytest.mark.parametrize("content", [PIXEL_PNG[:-1], PIXEL_PNG + b"x", b"x" * len(PIXEL_PNG)])
def test_integrity_failure(cliente, photo_claim, publicador, content):
    before = snapshot(cliente, photo_claim)
    publicador.retrieve = Mock(return_value=content)
    response = cliente.get(photo_url(photo_claim), headers=REVIEWER)
    assert response.status_code == 502
    assert response.json() == {"detail": FAILED}
    assert snapshot(cliente, photo_claim) == before


@pytest.mark.parametrize("field,value", [
    ("size_bytes", 0), ("size_bytes", -1), ("size_bytes", 10 * 1024 * 1024 + 1),
    ("mime_type", "text/html"), ("sha256", "invalid"),
])
def test_invalid_metadata_fails_before_io(cliente, photo_claim, sesiones, publicador, field, value):
    with sesiones() as session:
        evidence = session.scalar(select(Evidence).where(Evidence.id == photo_claim["file_id"]))
        setattr(evidence, field, value)
        session.commit()
    before = snapshot(cliente, photo_claim)
    publicador.retrieve = Mock(side_effect=AssertionError("no debe recuperar"))
    response = cliente.get(photo_url(photo_claim), headers=REVIEWER)
    assert response.status_code == 502
    assert response.json() == {"detail": FAILED}
    publicador.retrieve.assert_not_called()
    assert snapshot(cliente, photo_claim) == before

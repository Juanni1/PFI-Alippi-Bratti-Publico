"""Integración con Kubo local real, sin publicación ni replicación remota."""

import hashlib
import io
import os
from decimal import Decimal

import pytest
from PIL import Image

from app.adaptadores.ipfs_client import IpfsClient
from app.dominio.claims import UploadedFile, register_claim
from tests.test_photo_evidence import OPERATOR, REVIEWER


@pytest.mark.skipif(not os.environ.get("TEST_KUBO_API_URL"), reason="requiere Kubo local aislado")
def test_original_png_jpeg_from_real_local_kubo(cliente, sesiones):
    class LocalKubo(IpfsClient):
        def publish(self, content, filename="evidencia"):
            # Solo ADD local: no llama a Pinata ni prueba replicación remota.
            return self.add(content, filename)

    publisher = LocalKubo(kubo_api_url=os.environ["TEST_KUBO_API_URL"], pinata_jwt="")
    files = []
    for format_, mime in [("PNG", "image/png"), ("JPEG", "image/jpeg")]:
        output = io.BytesIO()
        Image.new("RGB", (7, 5), color=(24, 80, 160)).save(output, format=format_)
        content = output.getvalue()
        Image.open(io.BytesIO(content)).load()
        files.append(UploadedFile(f"synthetic.{format_.lower()}", mime, content))
    with sesiones() as session:
        registered = register_claim(
            session, publisher, damage_description="Daño sintético.", files=files,
            declared_amount=Decimal("100"),
        )
        session.commit()
        number = registered.claim.tracking_number
        first_cid = registered.evidence[0].cid
    cliente.app.state.publisher = publisher
    detail = cliente.get(f"/reclamos/{number}", headers=REVIEWER).json()
    assert detail["evidence"]["evidenceId"] == first_cid
    assert len(detail["evidence"]["files"]) == 2
    for file, metadata in zip(files, detail["evidence"]["files"], strict=True):
        assert metadata["sha256"] == hashlib.sha256(file.content).hexdigest()
        for headers in [REVIEWER, OPERATOR]:
            response = cliente.get(metadata["url"], headers=headers)
            assert response.status_code == 200
            assert response.content == file.content
            assert response.headers["content-type"] == file.mime_type
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            Image.open(io.BytesIO(response.content)).load()
    assert publisher.retrieve(first_cid) == files[0].content

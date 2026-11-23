"""Publicador local: el CID tiene que ser el mismo que produciría Kubo.

Si no lo fuera, la traza pública de MK-08 exhibiría un identificador que nadie
puede resolver, y el procedimiento de replicación de RF-53 quedaría apoyado
sobre una promesa falsa.
"""

import pytest

from app.adaptadores.ipfs_client import IpfsError
from app.adaptadores.local_storage import (
    TAMANO_MAXIMO_DE_BLOQUE,
    LocalEvidencePublisher,
    compute_cid,
)


def test_el_cid_coincide_con_el_canonico_de_ipfs():
    """Vector conocido: `hello world` con CID v1, códec raw y sha2-256."""
    assert (
        compute_cid(b"hello world")
        == "bafkreifzjut3te2nhyekklss27nh3k72ysco7y32koao5eei66wof36n5e"
    )


def test_el_cid_es_determinista_y_distingue_contenidos():
    assert compute_cid(b"una foto") == compute_cid(b"una foto")
    assert compute_cid(b"una foto") != compute_cid(b"otra foto")


def test_por_encima_del_bloque_falla_en_vez_de_devolver_un_cid_que_no_corresponde():
    """Kubo trocea a partir de 256 KB y arma un nodo dag-pb con otro CID.
    Devolver el de un solo bloque sería devolver un identificador equivocado."""
    with pytest.raises(IpfsError, match="dag-pb"):
        compute_cid(b"x" * (TAMANO_MAXIMO_DE_BLOQUE + 1))


def test_lo_publicado_se_recupera_por_su_cid(tmp_path):
    publicador = LocalEvidencePublisher(root=str(tmp_path))
    cid = publicador.publish(b"contenido de la foto")
    assert publicador.retrieve(cid) == b"contenido de la foto"


def test_bounded_local_retrieval(tmp_path):
    publisher = LocalEvidencePublisher(root=str(tmp_path))
    cid = publisher.publish(b"photo")
    assert publisher.retrieve(cid, max_bytes=5) == b"photo"
    with pytest.raises(IpfsError):
        publisher.retrieve(cid, max_bytes=4)
    assert publisher.retrieve(cid) == b"photo"


def test_recuperar_un_cid_desconocido_falla(tmp_path):
    publicador = LocalEvidencePublisher(root=str(tmp_path))
    with pytest.raises(IpfsError):
        publicador.retrieve("bafkreifzjut3te2nhyekklss27nh3k72ysco7y32koao5eei66wof36n5e")

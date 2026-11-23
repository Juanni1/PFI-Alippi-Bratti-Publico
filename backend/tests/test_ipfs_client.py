"""Tests del adaptador «Cliente IPFS», con transporte simulado de httpx."""

import httpx
import pytest

from app.adaptadores.ipfs_client import PINATA_UPLOAD_URL, IpfsClient, IpfsError

FAKE_CID = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
OTHER_CID = "bafybeihdwdcefgh4dqkjv67uzcmw7ojee6xedzdetojuzjevtenxquvyku"


def make_client(handler) -> IpfsClient:
    return IpfsClient(
        kubo_api_url="http://kubo.railway.internal:5001",
        pinata_jwt="jwt-de-prueba",
        transport=httpx.MockTransport(handler),
    )


def test_retrieve_default_preserves_request_and_transport_errors():
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v0/cat"
        assert dict(request.url.params) == {"arg": FAKE_CID}
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"original")

    assert make_client(handler).retrieve(FAKE_CID) == b"original"
    with pytest.raises(httpx.ConnectError, match="original transport"):
        make_client(lambda request: (_ for _ in ()).throw(
            httpx.ConnectError("original transport")
        )).retrieve(FAKE_CID)
    with pytest.raises(IpfsError, match=f"Kubo respondió 503 al recuperar {FAKE_CID}"):
        make_client(lambda request: httpx.Response(503)).retrieve(FAKE_CID)


@pytest.mark.parametrize("content,limit,overflow", [(b"photo", 5, False), (b"photos", 5, True)])
def test_bounded_retrieve(content, limit, overflow):
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for byte in content:
                yield bytes([byte])
            if overflow:
                raise AssertionError("leyó más de límite + 1")

    def handler(request):
        assert "authorization" not in request.headers
        return httpx.Response(200, stream=Stream())

    client = make_client(handler)
    if overflow:
        with pytest.raises(IpfsError):
            client.retrieve(FAKE_CID, max_bytes=limit)
    else:
        assert client.retrieve(FAKE_CID, max_bytes=limit) == content


def test_publish_adds_to_kubo_and_replicates_in_pinata():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "kubo.railway.internal":
            assert request.url.path == "/api/v0/add"
            return httpx.Response(200, json={"Hash": FAKE_CID})
        assert str(request.url) == PINATA_UPLOAD_URL
        assert request.headers["Authorization"] == "Bearer jwt-de-prueba"
        assert b"public" in request.read()
        return httpx.Response(200, json={"data": {"cid": FAKE_CID, "size": 19}})

    cid = make_client(handler).publish(b"evidencia de prueba")

    assert cid == FAKE_CID
    assert len(seen) == 2


def test_publish_fails_when_cids_diverge():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "kubo.railway.internal":
            return httpx.Response(200, json={"Hash": FAKE_CID})
        return httpx.Response(200, json={"data": {"cid": OTHER_CID}})

    with pytest.raises(IpfsError, match="divergente"):
        make_client(handler).publish(b"evidencia")


def test_add_fails_without_kubo_url():
    client = IpfsClient(kubo_api_url="", pinata_jwt="jwt")
    with pytest.raises(IpfsError, match="KUBO_API_URL"):
        client.add(b"contenido")


def test_upload_remote_fails_without_jwt():
    client = IpfsClient(kubo_api_url="http://kubo:5001", pinata_jwt="")
    with pytest.raises(IpfsError, match="PINATA_JWT"):
        client.upload_remote(b"contenido")


def test_publish_propagates_remote_upload_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "kubo.railway.internal":
            return httpx.Response(200, json={"Hash": FAKE_CID})
        return httpx.Response(403, json={"error": "sin permiso"})

    with pytest.raises(IpfsError, match="403"):
        make_client(handler).publish(b"evidencia")

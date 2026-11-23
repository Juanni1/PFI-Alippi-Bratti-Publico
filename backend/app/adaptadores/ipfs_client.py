"""Adaptador «Cliente IPFS» del DCOMP.

Publica evidencia en el nodo Kubo propio por su API HTTP y sube los mismos
bytes a Pinata como copia remota, que es lo que sostiene la recuperabilidad
de RNF-20 con el host suspendido. El pin por CID (que el servicio saliera a
buscar el contenido a la red) quedó descartado el 2026-08-15: Pinata y
Filebase lo movieron a planes pagos, y el detalle está en las enmiendas de
adr-012. La subida directa cambia la semántica pero conserva una propiedad
verificable: el adaptador exige que el CID calculado por Pinata coincida
con el del nodo propio, dos implementaciones independientes computando el
mismo identificador sobre los mismos bytes.

Suspensión sin tráfico saliente: cada operación abre su conexión y la cierra
al terminar; este módulo no sondea a Kubo ni mantiene conexiones vivas.
"""

import os

import httpx

DEFAULT_TIMEOUT_SECONDS = 30.0
# API V3 de Pinata. El endpoint legacy `pinFileToIPFS` quedó descartado: las
# claves nuevas se emiten con permisos V3 y el anterior responde
# NO_SCOPES_FOUND. La red pública es la que corresponde, porque la evidencia
# tiene que ser recuperable por terceros.
PINATA_UPLOAD_URL = "https://uploads.pinata.cloud/v3/files"
PINATA_NETWORK = "public"

# Parámetros de troceado del nodo propio. Van explícitos y no por omisión
# porque el CID depende de ellos: los mismos bytes dan
# bafkrei… con hojas crudas y bafybei… sin ellas. La comparación entre el
# CID propio y el del servicio remoto solo tiene sentido si las dos
# implementaciones trocean igual; si el servicio cambiara de criterio, se
# ajusta acá y no en la lógica de publicación.
CID_VERSION = "1"
RAW_LEAVES = "true"


class IpfsError(RuntimeError):
    """Fallo al publicar o replicar contenido."""


class IpfsClient:
    """Cliente de publicación y copia remota de evidencia.

    `kubo_api_url` apunta a la API RPC del nodo propio y `pinata_jwt`
    autentica la subida a Pinata; los dos caen al entorno si no se pasan.
    `transport` existe para inyectar un transporte simulado en los tests.
    """

    def __init__(
        self,
        kubo_api_url: str | None = None,
        pinata_jwt: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.kubo_api_url = (kubo_api_url or os.environ.get("KUBO_API_URL", "")).rstrip("/")
        self.pinata_jwt = pinata_jwt if pinata_jwt is not None else os.environ.get("PINATA_JWT")
        self._transport = transport

    def _client(self) -> httpx.Client:
        # Una conexión por operación; el with del llamador la cierra.
        return httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, transport=self._transport)

    def add(self, content: bytes, filename: str = "evidencia") -> str:
        """Sube `content` al nodo propio, lo deja fijado ahí y devuelve su CID v1."""
        if not self.kubo_api_url:
            raise IpfsError("KUBO_API_URL no configurada")
        with self._client() as client:
            response = client.post(
                f"{self.kubo_api_url}/api/v0/add",
                params={
                    "cid-version": CID_VERSION,
                    "raw-leaves": RAW_LEAVES,
                    "pin": "true",
                },
                files={"file": (filename, content)},
            )
        if response.status_code != 200:
            raise IpfsError(f"Kubo respondió {response.status_code} al agregar contenido")
        return response.json()["Hash"]

    def upload_remote(self, content: bytes, filename: str = "evidencia") -> str:
        """Sube `content` a Pinata y devuelve el CID que el servicio calculó.

        Pinata computa un CID v1 con hojas crudas, que es lo mismo que
        declaran `CID_VERSION` y `RAW_LEAVES` para el nodo propio; sobre esa
        coincidencia se apoya la comprobación cruzada de `publish`.
        """
        if not self.pinata_jwt:
            raise IpfsError("PINATA_JWT no configurada")
        with self._client() as client:
            response = client.post(
                PINATA_UPLOAD_URL,
                headers={"Authorization": f"Bearer {self.pinata_jwt}"},
                files={"file": (filename, content)},
                data={"network": PINATA_NETWORK},
            )
        if response.status_code != 200:
            raise IpfsError(f"Pinata respondió {response.status_code} al subir la copia remota")
        return response.json()["data"]["cid"]

    def publish(self, content: bytes, filename: str = "evidencia") -> str:
        """Publica en el nodo propio, replica en Pinata y devuelve el CID.

        La copia remota es condición del enunciado de RNF-20, no un refuerzo
        opcional: si la subida falla o los CID difieren, la publicación
        completa falla y el llamador decide si reintenta o escala.
        """
        cid = self.add(content, filename=filename)
        remote_cid = self.upload_remote(content, filename=filename)
        if remote_cid != cid:
            raise IpfsError(
                f"CID divergente entre el nodo propio ({cid}) y Pinata ({remote_cid})"
            )
        return cid

    def retrieve(self, cid: str, *, max_bytes: int | None = None) -> bytes:
        """Recupera por su CID el contenido publicado.

        Es la etapa de recuperación del worker: la evidencia no se guarda en
        disco de la aseguradora, así que el procesamiento la trae de vuelta de
        la red por su identificador de contenido. Que el mismo camino sirva
        para el worker y para un tercero es lo que vuelve comprobable la
        ligadura entre la imagen y el vector que la prueba consumió.
        """
        if not self.kubo_api_url:
            raise IpfsError("KUBO_API_URL no configurada")
        if max_bytes is not None:
            if max_bytes <= 0:
                raise IpfsError("límite de recuperación inválido")
            with self._client() as client:
                # Kubo puede ignorar Range: se limita la lectura, no solo la petición.
                with client.stream(
                    "POST", f"{self.kubo_api_url}/api/v0/cat", params={"arg": cid},
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code != 200:
                        raise IpfsError("falló la recuperación acotada")
                    content = bytearray()
                    for chunk in response.iter_raw(chunk_size=max_bytes + 1):
                        content.extend(chunk)
                        if len(content) > max_bytes:
                            raise IpfsError("contenido excede el límite de recuperación")
                    return bytes(content)
        with self._client() as client:
            response = client.post(
                f"{self.kubo_api_url}/api/v0/cat", params={"arg": cid}
            )
        if response.status_code != 200:
            raise IpfsError(f"Kubo respondió {response.status_code} al recuperar {cid}")
        return response.content

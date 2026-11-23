"""Publicador de evidencia sobre el sistema de archivos, para correr sin Kubo.

Existe para que la aplicación se pueda levantar entera en una máquina sin nodo
IPFS: sirve para desarrollo y para ensayar la demo, no para la demo misma.

Lo que sí conserva es el identificador. El CID no se inventa: se calcula con el
mismo esquema que usa el nodo propio (`CID_VERSION=1`, `RAW_LEAVES=true`), de
modo que los mismos bytes producen acá el mismo identificador que produciría
Kubo. Un identificador inventado habría convertido la traza pública de MK-08 en
una promesa falsa; así, lo único que falta es la publicación.

**Limitación declarada.** La equivalencia vale mientras el contenido entre en un
solo bloque. Kubo trocea a partir de 256 KB y arma un nodo dag-pb cuyo CID no es
este, así que por encima de ese tamaño los dos identificadores divergen. El
publicador lo detecta y falla en lugar de devolver un CID que no corresponde.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from app.adaptadores.ipfs_client import IpfsError

# Kubo trocea a partir de este tamaño; por encima, el CID de un solo bloque
# deja de ser el mismo y este publicador no puede sostener la equivalencia.
TAMANO_MAXIMO_DE_BLOQUE = 256 * 1024

# Prefijo binario de un CID v1 con códec `raw` y multihash sha2-256:
# 0x01 versión, 0x55 códec raw, 0x12 sha2-256, 0x20 largo del digesto.
PREFIJO_CID_V1_RAW = bytes([0x01, 0x55, 0x12, 0x20])


def compute_cid(content: bytes) -> str:
    """CID v1 con hojas crudas, en base32, tal como lo computa Kubo."""
    if len(content) > TAMANO_MAXIMO_DE_BLOQUE:
        raise IpfsError(
            f"{len(content)} B superan el bloque de {TAMANO_MAXIMO_DE_BLOQUE} B: "
            "el CID de Kubo sería el de un nodo dag-pb y este publicador no lo "
            "puede calcular"
        )
    crudo = PREFIJO_CID_V1_RAW + hashlib.sha256(content).digest()
    # Multibase base32 en minúsculas y sin relleno; la 'b' inicial es el código
    # de base32 y por eso todo CID v1 en esta forma empieza con esa letra.
    return "b" + base64.b32encode(crudo).decode("ascii").lower().rstrip("=")


class LocalEvidencePublisher:
    """Guarda cada contenido en un archivo nombrado por su CID."""

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or os.environ.get("RUTA_EVIDENCIA_LOCAL", "datos/evidencia"))
        self.root.mkdir(parents=True, exist_ok=True)

    def publish(self, content: bytes, filename: str = "evidencia") -> str:
        cid = compute_cid(content)
        (self.root / cid).write_bytes(content)
        return cid

    def retrieve(self, cid: str, *, max_bytes: int | None = None) -> bytes:
        ruta = self.root / cid
        if not ruta.exists():
            raise IpfsError(f"no se pudo recuperar {cid} del almacenamiento local")
        if max_bytes is None:
            return ruta.read_bytes()
        if max_bytes <= 0:
            raise IpfsError("límite de recuperación inválido")
        with ruta.open("rb") as source:
            content = source.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise IpfsError("contenido excede el límite de recuperación")
        return content


def build_evidence_publisher():
    """Elige el publicador por entorno.

    Por defecto va el real: que la evidencia sea recuperable por un tercero es
    una propiedad del sistema y no una comodidad, así que caer al local tiene
    que ser una decisión explícita de quien levanta el servicio.
    """
    from app.adaptadores.ipfs_client import IpfsClient

    if os.environ.get("PUBLICADOR_EVIDENCIA", "ipfs").lower() == "local":
        return LocalEvidencePublisher()
    return IpfsClient()

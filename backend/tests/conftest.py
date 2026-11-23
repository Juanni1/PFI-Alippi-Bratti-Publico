"""Arnés compartido: aplicación sobre base temporal y publicador de mentira.

El publicador falso evita que la suite dependa de la red, y a la vez conserva
la propiedad que importa del real: devuelve un identificador determinista por
contenido, de modo que los mismos bytes dan el mismo CID.
"""

import hashlib
import os
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.adaptadores.database import (
    create_database_engine,
    create_session_factory,
    upgrade_schema,
)
from app.dominio.claims import UploadedFile, register_claim
from app.main import app as aplicacion
PIXEL_PNG = b"\x89PNG\r\n\x1a\n" + b"contenido de prueba"
DAMAGE_DESCRIPTION = "El paragolpes quedó abollado."


class PublicadorFalso:
    """Satisface el puerto `EvidencePublisher` sin salir a la red."""

    def __init__(self) -> None:
        self.publicados = []
        self.contenido: dict[str, bytes] = {}

    def publish(self, content: bytes, filename: str = "evidencia") -> str:
        cid = "bafkrei" + hashlib.sha256(content).hexdigest()[:52]
        self.publicados.append(cid)
        self.contenido[cid] = content
        return cid

    def retrieve(self, cid: str, *, max_bytes: int | None = None) -> bytes:
        if cid not in self.contenido:
            from app.adaptadores.ipfs_client import IpfsError

            raise IpfsError(f"no se pudo recuperar {cid}")
        content = self.contenido[cid]
        if max_bytes is not None and len(content) > max_bytes:
            from app.adaptadores.ipfs_client import IpfsError

            raise IpfsError("contenido excede el límite")
        return content


class PublicadorQueFalla:
    def publish(self, content: bytes, filename: str = "evidencia") -> str:
        from app.adaptadores.ipfs_client import IpfsError

        raise IpfsError("el nodo no respondió")

    def retrieve(self, cid: str, *, max_bytes: int | None = None) -> bytes:
        from app.adaptadores.ipfs_client import IpfsError

        raise IpfsError("el nodo no respondió")




@pytest.fixture()
def postgres_unmigrated_url():
    raw_url = os.environ.get("TEST_DATABASE_URL")
    if not raw_url:
        raise RuntimeError(
            "TEST_DATABASE_URL no está configurada. Los tests requieren una base de datos PostgreSQL."
        )

    base_url = raw_url
    schema_name = f"test_{uuid.uuid4().hex[:12]}"

    admin_engine = create_database_engine(base_url).execution_options(
        isolation_level="AUTOCOMMIT"
    )
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    u = make_url(base_url)
    options = f"{u.query.get('options', '')} -csearch_path={schema_name}".strip()
    schema_url = u.set(query={**u.query, "options": options}).render_as_string(
        hide_password=False
    )

    try:
        yield schema_url
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
        admin_engine.dispose()


@pytest.fixture()
def postgres_url(postgres_unmigrated_url, monkeypatch):
    schema_url = postgres_unmigrated_url
    engine = create_database_engine(schema_url)
    upgrade_schema(engine)
    engine.dispose()

    monkeypatch.setenv("DATABASE_URL", schema_url)
    return schema_url


@pytest.fixture()
def sesiones(postgres_url):
    motor = create_database_engine(postgres_url)
    factory = create_session_factory(motor)
    yield factory
    motor.dispose()

@pytest.fixture()
def publicador() -> PublicadorFalso:
    return PublicadorFalso()


@pytest.fixture()
def cliente(sesiones, publicador):
    """Cliente HTTP contra la aplicación real, con la base y el publicador
    reemplazados. No se toca ninguna ruta: lo que se sustituye es el estado."""
    from app.queue.celery_app import celery_app
    from app.queue.worker_context import WorkerContext, set_worker_context

    class _AppStateWorkerContext(WorkerContext):
        def __init__(self, app_ref):
            self._app = app_ref

        @property
        def session_factory(self):
            return self._app.state.session_factory

        @property
        def publisher(self):
            return self._app.state.publisher

        @property
        def inference_engine(self):
            return self._app.state.inference_engine

        @property
        def proof_generator(self):
            return self._app.state.proof_generator

    prev_eager = celery_app.conf.task_always_eager
    celery_app.conf.task_always_eager = True
    set_worker_context(_AppStateWorkerContext(aplicacion))

    aplicacion.state.session_factory = sesiones
    aplicacion.state.publisher = publicador
    try:
        with TestClient(aplicacion) as cliente:
            # El `lifespan` vuelve a fijar el estado con los adaptadores reales, así
            # que se reemplaza otra vez una vez arrancado.
            aplicacion.state.session_factory = sesiones
            aplicacion.state.publisher = publicador
            yield cliente
    finally:
        celery_app.conf.task_always_eager = prev_eager
        set_worker_context(None)


@pytest.fixture()
def archivos() -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("photos", ("frente.png", PIXEL_PNG, "image/png"))]


@pytest.fixture()
def reclamo_registrado(sesiones, publicador):
    """Un reclamo ya creado, con su token en claro."""
    from app.adaptadores.database import session_scope

    with session_scope(sesiones) as sesion:
        registrado = register_claim(
            sesion,
            publicador,
            damage_description=DAMAGE_DESCRIPTION,
            files=[UploadedFile("frente.png", "image/png", PIXEL_PNG)],
            declared_amount=Decimal("185000.00"),
        )
        return {
            "number": registrado.claim.tracking_number,
            "token": registrado.access_token,
            "id": registrado.claim.id,
        }

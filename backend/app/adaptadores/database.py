"""Conexión a PostgreSQL, ciclo de vida de la sesión y migraciones de esquema.

Se utiliza PostgreSQL con el dialecto `postgresql+psycopg` (psycopg 3) sin
fallback a SQLite ni sistemas de archivos locales. La URL entra por
`DATABASE_URL` (RNF-29). Todas las sesiones fuerzan el huso horario en UTC.

Las migraciones de esquema se gestionan exclusivamente con Alembic. Al iniciar
la aplicación, `verify_schema` comprueba que la base de datos se encuentre en
la revisión vigente (`head`); el arranque falla de inmediato si la base no está
migrada. La aplicación de migraciones se realiza de forma explícita mediante
`upgrade_schema` o el CLI de Alembic.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import Session, sessionmaker


def _normalize_database_url(url: str) -> str:
    """Normaliza la URL de base de datos para asegurar el dialecto psycopg 3.

    Rechaza esquemas SQLite o ausentes: PostgreSQL es obligatorio.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+psycopg://"):
        return url
    raise ValueError("DATABASE_URL debe usar PostgreSQL con el dialecto psycopg.")




def create_database_engine(url: str | None = None) -> Engine:
    """Crea el motor de base de datos PostgreSQL.

    Cada conexión usa UTC; el pool no conserva sockets salientes al cerrar
    la sesión. Las opciones de conexión se transmiten a psycopg sin reinterpretarlas.
    """
    if url is None:
        url_env = os.environ.get("DATABASE_URL")
        if not url_env:
            raise RuntimeError(
                "DATABASE_URL es obligatoria y debe ser una URL de PostgreSQL"
            )
        url = url_env

    normalized_url = _normalize_database_url(url)

    motor = create_engine(normalized_url, future=True, poolclass=NullPool)

    @event.listens_for(motor, "connect")
    def _configure_connection(dbapi_connection, _record):  # noqa: ANN001
        # SET SESSION debe sobrevivir al rollback de inicialización del dialecto.
        autocommit = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        try:
            with dbapi_connection.cursor() as cursor:
                cursor.execute("SET TIME ZONE 'UTC'")
        finally:
            dbapi_connection.autocommit = autocommit

    return motor


def _alembic_config() -> Config:
    project_root = Path(__file__).resolve().parents[2]
    ini_path = project_root / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("script_location", str(project_root / "migrations"))
    return cfg


def upgrade_schema(engine: Engine) -> None:
    """Aplica las migraciones de Alembic hasta la última versión (`head`)."""
    cfg = _alembic_config()
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")


def verify_schema(engine: Engine) -> None:
    """Verifica que la base de datos se encuentre migrada a la última versión.

    Lanza RuntimeError si la base no está migrada o tiene una versión distinta
    a la esperada. La aplicación no arranca sobre una base sin migrar.
    """
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)
    expected_head = script.get_current_head()

    with engine.connect() as connection:
        migration_context = MigrationContext.configure(connection)
        current_rev = migration_context.get_current_revision()

    if current_rev is None:
        raise RuntimeError(
            f"La base de datos no está migrada (revisión actual: None, "
            f"esperada: {expected_head}). Ejecute las migraciones antes de iniciar."
        )

    if current_rev != expected_head:
        raise RuntimeError(
            f"La base de datos no está en la revisión esperada (revisión actual: {current_rev}, "
            f"esperada: {expected_head}). Ejecute las migraciones pendientes."
        )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Sesión transaccional: confirma al salir bien y revierte ante excepción."""
    sesion = factory()
    try:
        yield sesion
        sesion.commit()
    except Exception:
        sesion.rollback()
        raise
    finally:
        sesion.close()

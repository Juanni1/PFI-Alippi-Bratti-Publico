"""Herramientas de migración operativa y preservación de datos para PFI-137.

Permite:
1. Crear un snapshot consistente de SQLite mediante la API de backup (compatible con WAL)
   con permisos 0600, verificación de integridad y publicación atómica sin sobreescritura.
2. Importar dicho snapshot a una base de datos PostgreSQL migrada y vacía:
   - Verificación de revisión de Alembic antes de operar.
   - Bloqueo exclusivo de tablas de destino para evitar condiciones de carrera.
   - Conversión canónica y exacta de tipos (datetimes UTC-aware, Numeric/Decimal exacto, binarios, JSON, enums).
   - Preservación de claves primarias, claves foráneas, identidades y tokens.
   - Preservación de autorreferencias cíclicas e inversas en `claims.origin_claim_id`.
   - Normalización explícita de vocabulario renombrado (`corrosion` -> `fisura`).
   - Manejo estricto de columnas legacy conocidas (`damage_description`, `registered_by`, `evidence.digest_verified`).
   - Rechazo de tablas o columnas desconocidas y datos no mapeables sin filtrar valores sensibles.
   - Transacción todo o nada con validación completa de contenido (no solo conteos).
   - Ajuste transaccional de secuencias (ALTER SEQUENCE RESTART WITH) y semántica de tablas vacías.
   - Salvaguarda estricta de destino vacío.
3. Respaldo y restauración PostgreSQL nativa (pg_dump custom y pg_restore transaccional):
   - Credenciales pasadas únicamente por variables de entorno (nunca en argv).
   - Restauración sin drop/clean destructivo y con guard estricto de destino vacío.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import JSON, Connection, Engine, Text, bindparam, cast, select, text
from sqlalchemy.engine.url import make_url

from app.adaptadores.database import create_database_engine, verify_schema
from app.adaptadores.models import Base
from app.dominio.vocabulary import DamageClass

_log = logging.getLogger("app.data_migration")

TABLES_ORDER: tuple[str, ...] = (
    "models",
    "configurations",
    "claims",
    "evidence",
    "inferences",
    "proofs",
    "jobs",
    "transitions",
    "reviews",
)

KNOWN_LEGACY_NULLABLE_COLUMNS: frozenset[tuple[str, str]] = frozenset(
    {
        ("claims", "damage_description"),
        ("claims", "registered_by"),
        ("evidence", "digest_verified"),
        ("claims", "pending_chain_action"),
        ("jobs", "current_stage"),
        ("jobs", "configuration_id"),
        ("jobs", "inference_id"),
        ("jobs", "proof_id"),
        ("jobs", "checkpoint_context"),
        ("jobs", "worker_id"),
        ("jobs", "fence_token"),
        ("jobs", "lease_expires_at"),
        ("jobs", "heartbeat_at"),
        ("jobs", "available_at"),
    }
)


class DataMigrationError(Exception):
    """Error base en los procedimientos de migración de datos."""


class MigrationTargetNotEmptyError(DataMigrationError):
    """Se rechazó la operación porque la base o tabla de destino no está vacía."""


class DataMigrationSchemaError(DataMigrationError):
    """Discrepancia estructural en las tablas o columnas de origen/destino."""


class MigrationValidationError(DataMigrationError):
    """Fallo en la validación de contenido entre origen y destino."""


class PostgresToolError(DataMigrationError):
    """Error al ejecutar herramientas nativas de PostgreSQL (pg_dump/pg_restore)."""


def _reject_json_constant(c: str) -> Any:
    raise MigrationValidationError(f"Constante no válida en JSON: '{c}'")


def canonicalize_value(
    table_name: str,
    col_name: str,
    val: Any,
    col_type: Any,
    nullable: bool,
    *,
    is_encoded: bool = False,
    transformations: dict[str, int] | None = None,
) -> Any:
    """Convierte y valida un valor de forma determinista para importación y comparación."""
    if val is None:
        if (table_name, col_name) == ("jobs", "fence_token"):
            return 0
        # psycopg decodifica el literal JSON null como None; no es un SQL NULL.
        if isinstance(col_type, JSON) and not is_encoded:
            return None
        if not nullable:
            raise MigrationValidationError(
                f"Valor SQL NULL en columna obligatoria: {table_name}.{col_name}"
            )
        return None

    # Regla histórica CarDD: renombre de corrosion a fisura
    if table_name == "inferences" and col_name == "predicted_class":
        v_str = val.value if hasattr(val, "value") else str(val)
        if v_str == "corrosion":
            if transformations is not None:
                transformations["inferences.predicted_class corrosion -> fisura"] = (
                    transformations.get("inferences.predicted_class corrosion -> fisura", 0) + 1
                )
            return DamageClass.FISURA.value

    type_cls_name = type(col_type).__name__

    # 1. Integer
    if type_cls_name in ("Integer", "SmallInteger", "BigInteger"):
        if isinstance(val, bool):
            raise MigrationValidationError(f"Booleano inválido como entero en {table_name}.{col_name}")
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            try:
                return int(val.strip())
            except ValueError:
                raise MigrationValidationError(f"Texto entero inválido en {table_name}.{col_name}")
        if isinstance(val, float):
            if val.is_integer():
                return int(val)
            raise MigrationValidationError(f"Flotante no entero en {table_name}.{col_name}")
        raise MigrationValidationError(f"Tipo entero inválido en {table_name}.{col_name}")

    # 2. Boolean
    if type_cls_name == "Boolean":
        if isinstance(val, bool):
            return val
        if isinstance(val, int):
            if val == 1:
                return True
            if val == 0:
                return False
            raise MigrationValidationError(f"Entero booleano inválido en {table_name}.{col_name} (debe ser 0 o 1)")
        raise MigrationValidationError(f"Tipo booleano inválido en {table_name}.{col_name}")

    # 3. Numeric / Decimal
    if type_cls_name in ("Numeric", "DECIMAL"):
        if isinstance(val, Decimal):
            d = val
        elif isinstance(val, (int, str)):
            try:
                d = Decimal(str(val).strip())
            except Exception:
                raise MigrationValidationError(f"Decimal inválido en {table_name}.{col_name}")
        elif isinstance(val, float):
            if math.isnan(val) or math.isinf(val):
                raise MigrationValidationError(f"Flotante no finito para Decimal en {table_name}.{col_name}")
            d = Decimal(str(val))
        else:
            raise MigrationValidationError(f"Tipo Decimal inválido en {table_name}.{col_name}")

        if not d.is_finite():
            raise MigrationValidationError(f"Decimal no finito en {table_name}.{col_name}")

        scale = getattr(col_type, "scale", None)
        if scale is not None:
            quantized = d.quantize(Decimal(10) ** -scale)
            if d != quantized:
                raise MigrationValidationError(
                    f"Decimal excede la escala permitida ({scale} decimales) en {table_name}.{col_name}"
                )
            d = quantized
        return d

    # 4. Float
    if type_cls_name in ("Float", "Double"):
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            f = float(val)
            if math.isnan(f) or math.isinf(f):
                raise MigrationValidationError(f"Flotante no finito en {table_name}.{col_name}")
            return f
        if isinstance(val, str):
            try:
                f = float(val.strip())
                if math.isnan(f) or math.isinf(f):
                    raise MigrationValidationError(f"Flotante no finito en {table_name}.{col_name}")
                return f
            except ValueError:
                raise MigrationValidationError(f"Flotante inválido en {table_name}.{col_name}")
        raise MigrationValidationError(f"Tipo flotante inválido en {table_name}.{col_name}")

    # 5. DateTime
    if type_cls_name == "DateTime":
        if isinstance(val, datetime):
            if val.tzinfo is None:
                return val.replace(tzinfo=timezone.utc)
            return val.astimezone(timezone.utc)
        if isinstance(val, str):
            clean = val.strip()
            if not clean:
                raise MigrationValidationError(f"Datetime vacío en {table_name}.{col_name}")
            try:
                dt = datetime.fromisoformat(clean.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except ValueError:
                raise MigrationValidationError(
                    f"Formato datetime inválido en {table_name}.{col_name}"
                ) from None
        raise MigrationValidationError(f"Tipo datetime inválido en {table_name}.{col_name}")

    # 6. LargeBinary
    if type_cls_name == "LargeBinary":
        if isinstance(val, (bytes, bytearray)):
            return bytes(val)
        if isinstance(val, memoryview):
            return val.tobytes()
        raise MigrationValidationError(f"Tipo binario inválido en {table_name}.{col_name}")

    # 7. JSON
    if type_cls_name == "JSON":
        if is_encoded:
            # SQLite da afinidad NUMERIC a JSON y puede devolver números sin texto.
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                if not math.isfinite(val):
                    raise MigrationValidationError(f"JSON inválido en {table_name}.{col_name}")
                return Decimal(str(val)) if isinstance(val, float) else val
            if isinstance(val, (bytes, bytearray)):
                try:
                    val = val.decode("utf-8")
                except Exception:
                    raise MigrationValidationError(f"Bytes no decodificables para JSON en {table_name}.{col_name}")
            if not isinstance(val, str):
                raise MigrationValidationError(f"Tipo JSON no serializado en SQLite en {table_name}.{col_name}")
            try:
                return json.loads(
                    val, parse_float=Decimal, parse_constant=_reject_json_constant
                )
            except Exception:
                raise MigrationValidationError(f"JSON inválido en {table_name}.{col_name}")
        else:
            # Ya decodificado por PostgreSQL (objeto, arreglo, string, escalar o null)
            return val

    # 8. Enum o String
    enum_class = getattr(col_type, "enum_class", None)
    if enum_class is not None:
        v_str = val.value if hasattr(val, "value") else str(val)
        valid_vals = {e.value for e in enum_class}
        if v_str in valid_vals:
            return v_str
        raise MigrationValidationError(
            f"Valor enum inválido en {table_name}.{col_name}: no pertenece a {enum_class.__name__}"
        )

    # String / Text
    if not isinstance(val, str):
        raise MigrationValidationError(f"Tipo de texto inválido en {table_name}.{col_name}")
    v_str = val
    max_len = getattr(col_type, "length", None)
    if max_len is not None and len(v_str) > max_len:
        raise MigrationValidationError(
            f"Texto excede longitud máxima en {table_name}.{col_name} (máximo {max_len})"
        )
    return v_str


def compare_canonical_values(val_a: Any, val_b: Any) -> bool:
    """Compara dos valores canónicos con exactitud matemática y de contenido."""
    if val_a is None or val_b is None:
        return val_a is val_b
    return val_a == val_b


def snapshot_sqlite(source_path: str | Path, target_path: str | Path) -> Path:
    """Crea un snapshot atómico consistente de SQLite (0600) con verificación de integridad."""
    src = Path(source_path).resolve()
    dst = Path(target_path).resolve()

    if not src.is_file():
        raise FileNotFoundError(f"El archivo SQLite de origen no existe: {source_path}")

    try:
        if src.samefile(dst):
            raise ValueError("Las rutas de origen y destino del snapshot no pueden ser el mismo archivo")
    except FileNotFoundError:
        pass

    if dst.exists():
        raise FileExistsError(f"El archivo de destino ya existe: {target_path}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_file = tempfile.mkstemp(dir=dst.parent, prefix=f".tmp.{dst.name}.")
    os.close(fd)
    tmp_path = Path(tmp_file)

    src_uri = f"{src.as_uri()}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    try:
        src_conn.execute("BEGIN DEFERRED")
        dst_conn = sqlite3.connect(str(tmp_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()

        verify_uri = f"{tmp_path.as_uri()}?mode=ro"
        verify_conn = sqlite3.connect(verify_uri, uri=True)
        try:
            cur = verify_conn.cursor()
            if cur.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise DataMigrationError("Fallo en la verificación de integridad del snapshot SQLite")
            if cur.execute("PRAGMA foreign_key_check").fetchall():
                raise DataMigrationError("Fallo en la verificación de claves foráneas del snapshot SQLite")
        finally:
            verify_conn.close()

        try:
            os.link(tmp_path, dst)
        except FileExistsError:
            raise FileExistsError(f"El archivo de destino ya existe: {target_path}") from None

    finally:
        src_conn.close()
        if tmp_path.exists():
            tmp_path.unlink()

    return dst


def import_sqlite_snapshot(
    sqlite_path: str | Path,
    target_engine_or_url: Engine | str | None = None,
) -> dict[str, int]:
    """Importa el snapshot SQLite a PostgreSQL con verificación Alembic y transacción atómica."""
    src_path = Path(sqlite_path).resolve()
    if not src_path.is_file():
        raise FileNotFoundError(f"El archivo SQLite no existe: {sqlite_path}")

    target = target_engine_or_url or os.environ.get("DATABASE_URL")
    if not target:
        raise ValueError("Se requiere target_engine o la variable de entorno DATABASE_URL")

    if isinstance(target, str):
        target_engine = create_database_engine(target)
        dispose_engine = True
    else:
        target_engine = target
        dispose_engine = False

    transformations: dict[str, int] = {}

    try:
        verify_schema(target_engine)

        src_uri = f"{src_path.as_uri()}?mode=ro"
        sqlite_conn = sqlite3.connect(src_uri, uri=True)
        try:
            sqlite_conn.execute("BEGIN DEFERRED")
            cur = sqlite_conn.cursor()
            if cur.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise DataMigrationError("Integrity check falló en el archivo SQLite origen")
            if cur.execute("PRAGMA foreign_key_check").fetchall():
                raise DataMigrationError("Foreign key check falló en el archivo SQLite origen")

            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")
            user_tables = {r[0] for r in cur.fetchall()}
            expected_tables = set(TABLES_ORDER)
            if user_tables != expected_tables:
                unknown = user_tables - expected_tables
                missing = expected_tables - user_tables
                if unknown:
                    raise DataMigrationSchemaError(f"Tablas desconocidas en SQLite: {sorted(unknown)}")
                if missing:
                    raise DataMigrationSchemaError(f"Tablas obligatorias faltantes en SQLite: {sorted(missing)}")

            with target_engine.begin() as pg_conn:
                lock_tables_sql = ", ".join(TABLES_ORDER)
                pg_conn.execute(text(f"LOCK TABLE {lock_tables_sql} IN ACCESS EXCLUSIVE MODE"))

                for t in TABLES_ORDER:
                    if pg_conn.execute(text(f"SELECT 1 FROM {t} LIMIT 1")).first():
                        raise MigrationTargetNotEmptyError(
                            f"La tabla de destino '{t}' contiene datos. La migración requiere destino vacío."
                        )

                parsed_data: dict[str, list[dict[str, Any]]] = {}
                origin_claim_updates: list[tuple[int, int]] = []

                for table_name in TABLES_ORDER:
                    table_obj = Base.metadata.tables[table_name]
                    cur.execute(f"SELECT * FROM {table_name} ORDER BY id ASC")
                    desc = cur.description or []
                    col_names = [d[0] for d in desc]
                    rows = cur.fetchall()

                    expected_cols = set(table_obj.columns.keys())
                    sqlite_cols = set(col_names)
                    unknown_cols = sqlite_cols - expected_cols
                    if unknown_cols:
                        raise DataMigrationSchemaError(
                            f"Columna desconocida en tabla '{table_name}': {sorted(unknown_cols)}"
                        )

                    missing_cols = expected_cols - sqlite_cols
                    for missing in missing_cols:
                        if (table_name, missing) not in KNOWN_LEGACY_NULLABLE_COLUMNS:
                            raise DataMigrationSchemaError(
                                f"Columna requerida ausente en '{table_name}': '{missing}'"
                            )
                        if rows:
                            if (table_name, missing) == ("jobs", "fence_token"):
                                transformations[f"missing_column:{table_name}.{missing} -> 0"] = len(rows)
                            else:
                                transformations[f"missing_column:{table_name}.{missing} -> NULL"] = len(rows)

                    table_rows: list[dict[str, Any]] = []
                    for row in rows:
                        raw = dict(zip(col_names, row))
                        parsed_row: dict[str, Any] = {}
                        for col in table_obj.columns:
                            val = raw.get(col.name)
                            c_val = canonicalize_value(
                                table_name,
                                col.name,
                                val,
                                col.type,
                                col.nullable,
                                is_encoded=True,
                                transformations=transformations,
                            )
                            if isinstance(col.type, JSON):
                                # Se transmite el JSON original: cargarlo como float perdería dígitos.
                                if val is None:
                                    # NULL SQL no equivale al literal JSON null.
                                    parsed_row[col.name] = None
                                elif isinstance(val, (bytes, bytearray)):
                                    parsed_row[col.name] = val.decode("utf-8")
                                elif isinstance(val, str):
                                    parsed_row[col.name] = val
                                else:
                                    parsed_row[col.name] = json.dumps(val, allow_nan=False)
                            else:
                                parsed_row[col.name] = c_val

                        if table_name == "claims":
                            orig_id = parsed_row.get("origin_claim_id")
                            if orig_id is not None:
                                origin_claim_updates.append((parsed_row["id"], orig_id))
                                parsed_row["origin_claim_id"] = None

                        table_rows.append(parsed_row)

                    parsed_data[table_name] = table_rows

                for table_name in TABLES_ORDER:
                    rows_to_insert = parsed_data[table_name]
                    if rows_to_insert:
                        table = Base.metadata.tables[table_name]
                        json_bindings = {
                            col.name: cast(bindparam(col.name, type_=Text()), JSON)
                            for col in table.columns
                            if isinstance(col.type, JSON)
                        }
                        statement = table.insert().values(**json_bindings)
                        pg_conn.execute(statement, rows_to_insert)

                if origin_claim_updates:
                    for claim_id, orig_id in origin_claim_updates:
                        pg_conn.execute(
                            text("UPDATE claims SET origin_claim_id = :orig_id WHERE id = :id"),
                            {"orig_id": orig_id, "id": claim_id},
                        )

                validate_imported_content(sqlite_conn, pg_conn)
                set_integer_sequences(pg_conn)

        finally:
            sqlite_conn.close()

    finally:
        if dispose_engine:
            target_engine.dispose()

    return transformations


def validate_imported_content(sqlite_conn: sqlite3.Connection, pg_conn: Connection) -> None:
    """Valida exhaustivamente todo el contenido fila por fila y columna por columna."""
    cur = sqlite_conn.cursor()
    for table_name in TABLES_ORDER:
        table_obj = Base.metadata.tables[table_name]
        cur.execute(f"SELECT * FROM {table_name} ORDER BY id ASC")
        desc = cur.description or []
        col_names = [d[0] for d in desc]
        s_rows = cur.fetchall()

        columns = [
            cast(col, Text()).label(col.name) if isinstance(col.type, JSON) else col
            for col in table_obj.columns
        ]
        p_rows = pg_conn.execute(
            select(*columns).order_by(table_obj.c.id)
        ).mappings().all()

        if len(s_rows) != len(p_rows):
            raise MigrationValidationError(
                f"Discrepancia en cantidad de filas en '{table_name}': "
                f"SQLite tiene {len(s_rows)}, PostgreSQL tiene {len(p_rows)}"
            )

        for idx, s_row in enumerate(s_rows):
            s_dict = dict(zip(col_names, s_row))
            p_dict = p_rows[idx]
            row_id = p_dict["id"]

            for col in table_obj.columns:
                s_raw = s_dict.get(col.name)
                p_raw = p_dict[col.name]

                if isinstance(col.type, JSON) and (s_raw is None) != (p_raw is None):
                    raise MigrationValidationError(
                        f"Discrepancia de nulabilidad SQL en {table_name}.{col.name}"
                    )

                s_val = canonicalize_value(table_name, col.name, s_raw, col.type, col.nullable, is_encoded=True)
                p_val = canonicalize_value(
                    table_name, col.name, p_raw, col.type, col.nullable,
                    is_encoded=isinstance(col.type, JSON),
                )

                if not compare_canonical_values(s_val, p_val):
                    raise MigrationValidationError(
                        f"Discrepancia de contenido en {table_name}.{col.name} (id={row_id})"
                    )


def set_integer_sequences(conn: Connection) -> list[tuple[str, int]]:
    """Ajusta transaccionalmente las secuencias de las nueve entidades en PostgreSQL."""
    results: list[tuple[str, int]] = []
    preparer = conn.dialect.identifier_preparer

    for table_name in TABLES_ORDER:
        max_id = conn.execute(text(f"SELECT MAX(id) FROM {table_name}")).scalar()
        next_val = max(1, (int(max_id) if max_id is not None else 0) + 1)

        seq_info = conn.execute(
            text("""
                SELECT s.relname AS seq_name, n.nspname AS seq_schema
                FROM pg_class t
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attname = 'id'
                JOIN pg_depend d ON d.refobjid = t.oid AND d.refobjsubid = a.attnum AND d.deptype IN ('a', 'i')
                JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
                JOIN pg_namespace n ON n.oid = s.relnamespace
                WHERE t.oid = to_regclass(:t)
            """),
            {"t": table_name},
        ).mappings().first()

        if not seq_info:
            raise DataMigrationError(
                f"No se encontró secuencia serial/identity en el catálogo para la tabla '{table_name}'"
            )

        quoted_seq = f"{preparer.quote(seq_info['seq_schema'])}.{preparer.quote(seq_info['seq_name'])}"
        conn.execute(text(f"ALTER SEQUENCE {quoted_seq} RESTART WITH {next_val}"))
        results.append((table_name, next_val))

    return results


def _extract_libpq_env(url_str: str) -> tuple[dict[str, str], str]:
    parsed = make_url(url_str)
    if not (parsed.drivername.startswith("postgres") or parsed.drivername.startswith("postgresql")):
        raise ValueError(f"DATABASE_URL debe ser postgresql://: {parsed.drivername}")

    env = os.environ.copy()
    env.pop("PGHOSTADDR", None)
    env.pop("PGSERVICE", None)
    env.pop("PGSERVICEFILE", None)
    env.pop("PGOPTIONS", None)

    if parsed.host:
        env["PGHOST"] = parsed.host
    if parsed.port:
        env["PGPORT"] = str(parsed.port)
    if parsed.database:
        env["PGDATABASE"] = parsed.database
    if parsed.username:
        env["PGUSER"] = parsed.username
    if parsed.password:
        env["PGPASSWORD"] = parsed.password

    ssl_env_map = {
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
    }
    for k, v in parsed.query.items():
        if k not in ssl_env_map:
            raise ValueError(f"Opción de conexión no soportada en DATABASE_URL: '{k}'")
        env[ssl_env_map[k]] = v

    return env, parsed.database or ""


def _sanitize_credentials(text_msg: str) -> str:
    if not text_msg:
        return text_msg
    sanitized = re.sub(r"://([^:]+):([^@]+)@", r"://\1:******@", text_msg)
    sanitized = re.sub(r"password=([^\s]+)", r"password=******", sanitized)
    return sanitized


def backup_postgres(output_path: str | Path) -> Path:
    """Genera un respaldo atómico en formato custom (-Fc) con pg_dump."""
    url_str = os.environ.get("DATABASE_URL")
    if not url_str:
        raise ValueError("Se requiere la variable de entorno DATABASE_URL")

    out = Path(output_path).resolve()
    if out.exists():
        raise FileExistsError(f"El archivo de backup ya existe: {out}")

    if not shutil.which("pg_dump"):
        raise PostgresToolError("Cliente 'pg_dump' no encontrado en PATH. Instale postgresql-client (versión 17).")

    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_file = tempfile.mkstemp(dir=out.parent, prefix=f".tmp.{out.name}.")
    os.close(fd)
    tmp_path = Path(tmp_file)

    env, _ = _extract_libpq_env(url_str)
    try:
        cmd = ["pg_dump", "--format=custom", "--file", str(tmp_path)]
        result = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            err_msg = _sanitize_credentials(result.stderr)
            raise PostgresToolError(f"pg_dump falló (código {result.returncode}): {err_msg}")

        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            raise PostgresToolError("pg_dump produjo un archivo de salida vacío")

        try:
            os.link(tmp_path, out)
        except FileExistsError:
            raise FileExistsError(f"El archivo de backup ya existe: {out}") from None

    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    return out


def restore_postgres(input_path: str | Path) -> None:
    """Restaura un respaldo de PostgreSQL en base estrictamente vacía."""
    url_str = os.environ.get("DATABASE_URL")
    if not url_str:
        raise ValueError("Se requiere la variable de entorno DATABASE_URL")

    inp = Path(input_path).resolve()
    if not inp.is_file():
        raise FileNotFoundError(f"Archivo de backup no encontrado: {inp}")

    if not shutil.which("pg_restore"):
        raise PostgresToolError("Cliente 'pg_restore' no encontrado en PATH. Instale postgresql-client (versión 17).")

    engine = create_database_engine(url_str)
    try:
        with engine.connect() as conn:
            user_tables_count = conn.execute(
                text("SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema')")
            ).scalar()
            if user_tables_count and user_tables_count > 0:
                raise MigrationTargetNotEmptyError(
                    f"El PostgreSQL de destino contiene {user_tables_count} tabla(s) existente(s). "
                    "pg_restore requiere una base de datos vacía."
                )
    finally:
        engine.dispose()

    env, db_name = _extract_libpq_env(url_str)
    cmd = [
        "pg_restore",
        "--exit-on-error",
        "--single-transaction",
        "--no-owner",
        "-d",
        db_name,
        str(inp),
    ]

    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        err_msg = _sanitize_credentials(result.stderr)
        raise PostgresToolError(f"pg_restore falló (código {result.returncode}): {err_msg}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.adaptadores.data_migration",
        description="Herramientas operativas de migración y preservación de datos (SQLite -> PostgreSQL, backup, restore).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snap_p = subparsers.add_parser("snapshot-sqlite", help="Snapshot consistente de SQLite.")
    snap_p.add_argument("--source", required=True, help="Ruta al archivo SQLite de origen.")
    snap_p.add_argument("--target", required=True, help="Ruta de destino para el snapshot.")

    imp_p = subparsers.add_parser("import-sqlite", help="Importa snapshot SQLite a PostgreSQL vacío.")
    imp_p.add_argument("--source", required=True, help="Ruta al archivo snapshot de SQLite.")

    back_p = subparsers.add_parser("backup-postgres", help="Genera backup PostgreSQL con pg_dump.")
    back_p.add_argument("--output", required=True, help="Ruta del archivo de salida (.dump).")

    rest_p = subparsers.add_parser("restore-postgres", help="Restaura backup PostgreSQL en base vacía.")
    rest_p.add_argument("--input", required=True, help="Ruta del archivo de backup (.dump).")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "snapshot-sqlite":
            dst = snapshot_sqlite(args.source, args.target)
            print(f"Snapshot SQLite creado exitosamente: {dst}")
            return 0

        if args.command == "import-sqlite":
            transformations = import_sqlite_snapshot(args.source)
            print("Importación a PostgreSQL completada con éxito.")
            if transformations:
                print("Transformaciones aplicadas:")
                for k, v in transformations.items():
                    print(f"  - {k}: {v}")
            return 0

        if args.command == "backup-postgres":
            out = backup_postgres(args.output)
            print(f"Backup creado exitosamente: {out}")
            return 0

        if args.command == "restore-postgres":
            restore_postgres(args.input)
            print("Restauración completada con éxito.")
            return 0

        parser.print_help(file=sys.stderr)
        return 1

    except DataMigrationError as exc:
        print(f"Error de migración: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        msg = _sanitize_credentials(str(exc))
        first_line = msg.splitlines()[0] if msg else "Error desconocido"
        print(f"Error en operación '{args.command}': {type(exc).__name__}: {first_line}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

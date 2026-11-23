"""Pruebas de comportamiento para la migración de datos y operaciones (PFI-137).

Verifica de forma permanente y determinista:
1. Snapshot consistente de SQLite con permisos 0600 y verificación de integridad.
2. Inmutabilidad estricta del archivo origen y rechazo absoluto de sobrescritura.
3. Importación atómica a PostgreSQL de las nueve entidades del DER:
   - Tipos exactos (Numeric/Decimal, bytes binarios BLOB, JSON estructurados, enums, ISO datetimes UTC).
   - Preservación de claves primarias, tokens de acceso (RF-47) e identidades de revisores.
   - Preservación de autorreferencias cíclicas e inversas en `claims.origin_claim_id`.
   - Normalización explícita de vocabulario renombrado (`corrosion` -> `fisura`).
   - Manejo de columnas legacy conocidas ausentes (`damage_description`, `registered_by`).
   - Interpretación de timestamps naive como UTC independiente de la zona horaria local.
   - Ajuste transaccional de secuencias (ALTER SEQUENCE RESTART WITH) y semántica de tabla vacía.
   - Validación completa de contenido fila por fila y columna por columna.
   - Transacción todo o nada (all-or-nothing): rollback completo ante cualquier error.
   - Salvaguarda estricta de destino no vacío y bloqueo exclusivo contra carreras.
   - Verificación de esquema y revisión de Alembic antes de operar.
   - Rechazo de tablas desconocidas de usuario en SQLite.
   - Rechazo de columnas desconocidas y datos no mapeables sin filtrar valores confidenciales.
4. Respaldo y restauración PostgreSQL (pg_dump custom y pg_restore):
   - Creación atómica sin sobreescritura destructiva.
   - Credenciales estrictamente pasadas por entorno y nunca en argv.
   - Rechazo de restauración sobre bases con tablas de usuario existentes.
   - Validación y rechazo de opciones libpq no soportadas en la URL.
5. Lectura correcta de la bandeja de revisión `/revision/bandeja` con datos migrados.
"""

from __future__ import annotations

import os
import sqlite3
import json
import hashlib
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError
from app.adaptadores.database import create_database_engine

from app.adaptadores.data_migration import (
    MigrationValidationError,
    DataMigrationError,
    DataMigrationSchemaError,
    MigrationTargetNotEmptyError,
    _extract_libpq_env,
    backup_postgres,
    import_sqlite_snapshot,
    restore_postgres,
    snapshot_sqlite,
    validate_imported_content,
)
from app.dominio.vocabulary import DamageClass
from tests.legacy_fixture import generate_synthetic_legacy_sqlite

REVISOR = {"Authorization": "Bearer revisor-rev-04"}


def test_snapshot_sqlite_preserva_consistencia_y_no_muta_origen(tmp_path: Path):
    """El snapshot SQLite crea una copia idéntica y jamás modifica el archivo origen."""
    origen = tmp_path / "origen.db"
    generate_synthetic_legacy_sqlite(origen)

    tamano_origen = origen.stat().st_size
    mtime_origen = origen.stat().st_mtime_ns

    destino = tmp_path / "snapshot.db"
    resultado = snapshot_sqlite(origen, destino)

    assert resultado == destino
    assert destino.is_file()
    assert destino.stat().st_size > 0

    # Comprobar inmutabilidad del origen
    assert origen.stat().st_size == tamano_origen
    assert origen.stat().st_mtime_ns == mtime_origen

    # Rechazo si origen y destino son el mismo archivo
    with pytest.raises(ValueError, match="mismo archivo"):
        snapshot_sqlite(origen, origen)

    # Rechazo absoluto de sobrescritura si el destino ya existe
    with pytest.raises(FileExistsError, match="ya existe"):
        snapshot_sqlite(origen, destino)


def test_import_preserva_las_nueve_tablas_tipos_exactos_y_autoreferencias(postgres_url: str, tmp_path: Path):
    """Importa el snapshot SQLite preservando las 9 entidades, tipos exactos y autorreferencias."""
    sqlite_path = tmp_path / "legacy.db"
    generate_synthetic_legacy_sqlite(sqlite_path, include_legacy_corrosion=True)

    transformaciones = import_sqlite_snapshot(sqlite_path, postgres_url)

    # Normalización CarDD contabilizada explícitamente
    assert transformaciones["inferences.predicted_class corrosion -> fisura"] == 1

    motor = create_database_engine(postgres_url)
    with motor.connect() as conn:
        # 1. models
        filas_models = conn.execute(text("SELECT id, version, current, onnx_opset FROM models")).mappings().all()
        assert len(filas_models) == 1
        assert filas_models[0]["id"] == 1
        assert filas_models[0]["version"] == "v1.0"
        assert filas_models[0]["current"] is True
        assert filas_models[0]["onnx_opset"] == 17

        # 2. configurations
        filas_config = conn.execute(
            text("SELECT id, amount_limit, effective_from, policy_mode FROM configurations")
        ).mappings().all()
        assert len(filas_config) == 1
        assert filas_config[0]["id"] == 1
        assert filas_config[0]["amount_limit"] == Decimal("150000.00")
        assert filas_config[0]["effective_from"].tzinfo is not None
        assert filas_config[0]["policy_mode"] == "automatico_con_escalamiento"

        # 3. claims: autorreferencias cíclicas e inversas preservadas
        filas_claims = conn.execute(
            text("SELECT id, tracking_number, origin_claim_id, declared_amount, registered_by, damage_description, status FROM claims ORDER BY id")
        ).mappings().all()
        assert len(filas_claims) == 3

        # Reclamo 1 apunta a Reclamo 2 (reverse-ID)
        assert filas_claims[0]["id"] == 1
        assert filas_claims[0]["tracking_number"] == "SN-0001"
        assert filas_claims[0]["origin_claim_id"] == 2
        assert filas_claims[0]["declared_amount"] == Decimal("125000.50")
        assert filas_claims[0]["registered_by"] == "operador-01"
        assert filas_claims[0]["status"] == "RequiereRevision"

        # Reclamo 2 apunta a Reclamo 1 (cíclico con 1)
        assert filas_claims[1]["id"] == 2
        assert filas_claims[1]["tracking_number"] == "SN-0002"
        assert filas_claims[1]["origin_claim_id"] == 1
        assert filas_claims[1]["declared_amount"] == Decimal("85000.00")
        assert filas_claims[1]["registered_by"] == "operador-02"
        assert filas_claims[1]["status"] == "Registrado"

        # Reclamo 3 no tiene autorreferencia ni campos opcionales, estado válido
        assert filas_claims[2]["id"] == 3
        assert filas_claims[2]["origin_claim_id"] is None
        assert filas_claims[2]["registered_by"] is None
        assert filas_claims[2]["damage_description"] is None
        assert filas_claims[2]["status"] == "Aprobado"

        # 4. evidence
        filas_evidence = conn.execute(
            text("SELECT id, claim_id, cid, pin_status FROM evidence ORDER BY id")
        ).mappings().all()
        assert len(filas_evidence) == 2
        assert filas_evidence[0]["cid"].startswith("Qm")
        assert filas_evidence[0]["pin_status"] == "fijado"
        assert filas_evidence[1]["pin_status"] == "pendiente"

        # 5. inferences: binarios exactos (576 floats en feature_vector) y clase normalizada a fisura
        filas_inferences = conn.execute(
            text("SELECT id, claim_id, predicted_class, feature_vector, logits FROM inferences ORDER BY id")
        ).mappings().all()
        assert len(filas_inferences) == 2
        assert filas_inferences[0]["predicted_class"] == DamageClass.FISURA.value
        assert len(bytes(filas_inferences[0]["feature_vector"])) == 2304  # 576 floats * 4 bytes
        assert len(bytes(filas_inferences[0]["logits"])) == 24  # 6 floats * 4 bytes
        assert filas_inferences[1]["predicted_class"] == "abolladura"

        # 6. proofs: BLOBs y JSON estructurados preservados
        filas_proofs = conn.execute(
            text("SELECT id, inference_id, proof, circuit_parameters, declared_visibility FROM proofs")
        ).mappings().all()
        assert len(filas_proofs) == 1
        assert bytes(filas_proofs[0]["proof"]) == b"\xde\xad\xbe\xef" * 8
        assert filas_proofs[0]["circuit_parameters"] == {"k": 17, "bits": 16}
        assert filas_proofs[0]["declared_visibility"] == {"input": "private", "output": "public"}

        # 7. jobs
        filas_jobs = conn.execute(text("SELECT id, status, attempts FROM jobs ORDER BY id")).mappings().all()
        assert len(filas_jobs) == 2
        assert filas_jobs[0]["status"] == "completado"
        assert filas_jobs[1]["status"] == "en_curso"

        # 8. transitions
        filas_trans = conn.execute(text("SELECT id, cause, from_status, to_status FROM transitions")).mappings().all()
        assert len(filas_trans) == 1
        assert filas_trans[0]["from_status"] == "Registrado"
        assert filas_trans[0]["to_status"] == "RequiereRevision"
        assert filas_trans[0]["cause"] == "ConfianzaBajoUmbral"

        # 9. reviews
        filas_rev = conn.execute(
            text("SELECT id, reviewer_identity, decision, justification FROM reviews")
        ).mappings().all()
        assert len(filas_rev) == 1
        assert filas_rev[0]["reviewer_identity"] == "revisor-rev-04"
        assert filas_rev[0]["decision"] is True
        # Validación cruzada de contenido fila por fila
        sqlite_conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            validate_imported_content(sqlite_conn, conn)
        finally:
            sqlite_conn.close()

    motor.dispose()


@pytest.mark.parametrize("source_kind", ["missing", "sql_null", "json_null", "object"])
def test_import_cli_preserva_null_sql_y_json(postgres_url: str, tmp_path: Path, source_kind: str):
    """El CLI conserva NULL SQL, null JSON explícito y contenido JSON sin mutar SQLite."""
    source = tmp_path / "nullable-json.db"
    generate_synthetic_legacy_sqlite(source, include_legacy_corrosion=False)
    with sqlite3.connect(source) as conn:
        if source_kind != "missing":
            conn.execute("ALTER TABLE jobs ADD COLUMN checkpoint_context JSON")
            value = {"sql_null": None, "json_null": "null", "object": '{"stage":"inferir","nested":null}'}[source_kind]
            conn.execute("UPDATE jobs SET checkpoint_context = ?", (value,))
        expected_counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("models", "configurations", "claims", "evidence", "inferences", "proofs", "jobs", "transitions", "reviews")
        }
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    env = {**os.environ, "DATABASE_URL": postgres_url}
    result = subprocess.run(
        [sys.executable, "-m", "app.adaptadores.data_migration", "import-sqlite", "--source", str(source)],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    lines = result.stdout.splitlines()
    assert lines[:2] == ["Importación a PostgreSQL completada con éxito.", "Transformaciones aplicadas:"]
    expected_transforms = {
        "  - missing_column:claims.pending_chain_action -> NULL: 3",
        *{f"  - missing_column:jobs.{name} -> NULL: 2" for name in
          ("current_stage", "configuration_id", "inference_id", "proof_id", "checkpoint_context",
           "worker_id", "lease_expires_at", "heartbeat_at", "available_at")
          if name != "checkpoint_context" or source_kind == "missing"},
        "  - missing_column:jobs.fence_token -> 0: 2",
    }
    assert len(lines[2:]) == len(expected_transforms)
    assert set(lines[2:]) == expected_transforms
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    engine = create_database_engine(postgres_url)
    try:
        with engine.connect() as conn:
            actual = conn.execute(text(
                "SELECT checkpoint_context IS NULL, checkpoint_context::text FROM jobs ORDER BY id"
            )).all()
            expected = {
                "missing": (True, None), "sql_null": (True, None),
                "json_null": (False, "null"), "object": (False, '{"stage":"inferir","nested":null}'),
            }[source_kind]
            assert actual == [expected, expected]
            for name, count in expected_counts.items():
                assert conn.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar() == count
                seq = conn.execute(text("SELECT pg_get_serial_sequence(:name, 'id')"), {"name": name}).scalar()
                last, called = conn.execute(text(f"SELECT last_value, is_called FROM {seq}")).one()
                maximum = conn.execute(text(f"SELECT MAX(id) FROM {name}")).scalar() or 0
                assert last > maximum and called is False
            if source_kind != "object":
                replacement = "'null'::json" if expected[0] else "NULL"
                conn.execute(text(f"UPDATE jobs SET checkpoint_context = {replacement}"))
                with sqlite3.connect(source) as sqlite_conn:
                    with pytest.raises(MigrationValidationError, match="nulabilidad SQL"):
                        validate_imported_content(sqlite_conn, conn)
                conn.rollback()
                assert conn.execute(text(
                    "SELECT checkpoint_context IS NULL, checkpoint_context::text FROM jobs ORDER BY id"
                )).all() == [expected, expected]
    finally:
        engine.dispose()


def test_secuencias_avanzadas_permiten_nuevas_inserciones_sin_colision(postgres_url: str, tmp_path: Path):
    """Comprueba que las secuencias seriales hayan quedado avanzadas tras el import."""
    sqlite_path = tmp_path / "legacy.db"
    generate_synthetic_legacy_sqlite(sqlite_path)
    import_sqlite_snapshot(sqlite_path, postgres_url)

    motor = create_database_engine(postgres_url)
    with motor.begin() as conn:
        nuevo_id = conn.execute(
            text("""
                INSERT INTO claims (tracking_number, access_token_hash, declared_amount, status, created_at, updated_at)
                VALUES ('SN-NUEVO-01', 'tokenhashnuevo', 50000.00, 'Registrado', NOW(), NOW())
                RETURNING id
            """)
        ).scalar()
        # Había 3 reclamos (id=1, 2, 3), por lo que el próximo debe ser 4
        assert nuevo_id == 4

        nuevo_model_id = conn.execute(
            text("""
                INSERT INTO models (version, extractor_path, extractor_sha256, head_path, head_sha256, onnx_opset, engine_version, current)
                VALUES ('v2.0', 'path/ext', 'sha1', 'path/head', 'sha2', 18, '1.17.0', FALSE)
                RETURNING id
            """)
        ).scalar()
        assert nuevo_model_id == 2

    motor.dispose()


def test_secuencia_tabla_vacia_asigna_uno_en_primer_insert(postgres_url: str, tmp_path: Path):
    """Semántica de tablas vacías: las secuencias de tablas sin filas asignan 1 en el primer insert."""
    sqlite_path = tmp_path / "vacio.db"
    conn = sqlite3.connect(str(sqlite_path))
    c = conn.cursor()
    c.execute("CREATE TABLE models (id INTEGER PRIMARY KEY, version VARCHAR(32) NOT NULL, extractor_path VARCHAR(255) NOT NULL, extractor_sha256 VARCHAR(64) NOT NULL, head_path VARCHAR(255) NOT NULL, head_sha256 VARCHAR(64) NOT NULL, onnx_opset INTEGER NOT NULL, engine_version VARCHAR(32) NOT NULL, current BOOLEAN NOT NULL)")
    c.execute("CREATE TABLE configurations (id INTEGER PRIMARY KEY, version VARCHAR(32) NOT NULL, derivation_threshold FLOAT, auto_settlement_threshold FLOAT, amount_limit NUMERIC(12,2), policy_mode VARCHAR(64) NOT NULL, max_files INTEGER NOT NULL, max_file_size_bytes INTEGER, max_job_attempts INTEGER, queue_concurrency INTEGER NOT NULL, contract_address VARCHAR(42), verifier_address VARCHAR(42), effective_from TIMESTAMP NOT NULL)")
    c.execute("CREATE TABLE claims (id INTEGER PRIMARY KEY, tracking_number VARCHAR(32) NOT NULL, access_token_hash VARCHAR(64) NOT NULL, declared_amount NUMERIC(12,2) NOT NULL, registered_by VARCHAR(64), damage_description TEXT, status VARCHAR(64) NOT NULL, onchain_claim_id VARCHAR(66), origin_claim_id INTEGER, assigned_reviewer VARCHAR(64), assigned_at TIMESTAMP, created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL)")
    c.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL, filename VARCHAR(255) NOT NULL, mime_type VARCHAR(64) NOT NULL, size_bytes INTEGER NOT NULL, sha256 VARCHAR(64) NOT NULL, cid VARCHAR(80) NOT NULL, pin_status VARCHAR(64) NOT NULL, remote_pin_status VARCHAR(64) NOT NULL, remote_cid_verified BOOLEAN NOT NULL, digest_verified BOOLEAN, tensor_commitment VARCHAR(80), created_at TIMESTAMP NOT NULL)")
    c.execute("CREATE TABLE inferences (id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL, model_id INTEGER NOT NULL, feature_vector BLOB NOT NULL, extractor_artifact_hash VARCHAR(64) NOT NULL, logits BLOB NOT NULL, predicted_class VARCHAR(64) NOT NULL, confidence FLOAT NOT NULL, created_at TIMESTAMP NOT NULL)")
    c.execute("CREATE TABLE proofs (id INTEGER PRIMARY KEY, inference_id INTEGER NOT NULL, proof BLOB NOT NULL, public_inputs BLOB NOT NULL, proof_hash VARCHAR(66) NOT NULL, circuit_parameters JSON NOT NULL, declared_visibility JSON NOT NULL, verified_locally BOOLEAN NOT NULL, created_at TIMESTAMP NOT NULL)")
    c.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL, status VARCHAR(64) NOT NULL, attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, last_failure_cause VARCHAR(64), enqueued_at TIMESTAMP NOT NULL, started_at TIMESTAMP, finished_at TIMESTAMP)")
    c.execute("CREATE TABLE transitions (id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL, configuration_id INTEGER NOT NULL, from_status VARCHAR(64), to_status VARCHAR(64) NOT NULL, cause VARCHAR(64), proof_hash VARCHAR(66), transaction_hash VARCHAR(66), block_number INTEGER, recorded_at TIMESTAMP NOT NULL)")
    c.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL, transition_id INTEGER, reviewer_identity VARCHAR(64) NOT NULL, decision BOOLEAN NOT NULL, justification TEXT NOT NULL, justification_hash VARCHAR(66) NOT NULL, timestamp TIMESTAMP NOT NULL)")
    conn.commit()
    conn.close()

    import_sqlite_snapshot(sqlite_path, postgres_url)

    motor = create_database_engine(postgres_url)
    with motor.begin() as pg_conn:
        primer_claim_id = pg_conn.execute(
            text("""
                INSERT INTO claims (tracking_number, access_token_hash, declared_amount, status, created_at, updated_at)
                VALUES ('SN-PRIMERO', 'hash1', 1000.00, 'Registrado', NOW(), NOW())
                RETURNING id
            """)
        ).scalar()
        assert primer_claim_id == 1

    motor.dispose()


def test_import_maneja_columnas_legacy_ausentes_y_reporta_transformaciones(postgres_url: str, tmp_path: Path):
    """Bases SQLite históricas sin registered_by, damage_description ni digest_verified se importan como NULL y se reportan."""
    sqlite_path = tmp_path / "legacy_viejas_columnas.db"
    generate_synthetic_legacy_sqlite(sqlite_path, drop_legacy_columns=True)

    transformaciones = import_sqlite_snapshot(sqlite_path, postgres_url)

    assert transformaciones["missing_column:claims.damage_description -> NULL"] == 3
    assert transformaciones["missing_column:claims.registered_by -> NULL"] == 3
    assert transformaciones["missing_column:evidence.digest_verified -> NULL"] == 2

    motor = create_database_engine(postgres_url)
    with motor.connect() as conn:
        filas_claims = conn.execute(
            text("SELECT damage_description, registered_by FROM claims ORDER BY id")
        ).all()
        for desc, reg in filas_claims:
            assert desc is None
            assert reg is None

        filas_evidence = conn.execute(
            text("SELECT digest_verified FROM evidence ORDER BY id")
        ).all()
        for (dv,) in filas_evidence:
            assert dv is None
    motor.dispose()

def test_timestamps_legacy_sin_zona_se_interpretan_utc_independiente_de_zona_local(postgres_url: str, tmp_path: Path, monkeypatch):
    """Comprueba que una marca temporal ISO naive '2026-08-20 12:00:00' se interprete como 12:00:00 UTC."""
    monkeypatch.setenv("TZ", "America/Argentina/Buenos_Aires")

    sqlite_path = tmp_path / "timestamps.db"
    generate_synthetic_legacy_sqlite(sqlite_path)

    import_sqlite_snapshot(sqlite_path, postgres_url)

    motor = create_database_engine(postgres_url)
    with motor.connect() as conn:
        dt_claim = conn.execute(text("SELECT created_at FROM claims WHERE id = 1")).scalar()
        assert dt_claim.tzinfo is not None
        assert dt_claim.hour == 12
        assert dt_claim.day == 20
        assert dt_claim.minute == 0
    motor.dispose()


def test_guard_de_destino_no_vacio_rechaza_importacion(postgres_url: str, tmp_path: Path):
    """Si el PostgreSQL de destino ya contiene filas, se rechaza la importación sin alterar nada."""
    sqlite_path = tmp_path / "legacy.db"
    generate_synthetic_legacy_sqlite(sqlite_path)

    motor = create_database_engine(postgres_url)
    with motor.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO models (id, version, extractor_path, extractor_sha256, head_path, head_sha256, onnx_opset, engine_version, current)
                VALUES (99, 'v_previa', 'p1', 's1', 'p2', 's2', 17, '1.16', FALSE)
            """)
        )
    motor.dispose()

    with pytest.raises(MigrationTargetNotEmptyError, match="contiene datos"):
        import_sqlite_snapshot(sqlite_path, postgres_url)


def test_transaccion_atomica_all_or_nothing_rollback_ante_error(postgres_url: str, tmp_path: Path):
    """Un fallo PostgreSQL posterior a las primeras inserciones revierte todo."""
    sqlite_path = tmp_path / "corrupto.db"
    generate_synthetic_legacy_sqlite(sqlite_path)

    conn = sqlite3.connect(str(sqlite_path))
    c = conn.cursor()
    # SQLite admite el monto; Numeric(12,2) lo rechaza luego de copiar modelos/configuración.
    c.execute("UPDATE claims SET declared_amount = 10000000000 WHERE id = 1")
    conn.commit()
    conn.close()

    with pytest.raises(DataError):
        import_sqlite_snapshot(sqlite_path, postgres_url)

    motor = create_database_engine(postgres_url)
    with motor.connect() as pg_conn:
        for tabla in ("models", "configurations", "claims", "evidence", "inferences", "proofs", "jobs", "transitions", "reviews"):
            cant = pg_conn.execute(text(f"SELECT COUNT(*) FROM {tabla}")).scalar()
            assert cant == 0, f"La tabla {tabla} debió quedar vacía tras el rollback"
    motor.dispose()


@pytest.mark.parametrize("extra_table", ["tabla_fantasma", "sqliteXprivate"])
def test_rechazo_tablas_desconocidas_en_sqlite(postgres_url: str, tmp_path: Path, extra_table: str):
    """Si SQLite contiene tablas de usuario no contempladas en el DER, debe fallar."""
    sqlite_path = tmp_path / "tabla_extra.db"
    generate_synthetic_legacy_sqlite(sqlite_path)

    conn = sqlite3.connect(str(sqlite_path))
    c = conn.cursor()
    c.execute(f"CREATE TABLE {extra_table} (id INTEGER PRIMARY KEY, valor TEXT)")
    conn.commit()
    conn.close()

    with pytest.raises(DataMigrationSchemaError, match="Tablas desconocidas"):
        import_sqlite_snapshot(sqlite_path, postgres_url)


def test_rechazo_columna_desconocida_sin_filtrar_valores_sensibles(postgres_url: str, tmp_path: Path):
    """Columnas no mapeables en SQLite deben provocar error sin filtrar valores confidenciales."""
    sqlite_path = tmp_path / "columna_rara.db"
    generate_synthetic_legacy_sqlite(sqlite_path)

    conn = sqlite3.connect(str(sqlite_path))
    c = conn.cursor()
    c.execute("ALTER TABLE claims ADD COLUMN secret_backdoor_token TEXT")
    c.execute("UPDATE claims SET secret_backdoor_token = 'super_secret_password_12345'")
    conn.commit()
    conn.close()

    with pytest.raises(DataMigrationSchemaError) as exc_info:
        import_sqlite_snapshot(sqlite_path, postgres_url)

    assert "secret_backdoor_token" in str(exc_info.value)
    assert "super_secret_password_12345" not in str(exc_info.value)


def test_rechazo_columna_obligatoria_faltante(postgres_url: str, tmp_path: Path):
    """Si falta una columna requerida no nulleable en SQLite, debe fallar claramente."""
    sqlite_path = tmp_path / "falta_columna.db"
    conn = sqlite3.connect(str(sqlite_path))
    c = conn.cursor()
    c.execute("CREATE TABLE models (id INTEGER PRIMARY KEY, extractor_path VARCHAR(255) NOT NULL, extractor_sha256 VARCHAR(64) NOT NULL, head_path VARCHAR(255) NOT NULL, head_sha256 VARCHAR(64) NOT NULL, onnx_opset INTEGER NOT NULL, engine_version VARCHAR(32) NOT NULL, current BOOLEAN NOT NULL)")
    for t in ("configurations", "claims", "evidence", "inferences", "proofs", "jobs", "transitions", "reviews"):
        c.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(DataMigrationSchemaError, match="Columna requerida ausente"):
        import_sqlite_snapshot(sqlite_path, postgres_url)


def test_esquema_no_migrado_rechaza_importacion(postgres_unmigrated_url: str, tmp_path: Path):
    """Si PostgreSQL no tiene las tablas creadas o la revisión Alembic, la importación se rechaza."""
    sqlite_path = tmp_path / "legacy.db"
    generate_synthetic_legacy_sqlite(sqlite_path)

    with pytest.raises(RuntimeError, match="no está migrada"):
        import_sqlite_snapshot(sqlite_path, postgres_unmigrated_url)


def test_backup_y_restore_comportamiento_atomico_y_guards(tmp_path: Path, monkeypatch):
    """Verifica el guard de no-sobrescritura, validación de libpq options y detección de archivos inexistentes."""
    dump_file = tmp_path / "backup.dump"
    dump_file.write_bytes(b"dummy")

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@127.0.0.1:5432/db")
    with pytest.raises(FileExistsError, match="ya existe"):
        backup_postgres(dump_file)

    no_existe = tmp_path / "no_existe.dump"
    with pytest.raises(FileNotFoundError):
        restore_postgres(no_existe)

    # Opciones de conexión no soportadas en DATABASE_URL deben ser rechazadas
    with pytest.raises(ValueError, match="no soportada"):
        _extract_libpq_env("postgresql://user:pass@127.0.0.1:5432/db?opcion_rara=peligro")



@pytest.mark.parametrize("confirmed", [True, False])
def test_bandeja_revision_legible_tras_migracion(
    confirmed, cliente, sesiones, postgres_url: str, tmp_path: Path
):
    """La bandeja solo acredita una inferencia enlazada a la prueba confirmada."""
    sqlite_path = tmp_path / "prod_simulada.db"
    generate_synthetic_legacy_sqlite(sqlite_path, include_legacy_corrosion=True)
    if not confirmed:
        with sqlite3.connect(sqlite_path) as conn:
            conn.execute("UPDATE proofs SET proof_hash = ?", ("0x" + "e" * 64,))

    import_sqlite_snapshot(sqlite_path, postgres_url)

    respuesta = cliente.get("/revision/bandeja", headers=REVISOR)
    assert respuesta.status_code == 200

    row = respuesta.json()["rows"][0]
    assert row["predictedClass"] == ("fisura" if confirmed else None)
    assert row["accredited"] is confirmed
    if not confirmed:
        assert row["confidence"] is None
        assert cliente.get("/reclamos/SN-0001/explicacion", headers=REVISOR).status_code == 409
        assert cliente.get("/publico/reclamos/SN-0001/traza").status_code == 409
@pytest.mark.parametrize(
    "parameters_json,visibility_json,invalid",
    [
        ('"root_string"', "null", False),
        ('{"nested": [1, true, null]}', '[1, "ñ", null]', False),
        ("123", "true", False),
        ('{"precise": 0.12345678901234567890123456789}', "{}", False),
        ("0.1", "false", False),
        ("NaN", "{}", True),
    ],
)
def test_json_preserva_strings_y_null_y_rechaza_nan(
    postgres_url: str, tmp_path: Path, parameters_json: str, visibility_json: str, invalid: bool
):
    """Preserva cualquier raíz JSON válida; rechaza constantes no conformes."""
    # Caso 1: JSON raíz string y JSON raíz null
    sqlite_path = tmp_path / "json_validos.db"
    generate_synthetic_legacy_sqlite(sqlite_path)

    conn = sqlite3.connect(str(sqlite_path))
    c = conn.cursor()
    c.execute(
        "UPDATE proofs SET circuit_parameters = ?, declared_visibility = ? WHERE id = 1",
        (parameters_json, visibility_json),
    )
    conn.commit()
    conn.close()

    if invalid:
        with pytest.raises(MigrationValidationError, match="JSON inválido"):
            import_sqlite_snapshot(sqlite_path, postgres_url)
        return

    import_sqlite_snapshot(sqlite_path, postgres_url)

    motor = create_database_engine(postgres_url)
    with motor.connect() as pg_conn:
        fila = pg_conn.execute(
            text("SELECT circuit_parameters::text, declared_visibility::text FROM proofs WHERE id = 1")
        ).one()
        assert json.loads(fila[0], parse_float=Decimal) == json.loads(parameters_json, parse_float=Decimal)
        assert json.loads(fila[1], parse_float=Decimal) == json.loads(visibility_json, parse_float=Decimal)
    motor.dispose()


def test_decimal_rechaza_perdida_de_precision(postgres_url: str, tmp_path: Path):
    """Rechaza antes de insertar cualquier Decimal que exceda la escala permitida (2 decimales)."""
    sqlite_path = tmp_path / "decimal_invalido.db"
    generate_synthetic_legacy_sqlite(sqlite_path)

    conn = sqlite3.connect(str(sqlite_path))
    c = conn.cursor()
    # declared_amount es Numeric(12, 2); 3 decimales no nulos debe fallar
    c.execute("UPDATE claims SET declared_amount = 125000.555 WHERE id = 1")
    conn.commit()
    conn.close()

    with pytest.raises(Exception, match="excede la escala permitida"):
        import_sqlite_snapshot(sqlite_path, postgres_url)

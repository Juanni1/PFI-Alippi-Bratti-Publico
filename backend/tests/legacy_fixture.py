"""Fixture sintético de SQLite legacy para pruebas y smoke de migración (PFI-137).

Genera una base SQLite consistente y realista que ejercita todo el modelo del DER:
- Las nueve entidades: models, configurations, claims, evidence, inferences, proofs, jobs, transitions, reviews.
- Tipos exactos: Decimal/Numeric(12,2), binarios BLOB con 576 floats en feature_vector, JSON estructurados, enums canónicos, ISO datetimes.
- Preservación de tokens de acceso con digesto sha256 conocido (`fixture-claim-1`, etc.).
- Autorreferencias cíclicas e inversas en claims (reclamo 1 -> 2 y 2 -> 1).
- Regla histórica de vocabulario CarDD ('corrosion' -> 'fisura').
- Vocabulario y estados canónicos reales (ClaimStatus, TransitionCause, PinStatus, JobStatus, PolicyMode).
"""

from __future__ import annotations

import array
import hashlib
import json
import sqlite3
from pathlib import Path

# Tokens en claro de prueba para permitir autenticación HTTP en smoke y tests
FIXTURE_CLAIM_TOKENS: dict[int, str] = {
    1: "fixture-claim-1",
    2: "fixture-claim-2",
    3: "fixture-claim-3",
}


def _dummy_feature_vector(seed: int = 17) -> bytes:
    """Genera 576 floats de 32 bits (2304 bytes), compatibles con decode_feature_vector."""
    floats = [float((i * seed + 7) % 100) / 100.0 for i in range(576)]
    return array.array("f", floats).tobytes()


def _dummy_logits(best_idx: int = 5) -> bytes:
    """Genera 6 floats de 32 bits para las seis clases de CarDD."""
    vals = [0.05] * 6
    vals[best_idx] = 0.75
    return array.array("f", vals).tobytes()


def generate_synthetic_legacy_sqlite(
    dest_path: str | Path,
    *,
    drop_legacy_columns: bool = False,
    include_legacy_corrosion: bool = True,
) -> Path:
    """Genera una base SQLite sintética y consistente con las 9 tablas del DER."""
    dst = Path(dest_path).resolve()
    if dst.exists():
        dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(dst))
    cursor = conn.cursor()

    # 1. models
    cursor.execute("""
        CREATE TABLE models (
            id INTEGER PRIMARY KEY,
            version VARCHAR(32) NOT NULL UNIQUE,
            extractor_path VARCHAR(255) NOT NULL,
            extractor_sha256 VARCHAR(64) NOT NULL,
            head_path VARCHAR(255) NOT NULL,
            head_sha256 VARCHAR(64) NOT NULL,
            onnx_opset INTEGER NOT NULL,
            engine_version VARCHAR(32) NOT NULL,
            current BOOLEAN NOT NULL
        )
    """)
    cursor.execute("""
        INSERT INTO models (id, version, extractor_path, extractor_sha256, head_path, head_sha256, onnx_opset, engine_version, current)
        VALUES (1, 'v1.0', 'modelos/extractor.onnx', 'a0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde', 'modelos/head.onnx', 'b0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde', 17, '1.16.3', 1)
    """)

    # 2. configurations
    cursor.execute("""
        CREATE TABLE configurations (
            id INTEGER PRIMARY KEY,
            version VARCHAR(32) NOT NULL UNIQUE,
            derivation_threshold FLOAT,
            auto_settlement_threshold FLOAT,
            amount_limit NUMERIC(12, 2),
            policy_mode VARCHAR(64) NOT NULL,
            max_files INTEGER NOT NULL,
            max_file_size_bytes INTEGER,
            max_job_attempts INTEGER,
            queue_concurrency INTEGER NOT NULL,
            contract_address VARCHAR(42),
            verifier_address VARCHAR(42),
            effective_from TIMESTAMP NOT NULL
        )
    """)
    cursor.execute("""
        INSERT INTO configurations (id, version, derivation_threshold, auto_settlement_threshold, amount_limit, policy_mode, max_files, max_file_size_bytes, max_job_attempts, queue_concurrency, contract_address, verifier_address, effective_from)
        VALUES (1, 'v1.0', 0.85, 0.95, 150000.00, 'automatico_con_escalamiento', 5, 10485760, 3, 1, '0x1111111111111111111111111111111111111111', '0x2222222222222222222222222222222222222222', '2026-08-01 10:00:00')
    """)

    # 3. claims
    token_1_hash = hashlib.sha256(FIXTURE_CLAIM_TOKENS[1].encode()).hexdigest()
    token_2_hash = hashlib.sha256(FIXTURE_CLAIM_TOKENS[2].encode()).hexdigest()
    token_3_hash = hashlib.sha256(FIXTURE_CLAIM_TOKENS[3].encode()).hexdigest()

    if drop_legacy_columns:
        cursor.execute("""
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                tracking_number VARCHAR(32) NOT NULL UNIQUE,
                access_token_hash VARCHAR(64) NOT NULL,
                declared_amount NUMERIC(12, 2) NOT NULL,
                status VARCHAR(64) NOT NULL,
                onchain_claim_id VARCHAR(66),
                origin_claim_id INTEGER REFERENCES claims(id),
                assigned_reviewer VARCHAR(64),
                assigned_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
        """)
        cursor.execute("""
            INSERT INTO claims (id, tracking_number, access_token_hash, declared_amount, status, onchain_claim_id, origin_claim_id, assigned_reviewer, assigned_at, created_at, updated_at)
            VALUES (1, 'SN-0001', ?, 125000.50, 'RequiereRevision', '0x1111111111111111111111111111111111111111111111111111111111111111', 2, 'revisor-rev-04', '2026-08-20 14:00:00', '2026-08-20 12:00:00', '2026-08-20 14:00:00')
        """, (token_1_hash,))
        cursor.execute("""
            INSERT INTO claims (id, tracking_number, access_token_hash, declared_amount, status, onchain_claim_id, origin_claim_id, assigned_reviewer, assigned_at, created_at, updated_at)
            VALUES (2, 'SN-0002', ?, 85000.00, 'Registrado', NULL, 1, NULL, NULL, '2026-08-21 09:30:00', '2026-08-21 09:30:00')
        """, (token_2_hash,))
        cursor.execute("""
            INSERT INTO claims (id, tracking_number, access_token_hash, declared_amount, status, onchain_claim_id, origin_claim_id, assigned_reviewer, assigned_at, created_at, updated_at)
            VALUES (3, 'SN-0003', ?, 250000.00, 'Aprobado', '0x3333333333333333333333333333333333333333333333333333333333333333', NULL, NULL, NULL, '2026-08-22 18:00:00', '2026-08-22 18:00:00')
        """, (token_3_hash,))
    else:
        cursor.execute("""
            CREATE TABLE claims (
                id INTEGER PRIMARY KEY,
                tracking_number VARCHAR(32) NOT NULL UNIQUE,
                access_token_hash VARCHAR(64) NOT NULL,
                declared_amount NUMERIC(12, 2) NOT NULL,
                registered_by VARCHAR(64),
                damage_description TEXT,
                status VARCHAR(64) NOT NULL,
                onchain_claim_id VARCHAR(66),
                origin_claim_id INTEGER REFERENCES claims(id),
                assigned_reviewer VARCHAR(64),
                assigned_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
        """)
        cursor.execute("""
            INSERT INTO claims (id, tracking_number, access_token_hash, declared_amount, registered_by, damage_description, status, onchain_claim_id, origin_claim_id, assigned_reviewer, assigned_at, created_at, updated_at)
            VALUES (1, 'SN-0001', ?, 125000.50, 'operador-01', 'Golpe lateral izquierdo en puerta y guardabarro', 'RequiereRevision', '0x1111111111111111111111111111111111111111111111111111111111111111', 2, 'revisor-rev-04', '2026-08-20 14:00:00', '2026-08-20 12:00:00', '2026-08-20 14:00:00')
        """, (token_1_hash,))
        cursor.execute("""
            INSERT INTO claims (id, tracking_number, access_token_hash, declared_amount, registered_by, damage_description, status, onchain_claim_id, origin_claim_id, assigned_reviewer, assigned_at, created_at, updated_at)
            VALUES (2, 'SN-0002', ?, 85000.00, 'operador-02', 'Fisura en parabrisas frontal', 'Registrado', NULL, 1, NULL, NULL, '2026-08-21 09:30:00', '2026-08-21 09:30:00')
        """, (token_2_hash,))
        cursor.execute("""
            INSERT INTO claims (id, tracking_number, access_token_hash, declared_amount, registered_by, damage_description, status, onchain_claim_id, origin_claim_id, assigned_reviewer, assigned_at, created_at, updated_at)
            VALUES (3, 'SN-0003', ?, 250000.00, NULL, NULL, 'Aprobado', '0x3333333333333333333333333333333333333333333333333333333333333333', NULL, NULL, NULL, '2026-08-22 18:00:00', '2026-08-22 18:00:00')
        """, (token_3_hash,))

    # 4. evidence
    if drop_legacy_columns:
        cursor.execute("""
            CREATE TABLE evidence (
                id INTEGER PRIMARY KEY,
                claim_id INTEGER NOT NULL REFERENCES claims(id),
                filename VARCHAR(255) NOT NULL,
                mime_type VARCHAR(64) NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 VARCHAR(64) NOT NULL,
                cid VARCHAR(80) NOT NULL,
                pin_status VARCHAR(64) NOT NULL,
                remote_pin_status VARCHAR(64) NOT NULL,
                remote_cid_verified BOOLEAN NOT NULL,
                tensor_commitment VARCHAR(80),
                created_at TIMESTAMP NOT NULL
            )
        """)
        cursor.execute("""
            INSERT INTO evidence (id, claim_id, filename, mime_type, size_bytes, sha256, cid, pin_status, remote_pin_status, remote_cid_verified, tensor_commitment, created_at)
            VALUES (1, 1, 'lateral.jpg', 'image/jpeg', 409600, 'c0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde', 'QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG', 'fijado', 'fijado', 1, 'tc-sha256-evidence-001', '2026-08-20 12:05:00')
        """)
        cursor.execute("""
            INSERT INTO evidence (id, claim_id, filename, mime_type, size_bytes, sha256, cid, pin_status, remote_pin_status, remote_cid_verified, tensor_commitment, created_at)
            VALUES (2, 2, 'parabrisas.png', 'image/png', 102400, 'd0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde', 'QmZtmD2qtNmR45N5z5v5Xf2nemtYgPpHdWEz79ojWnPbdH', 'pendiente', 'pendiente', 0, NULL, '2026-08-21 09:35:00')
        """)
    else:
        cursor.execute("""
            CREATE TABLE evidence (
                id INTEGER PRIMARY KEY,
                claim_id INTEGER NOT NULL REFERENCES claims(id),
                filename VARCHAR(255) NOT NULL,
                mime_type VARCHAR(64) NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 VARCHAR(64) NOT NULL,
                cid VARCHAR(80) NOT NULL,
                pin_status VARCHAR(64) NOT NULL,
                remote_pin_status VARCHAR(64) NOT NULL,
                remote_cid_verified BOOLEAN NOT NULL,
                digest_verified BOOLEAN,
                tensor_commitment VARCHAR(80),
                created_at TIMESTAMP NOT NULL
            )
        """)
        cursor.execute("""
            INSERT INTO evidence (id, claim_id, filename, mime_type, size_bytes, sha256, cid, pin_status, remote_pin_status, remote_cid_verified, digest_verified, tensor_commitment, created_at)
            VALUES (1, 1, 'lateral.jpg', 'image/jpeg', 409600, 'c0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde', 'QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG', 'fijado', 'fijado', 1, 1, 'tc-sha256-evidence-001', '2026-08-20 12:05:00')
        """)
        cursor.execute("""
            INSERT INTO evidence (id, claim_id, filename, mime_type, size_bytes, sha256, cid, pin_status, remote_pin_status, remote_cid_verified, digest_verified, tensor_commitment, created_at)
            VALUES (2, 2, 'parabrisas.png', 'image/png', 102400, 'd0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde', 'QmZtmD2qtNmR45N5z5v5Xf2nemtYgPpHdWEz79ojWnPbdH', 'pendiente', 'pendiente', 0, NULL, NULL, '2026-08-21 09:35:00')
        """)

    # 5. inferences: 576 floats en feature_vector y 6 floats en logits
    cursor.execute("""
        CREATE TABLE inferences (
            id INTEGER PRIMARY KEY,
            claim_id INTEGER NOT NULL REFERENCES claims(id),
            model_id INTEGER NOT NULL REFERENCES models(id),
            feature_vector BLOB NOT NULL,
            extractor_artifact_hash VARCHAR(64) NOT NULL,
            logits BLOB NOT NULL,
            predicted_class VARCHAR(64) NOT NULL,
            confidence FLOAT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
    """)
    pred_class_1 = "corrosion" if include_legacy_corrosion else "fisura"
    feat_1 = _dummy_feature_vector(seed=17)
    logits_1 = _dummy_logits(best_idx=5)
    cursor.execute("""
        INSERT INTO inferences (id, claim_id, model_id, feature_vector, extractor_artifact_hash, logits, predicted_class, confidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (1, 1, 1, feat_1, "e0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde", logits_1, pred_class_1, 0.92, "2026-08-20 12:10:00"))

    feat_2 = _dummy_feature_vector(seed=31)
    logits_2 = _dummy_logits(best_idx=0)
    cursor.execute("""
        INSERT INTO inferences (id, claim_id, model_id, feature_vector, extractor_artifact_hash, logits, predicted_class, confidence, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (2, 2, 1, feat_2, "e0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcde", logits_2, "abolladura", 0.88, "2026-08-21 09:40:00"))

    # 6. proofs
    cursor.execute("""
        CREATE TABLE proofs (
            id INTEGER PRIMARY KEY,
            inference_id INTEGER NOT NULL UNIQUE REFERENCES inferences(id),
            proof BLOB NOT NULL,
            public_inputs BLOB NOT NULL,
            proof_hash VARCHAR(66) NOT NULL,
            circuit_parameters JSON NOT NULL,
            declared_visibility JSON NOT NULL,
            verified_locally BOOLEAN NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
    """)
    cursor.execute("""
        INSERT INTO proofs (id, inference_id, proof, public_inputs, proof_hash, circuit_parameters, declared_visibility, verified_locally, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        1,
        1,
        b"\xde\xad\xbe\xef" * 8,
        b"\xca\xfe\xba\xbe" * 4,
        "0x" + "f" * 64,
        json.dumps({"k": 17, "bits": 16}),
        json.dumps({"input": "private", "output": "public"}),
        1,
        "2026-08-20 12:15:00",
    ))

    # 7. jobs: Job 1 completado, Job 2 en_curso con started_at (no pendiente, para no disparar procesamiento en smoke)
    cursor.execute("""
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            claim_id INTEGER NOT NULL REFERENCES claims(id),
            status VARCHAR(64) NOT NULL,
            attempts INTEGER NOT NULL,
            max_attempts INTEGER NOT NULL,
            last_failure_cause VARCHAR(64),
            enqueued_at TIMESTAMP NOT NULL,
            started_at TIMESTAMP,
            finished_at TIMESTAMP
        )
    """)
    cursor.execute("""
        INSERT INTO jobs (id, claim_id, status, attempts, max_attempts, last_failure_cause, enqueued_at, started_at, finished_at)
        VALUES (1, 1, 'completado', 1, 3, NULL, '2026-08-20 12:01:00', '2026-08-20 12:02:00', '2026-08-20 12:16:00')
    """)
    cursor.execute("""
        INSERT INTO jobs (id, claim_id, status, attempts, max_attempts, last_failure_cause, enqueued_at, started_at, finished_at)
        VALUES (2, 2, 'en_curso', 0, 3, NULL, '2026-08-21 09:31:00', '2026-08-21 09:32:00', NULL)
    """)

    # 8. transitions: causa real de states.yaml (ConfianzaBajoUmbral)
    cursor.execute("""
        CREATE TABLE transitions (
            id INTEGER PRIMARY KEY,
            claim_id INTEGER NOT NULL REFERENCES claims(id),
            configuration_id INTEGER NOT NULL REFERENCES configurations(id),
            from_status VARCHAR(64),
            to_status VARCHAR(64) NOT NULL,
            cause VARCHAR(64),
            proof_hash VARCHAR(66),
            transaction_hash VARCHAR(66),
            block_number INTEGER,
            recorded_at TIMESTAMP NOT NULL
        )
    """)
    cursor.execute("""
        INSERT INTO transitions (id, claim_id, configuration_id, from_status, to_status, cause, proof_hash, transaction_hash, block_number, recorded_at)
        VALUES (1, 1, 1, 'Registrado', 'RequiereRevision', 'ConfianzaBajoUmbral', (SELECT proof_hash FROM proofs WHERE inference_id = 1), '0x' || '9' || '1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcde', 1234567, '2026-08-20 12:20:00')
    """)

    # 9. reviews
    cursor.execute("""
        CREATE TABLE reviews (
            id INTEGER PRIMARY KEY,
            claim_id INTEGER NOT NULL REFERENCES claims(id),
            transition_id INTEGER REFERENCES transitions(id),
            reviewer_identity VARCHAR(64) NOT NULL,
            decision BOOLEAN NOT NULL,
            justification TEXT NOT NULL,
            justification_hash VARCHAR(66) NOT NULL,
            timestamp TIMESTAMP NOT NULL
        )
    """)
    cursor.execute("""
        INSERT INTO reviews (id, claim_id, transition_id, reviewer_identity, decision, justification, justification_hash, timestamp)
        VALUES (1, 1, 1, 'revisor-rev-04', 1, 'Daño compatible con el relato presentado.', '0x' || '8' || '1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcde', '2026-08-20 14:05:00')
    """)

    conn.commit()
    conn.close()

    return dst

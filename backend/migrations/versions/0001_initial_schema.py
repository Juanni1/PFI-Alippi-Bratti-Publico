"""Esquema inicial de las nueve entidades del DER, revisión 0001_initial_schema."""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# Identificadores de revisión usados por Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. configurations
    op.create_table(
        "configurations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("derivation_threshold", sa.Float(), nullable=True),
        sa.Column("auto_settlement_threshold", sa.Float(), nullable=True),
        sa.Column("amount_limit", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("policy_mode", sa.String(length=40), nullable=False),
        sa.Column("max_files", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("max_file_size_bytes", sa.Integer(), nullable=True),
        sa.Column("max_job_attempts", sa.Integer(), nullable=True),
        sa.Column("queue_concurrency", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("contract_address", sa.String(length=42), nullable=True),
        sa.Column("verifier_address", sa.String(length=42), nullable=True),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )

    # 2. models
    op.create_table(
        "models",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("extractor_path", sa.String(length=255), nullable=False),
        sa.Column("extractor_sha256", sa.String(length=64), nullable=False),
        sa.Column("head_path", sa.String(length=255), nullable=False),
        sa.Column("head_sha256", sa.String(length=64), nullable=False),
        sa.Column("onnx_opset", sa.Integer(), nullable=False),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("current", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )

    # 3. claims
    op.create_table(
        "claims",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tracking_number", sa.String(length=32), nullable=False),
        sa.Column("access_token_hash", sa.String(length=64), nullable=False),
        sa.Column("declared_amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("registered_by", sa.String(length=64), nullable=True),
        sa.Column("damage_description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("onchain_claim_id", sa.String(length=66), nullable=True),
        sa.Column("origin_claim_id", sa.Integer(), nullable=True),
        sa.Column("assigned_reviewer", sa.String(length=64), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["origin_claim_id"], ["claims.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tracking_number"),
    )
    op.create_index("ix_claims_status", "claims", ["status"])
    op.create_index("ix_claims_tracking_number", "claims", ["tracking_number"])

    # 4. evidence
    op.create_table(
        "evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("cid", sa.String(length=80), nullable=False),
        sa.Column("pin_status", sa.String(length=40), nullable=False),
        sa.Column("remote_pin_status", sa.String(length=40), nullable=False),
        sa.Column("remote_cid_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("digest_verified", sa.Boolean(), nullable=True),
        sa.Column("tensor_commitment", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_cid", "evidence", ["cid"])
    op.create_index("ix_evidence_claim_id", "evidence", ["claim_id"])

    # 5. inferences
    op.create_table(
        "inferences",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=False),
        sa.Column("feature_vector", sa.LargeBinary(), nullable=False),
        sa.Column("extractor_artifact_hash", sa.String(length=64), nullable=False),
        sa.Column("logits", sa.LargeBinary(), nullable=False),
        sa.Column("predicted_class", sa.String(length=40), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["model_id"], ["models.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inferences_claim_id", "inferences", ["claim_id"])

    # 6. proofs
    op.create_table(
        "proofs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("inference_id", sa.Integer(), nullable=False),
        sa.Column("proof", sa.LargeBinary(), nullable=False),
        sa.Column("public_inputs", sa.LargeBinary(), nullable=False),
        sa.Column("proof_hash", sa.String(length=66), nullable=False),
        sa.Column("circuit_parameters", sa.JSON(), nullable=False),
        sa.Column("declared_visibility", sa.JSON(), nullable=False),
        sa.Column("verified_locally", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["inference_id"], ["inferences.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("inference_id"),
    )
    op.create_index("ix_proofs_inference_id", "proofs", ["inference_id"])
    op.create_index("ix_proofs_proof_hash", "proofs", ["proof_hash"])

    # 7. jobs
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_failure_cause", sa.String(length=40), nullable=True),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_claim_id", "jobs", ["claim_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    # 8. transitions
    op.create_table(
        "transitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("configuration_id", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=40), nullable=True),
        sa.Column("to_status", sa.String(length=40), nullable=False),
        sa.Column("cause", sa.String(length=40), nullable=True),
        sa.Column("proof_hash", sa.String(length=66), nullable=True),
        sa.Column("transaction_hash", sa.String(length=66), nullable=True),
        sa.Column("block_number", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["configuration_id"], ["configurations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transitions_claim_id", "transitions", ["claim_id"])

    # 9. reviews
    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("transition_id", sa.Integer(), nullable=True),
        sa.Column("reviewer_identity", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.Boolean(), nullable=False),
        sa.Column("justification", sa.Text(), nullable=False),
        sa.Column("justification_hash", sa.String(length=66), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["transition_id"], ["transitions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reviews_claim_id", "reviews", ["claim_id"])


def downgrade() -> None:
    op.drop_table("reviews")
    op.drop_table("transitions")
    op.drop_table("jobs")
    op.drop_table("proofs")
    op.drop_table("inferences")
    op.drop_table("evidence")
    op.drop_table("claims")
    op.drop_table("models")
    op.drop_table("configurations")

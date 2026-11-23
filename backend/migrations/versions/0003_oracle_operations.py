"""Intenciones inmutables y exclusión de wallet de ADR-018."""
import uuid
from alembic import op
import sqlalchemy as sa

revision = "0003_oracle_operations"
down_revision = "0002_short_transactions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oracle_operations",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4),
        sa.Column("operation_key", sa.String(160), nullable=False, unique=True),
        sa.Column("claim_id", sa.Integer(), sa.ForeignKey("claims.id"), nullable=False),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("configuration_id", sa.Integer(), sa.ForeignKey("configurations.id"), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_address", sa.String(42), nullable=False),
        sa.Column("contract_address", sa.String(42), nullable=False),
        sa.Column("nonce", sa.BigInteger(), nullable=True),
        sa.Column("raw_signed_transaction", sa.LargeBinary(), nullable=True),
        sa.Column("transaction_hash", sa.String(66), nullable=True, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("broadcast_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("block_number", sa.BigInteger(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_oracle_operations_nonce",
        "oracle_operations",
        ["chain_id", "sender_address", "nonce"],
        unique=True,
        postgresql_where=sa.text("nonce IS NOT NULL"),
    )
    op.create_index("ix_oracle_operations_claim_id", "oracle_operations", ["claim_id"])
    op.create_index("ix_oracle_operations_job_id", "oracle_operations", ["job_id"])

    op.create_table(
        "oracle_wallets",
        sa.Column("chain_id", sa.BigInteger(), primary_key=True),
        sa.Column("sender_address", sa.String(42), primary_key=True),
        sa.Column("active_operation_id", sa.Uuid(), sa.ForeignKey("oracle_operations.id"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("oracle_wallets")
    op.drop_index("ix_oracle_operations_job_id", table_name="oracle_operations")
    op.drop_index("ix_oracle_operations_claim_id", table_name="oracle_operations")
    op.drop_index("uq_oracle_operations_nonce", table_name="oracle_operations")
    op.drop_table("oracle_operations")

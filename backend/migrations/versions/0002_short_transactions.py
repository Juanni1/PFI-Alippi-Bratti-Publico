"""Checkpoints y reservas previas al I/O externo de PFI-99."""
from alembic import op
import sqlalchemy as sa

revision = "0002_short_transactions"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("claims", sa.Column("pending_chain_action", sa.String(32), nullable=True))
    op.add_column("jobs", sa.Column("current_stage", sa.String(32), nullable=True))
    op.add_column("jobs", sa.Column("checkpoint_context", sa.JSON(), nullable=True))
    for column, target in (("configuration_id", "configurations"), ("inference_id", "inferences"), ("proof_id", "proofs")):
        op.add_column("jobs", sa.Column(column, sa.Integer(), nullable=True))
        op.create_foreign_key(f"fk_jobs_{column}", "jobs", target, [column], ["id"])


def downgrade() -> None:
    for column in ("proof_id", "inference_id", "configuration_id"):
        op.drop_constraint(f"fk_jobs_{column}", "jobs", type_="foreignkey")
        op.drop_column("jobs", column)
    op.drop_column("jobs", "checkpoint_context")
    op.drop_column("jobs", "current_stage")
    op.drop_column("claims", "pending_chain_action")

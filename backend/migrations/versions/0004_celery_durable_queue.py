"""Cola durable Celery con Redis, adquisición exclusiva en PostgreSQL y recuperación de ADR-018."""
import uuid
from alembic import op
import sqlalchemy as sa

revision = "0004_celery_durable_queue"
down_revision = "0003_oracle_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Nuevas columnas de jobs para propiedad y fencing
    op.add_column("jobs", sa.Column("worker_id", sa.Uuid(), nullable=True))
    op.add_column("jobs", sa.Column("fence_token", sa.BigInteger(), nullable=False, server_default=sa.text("0")))
    op.add_column("jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("available_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_jobs_available_at", "jobs", ["available_at"])

    # 2. Tabla singleton queue_control para control de concurrencia compartida
    op.create_table(
        "queue_control",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("configuration_id", sa.Integer(), sa.ForeignKey("configurations.id"), nullable=False),
        sa.Column("concurrency", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("concurrency > 0", name="ck_queue_control_concurrency_positive"),
    )

    # 3. Tabla job_dispatches para el outbox de despacho a Redis
    op.create_table(
        "job_dispatches",
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("next_publish_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("dispatch_token", sa.Uuid(), nullable=True),
        sa.Column("dispatch_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_job_dispatches_next_publish_at", "job_dispatches", ["next_publish_at"])

    # 4. Tabla job_attempts para trazabilidad de cada adquisición y fencing
    op.create_table(
        "job_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid4),
        sa.Column("job_id", sa.Integer(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fence_token", sa.BigInteger(), nullable=False),
        sa.Column("worker_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_id", sa.Integer(), sa.ForeignKey("configurations.id"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("stage_intervals", sa.JSON(), nullable=False),
        sa.Column("worker_boot_id", sa.Uuid(), nullable=False),
        sa.Column("worker_pid", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.String(128), nullable=True),
    )
    op.create_index(
        "uq_job_attempts_job_fence",
        "job_attempts",
        ["job_id", "fence_token"],
        unique=True,
    )
    op.create_index("ix_job_attempts_job_id", "job_attempts", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_job_attempts_job_id", table_name="job_attempts")
    op.drop_index("uq_job_attempts_job_fence", table_name="job_attempts")
    op.drop_table("job_attempts")

    op.drop_index("ix_job_dispatches_next_publish_at", table_name="job_dispatches")
    op.drop_table("job_dispatches")

    op.drop_table("queue_control")

    op.drop_index("ix_jobs_available_at", table_name="jobs")
    op.drop_column("jobs", "available_at")
    op.drop_column("jobs", "heartbeat_at")
    op.drop_column("jobs", "lease_expires_at")
    op.drop_column("jobs", "fence_token")
    op.drop_column("jobs", "worker_id")

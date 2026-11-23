"""Pruebas del esquema y modelos de la cola Celery (PFI-139 / ADR-018).

Verifica la migración 0004_celery_durable_queue, las restricciones de integridad,
valores por defecto y comportamiento en cascada de PostgreSQL.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.adaptadores.models import (
    Claim,
    Configuration,
    Job,
    JobAttempt,
    JobDispatch,
    QueueControl,
)
from app.dominio.vocabulary import ClaimStatus, JobStatus, PolicyMode


def test_queue_control_constraint_concurrency_positive(sesiones):
    """queue_control rechaza concurrencia <= 0 mediante CHECK constraint."""
    factory = sesiones
    with factory() as session:
        cfg = Configuration(
            version="v-qc-test",
            derivation_threshold=0.7,
            policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO,
            queue_concurrency=1,
        )
        session.add(cfg)
        session.flush()

        # Inválido: concurrencia 0
        qc = QueueControl(configuration_id=cfg.id, concurrency=0, paused=False)
        session.add(qc)
        with pytest.raises(IntegrityError):
            session.flush()


def test_job_default_fence_token_zero(sesiones):
    """Un job nuevo se inicializa con fence_token 0 y sin worker."""
    factory = sesiones
    with factory() as session:
        cfg = Configuration(
            version="v-job-test",
            derivation_threshold=0.7,
            policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO,
            queue_concurrency=1,
        )
        session.add(cfg)
        session.flush()

        claim = Claim(
            tracking_number=f"SN-{uuid.uuid4().hex[:8]}",
            access_token_hash="hash",
            declared_amount=Decimal("1000.00"),
            damage_description="Prueba",
            status=ClaimStatus.REGISTRADO,
        )
        session.add(claim)
        session.flush()

        job = Job(claim_id=claim.id, configuration_id=cfg.id, max_attempts=3)
        session.add(job)
        session.commit()

    with factory() as session:
        saved_job = session.scalars(select(Job).where(Job.claim_id == claim.id)).one()
        assert saved_job.fence_token == 0
        assert saved_job.worker_id is None
        assert saved_job.lease_expires_at is None
        assert saved_job.heartbeat_at is None
        assert saved_job.available_at is None


def test_job_attempts_unique_job_fence_token(sesiones):
    """job_attempts rechaza duplicados de (job_id, fence_token)."""
    factory = sesiones
    with factory() as session:
        cfg = Configuration(
            version="v-fence-test",
            derivation_threshold=0.7,
            policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO,
            queue_concurrency=1,
        )
        session.add(cfg)
        session.flush()

        claim = Claim(
            tracking_number=f"SN-{uuid.uuid4().hex[:8]}",
            access_token_hash="hash",
            declared_amount=Decimal("1000.00"),
            damage_description="Prueba",
            status=ClaimStatus.REGISTRADO,
        )
        session.add(claim)
        session.flush()

        job = Job(claim_id=claim.id, configuration_id=cfg.id, max_attempts=3)
        session.add(job)
        session.flush()

        w_id = uuid.uuid4()
        att1 = JobAttempt(
            job_id=job.id,
            fence_token=1,
            worker_id=w_id,
            configuration_id=cfg.id,
            stage_intervals=[],
            worker_boot_id=uuid.uuid4(),
            worker_pid=100,
        )
        session.add(att1)
        session.flush()

        # Duplicado: mismo job_id y fence_token
        att2 = JobAttempt(
            job_id=job.id,
            fence_token=1,
            worker_id=uuid.uuid4(),
            configuration_id=cfg.id,
            stage_intervals=[],
            worker_boot_id=uuid.uuid4(),
            worker_pid=200,
        )
        session.add(att2)
        with pytest.raises(IntegrityError):
            session.flush()


def test_job_deletion_cascades_to_dispatches_and_attempts(sesiones):
    """Al eliminar un job, sus despachos e intentos se eliminan en cascada."""
    factory = sesiones
    with factory() as session:
        cfg = Configuration(
            version="v-cascade-test",
            derivation_threshold=0.7,
            policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO,
            queue_concurrency=1,
        )
        session.add(cfg)
        session.flush()

        claim = Claim(
            tracking_number=f"SN-{uuid.uuid4().hex[:8]}",
            access_token_hash="hash",
            declared_amount=Decimal("1000.00"),
            damage_description="Prueba",
            status=ClaimStatus.REGISTRADO,
        )
        session.add(claim)
        session.flush()

        job = Job(claim_id=claim.id, configuration_id=cfg.id, max_attempts=3)
        session.add(job)
        session.flush()

        dispatch = JobDispatch(job_id=job.id)
        session.add(dispatch)

        attempt = JobAttempt(
            job_id=job.id,
            fence_token=1,
            worker_id=uuid.uuid4(),
            configuration_id=cfg.id,
            stage_intervals=[],
            worker_boot_id=uuid.uuid4(),
            worker_pid=100,
        )
        session.add(attempt)
        session.commit()

        # Eliminar el job
        session.delete(job)
        session.commit()

    with factory() as session:
        assert session.get(JobDispatch, job.id) is None
        assert session.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id)).first() is None

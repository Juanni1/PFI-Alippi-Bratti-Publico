"""Pruebas del protocolo de adquisición exclusiva y fencing (PFI-139 / ADR-018)."""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
import pytest
from sqlalchemy import select

from app.adaptadores.models import Claim, Configuration, Job, JobAttempt, QueueControl
from app.dominio.queue_acquisition import (
    QueueOwnershipLostError,
    acquire_job_by_id,
    finish_job_attempt,
    now,
    record_stage_progress,
    renew_lease,
)
from app.dominio.vocabulary import ClaimStatus, JobStatus, PolicyMode


def _create_claim_and_job(session, tracking="SN-01", max_attempts=3, config_id=None):
    claim = Claim(
        tracking_number=f"{tracking}-{uuid.uuid4().hex[:6]}",
        access_token_hash="hash",
        declared_amount=Decimal("1000.00"),
        damage_description="Prueba",
        status=ClaimStatus.REGISTRADO,
    )
    session.add(claim)
    session.flush()

    job = Job(claim_id=claim.id, configuration_id=config_id, max_attempts=max_attempts)
    session.add(job)
    session.flush()
    return claim, job


def test_acquire_job_success_and_fencing(sesiones):
    """Adquisición exitosa asigna worker_id, incrementa fence_token y crea JobAttempt."""
    factory = sesiones
    worker_id = uuid.uuid4()
    worker_boot_id = uuid.uuid4()

    with factory() as session:
        cfg = Configuration(version="v1", queue_concurrency=2, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
        session.add(cfg)
        session.flush()

        qc = QueueControl(id=1, configuration_id=cfg.id, concurrency=2, paused=False)
        session.add(qc)

        _, job = _create_claim_and_job(session, "SN-ACQ-1", config_id=cfg.id)
        job_id = job.id
        session.commit()

    with factory() as session:
        res = acquire_job_by_id(
            session,
            job_id,
            worker_id=worker_id,
            worker_boot_id=worker_boot_id,
            worker_pid=123,
            lease_duration_seconds=60,
        )
        assert res.acquired is True
        assert res.job_id == job_id
        assert res.fence_token == 1
        session.commit()

    with factory() as session:
        j = session.get(Job, job_id)
        assert j.status == JobStatus.EN_CURSO
        assert j.worker_id == worker_id
        assert j.fence_token == 1
        assert j.attempts == 1
        assert j.lease_expires_at is not None

        att = session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id)).one()
        assert att.fence_token == 1
        assert att.worker_id == worker_id
        assert att.worker_pid == 123


def test_acquire_enforces_concurrency_limit(sesiones):
    """Si la cantidad de jobs en curso alcanza concurrency, se rechaza la adquisición."""
    factory = sesiones
    worker_1 = uuid.uuid4()
    worker_2 = uuid.uuid4()

    with factory() as session:
        cfg = Configuration(version="v1", queue_concurrency=1, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
        session.add(cfg)
        session.flush()

        qc = QueueControl(id=1, configuration_id=cfg.id, concurrency=1, paused=False)
        session.add(qc)

        _, job1 = _create_claim_and_job(session, "SN-C1", config_id=cfg.id)
        _, job2 = _create_claim_and_job(session, "SN-C2", config_id=cfg.id)
        j1_id, j2_id = job1.id, job2.id
        session.commit()

    with factory() as session:
        # Worker 1 adquiere job 1 -> cupo lleno (1/1)
        res1 = acquire_job_by_id(session, j1_id, worker_id=worker_1, worker_boot_id=uuid.uuid4(), worker_pid=1)
        assert res1.acquired is True
        session.commit()

    with factory() as session:
        # Worker 2 intenta adquirir job 2 -> rechazado por límite
        res2 = acquire_job_by_id(session, j2_id, worker_id=worker_2, worker_boot_id=uuid.uuid4(), worker_pid=2)
        assert res2.acquired is False
        assert res2.reason == "concurrency_limit_reached"


def test_acquire_rejects_when_queue_paused(sesiones):
    """Si queue_control.paused es True, no se adquiere ningún trabajo."""
    factory = sesiones
    with factory() as session:
        cfg = Configuration(version="v1", queue_concurrency=2, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
        session.add(cfg)
        session.flush()

        qc = QueueControl(id=1, configuration_id=cfg.id, concurrency=2, paused=True)
        session.add(qc)

        _, job = _create_claim_and_job(session, "SN-PAUSED", config_id=cfg.id)
        job_id = job.id
        session.commit()

    with factory() as session:
        res = acquire_job_by_id(session, job_id, worker_id=uuid.uuid4(), worker_boot_id=uuid.uuid4(), worker_pid=1)
        assert res.acquired is False
        assert res.reason == "queue_paused"


def test_acquire_respects_fifo_order(sesiones):
    """Si hay un job más antiguo pendiente, se rechaza la adquisición fuera de orden sin consumir intento."""
    factory = sesiones
    with factory() as session:
        cfg = Configuration(version="v1", queue_concurrency=2, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
        session.add(cfg)
        session.flush()

        qc = QueueControl(id=1, configuration_id=cfg.id, concurrency=2, paused=False)
        session.add(qc)

        _, job1 = _create_claim_and_job(session, "SN-FIFO-1", config_id=cfg.id)
        _, job2 = _create_claim_and_job(session, "SN-FIFO-2", config_id=cfg.id)
        j1_id, j2_id = job1.id, job2.id
        session.commit()

    with factory() as session:
        # Intenta adquirir job 2 antes que job 1
        res = acquire_job_by_id(session, j2_id, worker_id=uuid.uuid4(), worker_boot_id=uuid.uuid4(), worker_pid=1)
        assert res.acquired is False
        assert res.reason == "out_of_order"
        assert res.earliest_job_id == j1_id

        # Verificar que job 2 sigue intacto y no consumió intentos
        j2 = session.get(Job, j2_id)
        assert j2.attempts == 0
        assert j2.status == JobStatus.PENDIENTE


def test_renew_lease_and_fencing_protection(sesiones):
    """La renovación valida worker y token; el avance rechaza trabajadores obsoletos o vencidos."""
    factory = sesiones
    worker_1 = uuid.uuid4()
    worker_2 = uuid.uuid4()

    with factory() as session:
        cfg = Configuration(version="v1", queue_concurrency=2, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
        session.add(cfg)
        session.flush()

        qc = QueueControl(id=1, configuration_id=cfg.id, concurrency=2, paused=False)
        session.add(qc)

        _, job = _create_claim_and_job(session, "SN-LEASE", config_id=cfg.id)
        job_id = job.id
        session.commit()

    with factory() as session:
        acquire_job_by_id(session, job_id, worker_id=worker_1, worker_boot_id=uuid.uuid4(), worker_pid=1)
        session.commit()

    with factory() as session:
        # Renovación válida
        assert renew_lease(session, job_id=job_id, worker_id=worker_1, fence_token=1) is True
        # Renovación con worker_id equivocado
        assert renew_lease(session, job_id=job_id, worker_id=worker_2, fence_token=1) is False
        # Renovación con token equivocado
        assert renew_lease(session, job_id=job_id, worker_id=worker_1, fence_token=99) is False

        # Avance válido
        record_stage_progress(session, job_id=job_id, worker_id=worker_1, fence_token=1, stage="extraccion")

        # Avance con token o worker incorrecto lanza QueueOwnershipLostError
        with pytest.raises(QueueOwnershipLostError):
            record_stage_progress(session, job_id=job_id, worker_id=worker_2, fence_token=1, stage="generacion")

        with pytest.raises(QueueOwnershipLostError):
            record_stage_progress(session, job_id=job_id, worker_id=worker_1, fence_token=2, stage="generacion")


def test_finish_job_attempt_records_outcome_and_releases(sesiones):
    """Finalizar el trabajo asienta resultado e intervalos y limpia la lease."""
    factory = sesiones
    worker_id = uuid.uuid4()

    with factory() as session:
        cfg = Configuration(version="v1", queue_concurrency=2, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
        session.add(cfg)
        session.flush()

        qc = QueueControl(id=1, configuration_id=cfg.id, concurrency=2, paused=False)
        session.add(qc)

        _, job = _create_claim_and_job(session, "SN-FINISH", config_id=cfg.id)
        job_id = job.id
        session.commit()

    with factory() as session:
        acquire_job_by_id(session, job_id, worker_id=worker_id, worker_boot_id=uuid.uuid4(), worker_pid=1)
        session.commit()

    intervals = [{"stage": "registro_cadena", "duration_ms": 150}]
    with factory() as session:
        finish_job_attempt(
            session,
            job_id=job_id,
            worker_id=worker_id,
            fence_token=1,
            status=JobStatus.COMPLETADO,
            outcome="completado",
            stage_intervals=intervals,
        )
        session.commit()

    with factory() as session:
        j = session.get(Job, job_id)
        assert j.status == JobStatus.COMPLETADO
        assert j.lease_expires_at is None
        assert j.finished_at is not None

        att = session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id)).one()
        assert att.outcome == "completado"
        assert att.stage_intervals == intervals


def test_concurrent_workers_competition(sesiones):
    """Múltiples workers concurrentes compitiendo por jobs respetan el límite de queue_control."""
    import concurrent.futures

    factory = sesiones
    concurrency_limit = 2
    num_jobs = 6

    with factory() as session:
        cfg = Configuration(version="v-conc", queue_concurrency=concurrency_limit, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
        session.add(cfg)
        session.flush()

        qc = QueueControl(id=1, configuration_id=cfg.id, concurrency=concurrency_limit, paused=False)
        session.add(qc)

        job_ids = []
        for i in range(num_jobs):
            _, job = _create_claim_and_job(session, f"SN-CONC-{i}", config_id=cfg.id)
            job_ids.append(job.id)
        session.commit()

    def try_acquire(j_id):
        import time
        worker_id = uuid.uuid4()
        for _ in range(10):
            with factory() as s:
                res = acquire_job_by_id(
                    s,
                    j_id,
                    worker_id=worker_id,
                    worker_boot_id=uuid.uuid4(),
                    worker_pid=1000,
                )
                s.commit()
            if res.reason != "out_of_order":
                return res
            time.sleep(0.005)
        return res

    # 6 workers intentan adquirir los 6 jobs en paralelo
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(try_acquire, job_ids))

    # Exactamente 2 deben haber sido adquiridos (los primeros en orden FIFO)
    acquired_count = sum(1 for r in results if r.acquired)
    assert acquired_count == concurrency_limit

    # Verificar en base de datos que solo 2 jobs están en estado EN_CURSO
    with factory() as session:
        active_jobs = session.scalars(select(Job).where(Job.status == JobStatus.EN_CURSO)).all()
        assert len(active_jobs) == concurrency_limit
        # Y que los 2 adquiridos fueron los primeros de la cola
        assert {j.id for j in active_jobs} == {job_ids[0], job_ids[1]}


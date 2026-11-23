"""Protocolo de adquisición exclusiva y fencing en PostgreSQL (PFI-139 / ADR-018).

Garantiza:
1. Concurrencia acotada compartida a nivel base de datos mediante queue_control.
2. Adquisición atómica con SELECT ... FOR UPDATE SKIP LOCKED.
3. Respeto estricto del orden de llegada CU-03 (enqueued_at, id).
4. Fencing token creciente para invalidar zombies y ejecutores obsoletos.
5. Renovación periódica de lease y verificación de propiedad en cada avance.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.adaptadores.models import Configuration, Job, JobAttempt, JobDispatch, QueueControl
from app.dominio.vocabulary import JobStatus


def now() -> datetime:
    return datetime.now(timezone.utc)


class QueueOwnershipLostError(Exception):
    """Lanzada cuando un worker intenta avanzar o confirmar un job habiendo perdido la propiedad."""


class QueueControlMismatchError(Exception):
    """Lanzada cuando queue_control no coincide con la configuración activa o el pool del worker."""


@dataclass(frozen=True)
class AcquisitionResult:
    acquired: bool
    job_id: int | None = None
    fence_token: int | None = None
    configuration_id: int | None = None
    reason: str | None = None
    earliest_job_id: int | None = None


def validate_queue_control(
    session: Session,
    *,
    pool_concurrency: int | None = None,
) -> QueueControl:
    """Valida que queue_control coincida con la configuración activa y el pool Celery."""
    active_config = session.scalars(
        select(Configuration).order_by(Configuration.effective_from.desc(), Configuration.id.desc())
    ).first()
    if active_config is None:
        from app.dominio.claims import ensure_declared_configuration
        active_config = ensure_declared_configuration(session)
    qc = session.get(QueueControl, 1)
    if qc is None:
        qc = QueueControl(
            id=1,
            configuration_id=active_config.id,
            concurrency=active_config.queue_concurrency,
            paused=False,
        )
        session.add(qc)
        session.flush()

    if active_config is not None:
        if qc.configuration_id != active_config.id or qc.concurrency != active_config.queue_concurrency:
            raise QueueControlMismatchError(
                f"Desacuerdo entre queue_control (config={qc.configuration_id}, conc={qc.concurrency}) "
                f"y configuración activa (id={active_config.id}, conc={active_config.queue_concurrency})"
            )
    if pool_concurrency is not None and qc.concurrency != pool_concurrency:
        raise QueueControlMismatchError(
            f"Desacuerdo entre queue_control.concurrency ({qc.concurrency}) y pool Celery ({pool_concurrency})"
        )
    return qc


def _reschedule_dispatch_now(session: Session, job_id: int, now_ts: datetime) -> None:
    dispatch = session.get(JobDispatch, job_id)
    if dispatch is not None:
        dispatch.next_publish_at = now_ts


def acquire_job_by_id(
    session: Session,
    job_id: int,
    *,
    worker_id: UUID,
    worker_boot_id: UUID,
    worker_pid: int,
    deployment_id: str | None = None,
    lease_duration_seconds: int = 60,
    is_continuation: bool = False,
) -> AcquisitionResult:
    """Intenta adquirir atómicamente el job indicado verificando cupo, orden y estado.

    Ejecuta en una transacción corta. Verifica el singleton queue_control, cuenta los slots
    ocupados (status == EN_CURSO) y confirma que job_id sea el primero en orden enqueued_at, id.
    """
    now_ts = now()

    # 1. Bloqueo del control de concurrencia
    active_config = session.scalars(
        select(Configuration).order_by(Configuration.effective_from.desc(), Configuration.id.desc())
    ).first()
    qc = session.scalars(
        select(QueueControl).where(QueueControl.id == 1).with_for_update()
    ).first()
    if qc is None:
        config_id = active_config.id if active_config else 1
        conc = active_config.queue_concurrency if active_config else 1
        qc = QueueControl(id=1, configuration_id=config_id, concurrency=conc, paused=False)
        session.add(qc)
        session.flush()
    elif active_config is not None and (
        qc.configuration_id != active_config.id
        or qc.concurrency != active_config.queue_concurrency
    ):
        return AcquisitionResult(
            acquired=False,
            job_id=job_id,
            reason="configuration_mismatch",
        )

    if qc.paused:
        return AcquisitionResult(acquired=False, job_id=job_id, reason="queue_paused")

    # Verificar que el job objetivo siga pendiente antes de evaluar la cola
    target_job = session.get(Job, job_id)
    if target_job is None or target_job.status != JobStatus.PENDIENTE:
        return AcquisitionResult(acquired=False, job_id=job_id, reason="job_not_pending")

    # 2. Control estricto de concurrencia: contar jobs en curso
    active_count = session.scalar(
        select(func.count(Job.id)).where(Job.status == JobStatus.EN_CURSO)
    ) or 0

    if active_count >= qc.concurrency:
        _reschedule_dispatch_now(session, job_id, now_ts)
        return AcquisitionResult(
            acquired=False,
            job_id=job_id,
            reason="concurrency_limit_reached",
        )

    # 3. Verificar orden de llegada CU-03 (enqueued_at ASC, id ASC)
    earliest_job = session.scalars(
        select(Job)
        .where(
            Job.status == JobStatus.PENDIENTE,
            or_(Job.available_at.is_(None), Job.available_at <= now_ts),
        )
        .order_by(Job.enqueued_at.asc(), Job.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()

    if earliest_job is None:
        return AcquisitionResult(acquired=False, job_id=job_id, reason="no_eligible_job")

    if earliest_job.id != job_id:
        # Llegó un mensaje desordenado o hay un job anterior todavía pendiente:
        # no se toma otro job ni se consume un intento; se preserva para el siguiente ciclo.
        _reschedule_dispatch_now(session, job_id, now_ts)
        return AcquisitionResult(
            acquired=False,
            job_id=job_id,
            reason="out_of_order",
            earliest_job_id=earliest_job.id,
        )

    # 4. Adquisición atómica exitosa
    earliest_job.fence_token += 1
    earliest_job.worker_id = worker_id
    earliest_job.lease_expires_at = now_ts + timedelta(seconds=lease_duration_seconds)
    earliest_job.heartbeat_at = now_ts
    earliest_job.status = JobStatus.EN_CURSO
    earliest_job.started_at = now_ts
    if not is_continuation:
        earliest_job.attempts += 1
    initial_stage = earliest_job.current_stage or "registro_cadena"
    earliest_job.current_stage = initial_stage

    if earliest_job.configuration_id is None:
        earliest_job.configuration_id = qc.configuration_id

    # 5. Registro de intento de ejecución para trazabilidad y fencing
    attempt = JobAttempt(
        id=uuid.uuid4(),
        job_id=earliest_job.id,
        fence_token=earliest_job.fence_token,
        worker_id=worker_id,
        configuration_id=earliest_job.configuration_id,
        started_at=now_ts,
        stage_intervals=[
            {
                "stage": initial_stage,
                "started_at": now_ts.isoformat(),
                "finished_at": None,
            }
        ],
        worker_boot_id=worker_boot_id,
        worker_pid=worker_pid,
        deployment_id=deployment_id,
    )
    session.add(attempt)
    session.flush()

    return AcquisitionResult(
        acquired=True,
        job_id=earliest_job.id,
        fence_token=earliest_job.fence_token,
        configuration_id=earliest_job.configuration_id,
    )


def renew_lease(
    session: Session,
    *,
    job_id: int,
    worker_id: UUID,
    fence_token: int,
    lease_duration_seconds: int = 60,
) -> bool:
    """Renueva la lease de un job si el worker y el token siguen siendo válidos."""
    now_ts = now()
    job = session.scalars(
        select(Job)
        .where(
            Job.id == job_id,
            Job.worker_id == worker_id,
            Job.fence_token == fence_token,
            Job.status == JobStatus.EN_CURSO,
        )
        .with_for_update()
    ).first()

    if job is None:
        return False

    if job.lease_expires_at is not None and job.lease_expires_at < now_ts:
        return False

    job.heartbeat_at = now_ts
    job.lease_expires_at = now_ts + timedelta(seconds=lease_duration_seconds)
    session.flush()
    return True


def record_stage_progress(
    session: Session,
    *,
    job_id: int,
    worker_id: UUID,
    fence_token: int,
    stage: str,
    inference_id: int | None = None,
    proof_id: int | None = None,
    checkpoint_context: dict[str, Any] | None = None,
) -> None:
    """Registra el avance de etapa validando estrictamente worker_id y fence_token."""
    now_ts = now()
    job = session.scalars(
        select(Job)
        .where(
            Job.id == job_id,
            Job.worker_id == worker_id,
            Job.fence_token == fence_token,
            Job.status == JobStatus.EN_CURSO,
        )
        .with_for_update()
    ).first()

    if job is None:
        raise QueueOwnershipLostError(
            f"El job {job_id} ya no pertenece al worker {worker_id} con fence {fence_token}"
        )

    # Validar que la lease no haya expirado según reloj de la base
    if job.lease_expires_at is not None and job.lease_expires_at < now_ts:
        raise QueueOwnershipLostError(
            f"La lease del job {job_id} expiró en {job.lease_expires_at} (hora actual: {now_ts})"
        )

    previous_stage = job.current_stage
    job.current_stage = stage
    if inference_id is not None:
        job.inference_id = inference_id
    if proof_id is not None:
        job.proof_id = proof_id
    if checkpoint_context is not None:
        job.checkpoint_context = checkpoint_context

    attempt = session.scalars(
        select(JobAttempt)
        .where(
            JobAttempt.job_id == job_id,
            JobAttempt.fence_token == fence_token,
        )
        .with_for_update()
    ).first()
    if attempt is not None:
        intervals = list(attempt.stage_intervals or [])
        now_iso = now_ts.isoformat()
        if not intervals:
            intervals.append({"stage": stage, "started_at": now_iso, "finished_at": None})
        elif intervals[-1].get("stage") != stage or previous_stage != stage:
            if intervals[-1].get("finished_at") is None:
                intervals[-1] = {**intervals[-1], "finished_at": now_iso}
            intervals.append({"stage": stage, "started_at": now_iso, "finished_at": None})
        attempt.stage_intervals = intervals

    session.flush()


def finish_job_attempt(
    session: Session,
    *,
    job_id: int,
    worker_id: UUID,
    fence_token: int,
    status: JobStatus,
    outcome: str,
    stage_intervals: list[dict[str, Any]] | None = None,
) -> None:
    """Finaliza el intento y el job liberando la propiedad."""
    now_ts = now()
    job = session.scalars(
        select(Job)
        .where(
            Job.id == job_id,
            Job.worker_id == worker_id,
            Job.fence_token == fence_token,
        )
        .with_for_update()
    ).first()

    if job is None:
        raise QueueOwnershipLostError(
            f"No se pudo finalizar el job {job_id}: propiedad perdida para worker {worker_id}"
        )

    if job.lease_expires_at is not None and job.lease_expires_at < now_ts:
        raise QueueOwnershipLostError(
            f"No se pudo finalizar el job {job_id}: lease vencida en {job.lease_expires_at}"
        )

    job.status = status
    if status in (JobStatus.COMPLETADO, JobStatus.FALLIDO):
        job.finished_at = now_ts
    job.lease_expires_at = None
    if status != JobStatus.EN_CURSO:
        job.worker_id = None

    attempt = session.scalars(
        select(JobAttempt)
        .where(
            JobAttempt.job_id == job_id,
            JobAttempt.fence_token == fence_token,
        )
        .with_for_update()
    ).first()

    if attempt is not None:
        attempt.finished_at = now_ts
        attempt.outcome = outcome
        if stage_intervals is not None:
            attempt.stage_intervals = stage_intervals
        else:
            intervals = list(attempt.stage_intervals or [])
            if intervals and intervals[-1].get("finished_at") is None:
                intervals[-1] = {**intervals[-1], "finished_at": now_ts.isoformat()}
            attempt.stage_intervals = intervals

    session.flush()

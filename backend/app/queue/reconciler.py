"""Despachador outbox y reconciliador autónomo de leases huérfanas (PFI-139 / ADR-018).

Corre en el servicio worker cada 10 segundos fuera del pool de cómputo, sin depender
de Celery Beat ni de que Redis entregue una tarea periódica.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import UUID

from celery import bootsteps
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.adaptadores.database import session_scope
from app.adaptadores.models import Claim, Job, JobAttempt, JobDispatch, OracleOperation
from app.dominio.vocabulary import JobFailureReason, JobStatus, OracleOperationStatus
from app.queue.tasks import _BOOT_ID, process_job_task
from app.queue.worker_context import get_worker_context

_log = logging.getLogger("app.queue.reconciler")

DISPATCH_RESERVATION_SECONDS = 30
REPUBLISH_UNACQUIRED_SECONDS = 60
MIN_FAILURE_BACKOFF_SECONDS = 30
MAX_FAILURE_BACKOFF_SECONDS = 300
TASK_EXPIRES_SECONDS = 300
RECONCILE_INTERVAL_SECONDS = 10.0


def now() -> datetime:
    return datetime.now(timezone.utc)


def _default_publisher(job_id: int) -> None:
    process_job_task.apply_async(args=[job_id], expires=TASK_EXPIRES_SECONDS)


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def default_is_executor_terminated(
    attempt: JobAttempt | None,
    *,
    current_boot_id: UUID = _BOOT_ID,
    current_deployment_id: str | None = None,
) -> bool:
    """Determina si el ejecutor anterior terminó de forma comprobable (ADR-018).

    - Si no hay registro de intento previo, se considera terminado.
    - Si hay cambio de deployment_id y no coincide con el actual, no se asume
      terminado automáticamente sin confirmación externa.
    - Si comparte boot_id con el supervisor actual, verifica si el PID sigue vivo.
    - Si tiene otro boot_id dentro del mismo entorno local (deployment_id nulo o igual),
      corresponde a un proceso anterior ya reiniciado.
    """
    if attempt is None:
        return True
    if (
        attempt.deployment_id is not None
        and current_deployment_id is not None
        and attempt.deployment_id != current_deployment_id
    ):
        return False
    if attempt.deployment_id is not None and current_deployment_id is None:
        return False
    if attempt.worker_boot_id == current_boot_id:
        return not _is_pid_alive(attempt.worker_pid)
    return True


def publish_job_dispatch(
    factory: sessionmaker[Session],
    job_id: int,
    *,
    publisher_fn: Callable[[int], None] | None = None,
) -> bool:
    """Reserva la fila de job_dispatches, publica en Redis fuera de la transacción y asienta el resultado."""
    publish = publisher_fn or _default_publisher
    now_ts = now()
    token = uuid.uuid4()

    # 1. Transacción corta: reservar despacho sin mantener conexión abierta hacia Redis
    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        if job is None or job.status != JobStatus.PENDIENTE or job.worker_id is not None:
            return False
        if job.available_at is not None and job.available_at > now_ts:
            return False

        dispatch = session.scalars(
            select(JobDispatch)
            .where(
                JobDispatch.job_id == job_id,
                or_(
                    JobDispatch.dispatch_expires_at.is_(None),
                    JobDispatch.dispatch_expires_at <= now_ts,
                ),
            )
            .with_for_update(skip_locked=True)
        ).first()

        if dispatch is None:
            existing = session.get(JobDispatch, job_id)
            if existing is not None:
                return False
            dispatch = JobDispatch(
                job_id=job_id,
                next_publish_at=now_ts,
                publish_attempts=0,
            )
            session.add(dispatch)
            session.flush()

        dispatch.dispatch_token = token
        dispatch.dispatch_expires_at = now_ts + timedelta(seconds=DISPATCH_RESERVATION_SECONDS)

    # 2. Publicación fuera de toda transacción de base de datos
    error_code: str | None = None
    try:
        publish(job_id)
    except Exception as exc:
        error_code = type(exc).__name__[:64] or "broker_error"
        _log.warning("Fallo al publicar job %s en broker: %s", job_id, error_code)

    # 3. Transacción corta: confirmar marca de publicación o reprogramar con backoff
    now_after = now()
    with session_scope(factory) as session:
        dispatch = session.scalars(
            select(JobDispatch)
            .where(
                JobDispatch.job_id == job_id,
                JobDispatch.dispatch_token == token,
            )
            .with_for_update()
        ).first()
        if dispatch is None:
            return error_code is None

        dispatch.publish_attempts += 1
        dispatch.dispatch_token = None
        dispatch.dispatch_expires_at = None

        if error_code is None:
            dispatch.last_published_at = now_after
            dispatch.last_error_code = None
            dispatch.next_publish_at = now_after + timedelta(
                seconds=REPUBLISH_UNACQUIRED_SECONDS
            )
            return True

        dispatch.last_error_code = error_code
        backoff = min(
            MAX_FAILURE_BACKOFF_SECONDS,
            MIN_FAILURE_BACKOFF_SECONDS * (2 ** max(0, dispatch.publish_attempts - 1)),
        )
        dispatch.next_publish_at = now_after + timedelta(seconds=backoff)
        return False


def dispatch_pending_outbox(
    factory: sessionmaker[Session],
    *,
    max_jobs: int = 50,
    publisher_fn: Callable[[int], None] | None = None,
) -> list[int]:
    """Examina jobs pendientes elegibles sin propietario y publica en orden enqueued_at, id."""
    now_ts = now()
    candidate_ids: list[int] = []

    with session_scope(factory) as session:
        # Asegurar que todo job PENDIENTE tenga fila en job_dispatches
        missing_jobs = session.scalars(
            select(Job)
            .outerjoin(JobDispatch, Job.id == JobDispatch.job_id)
            .where(
                Job.status == JobStatus.PENDIENTE,
                JobDispatch.job_id.is_(None),
            )
            .order_by(Job.enqueued_at.asc(), Job.id.asc())
            .limit(max_jobs)
        ).all()
        for j in missing_jobs:
            session.add(
                JobDispatch(
                    job_id=j.id,
                    next_publish_at=now_ts,
                    publish_attempts=0,
                )
            )
        if missing_jobs:
            session.flush()

        rows = session.scalars(
            select(Job.id)
            .join(JobDispatch, Job.id == JobDispatch.job_id)
            .where(
                Job.status == JobStatus.PENDIENTE,
                Job.worker_id.is_(None),
                or_(Job.available_at.is_(None), Job.available_at <= now_ts),
                JobDispatch.next_publish_at <= now_ts,
                or_(
                    JobDispatch.dispatch_expires_at.is_(None),
                    JobDispatch.dispatch_expires_at <= now_ts,
                ),
            )
            .order_by(Job.enqueued_at.asc(), Job.id.asc())
            .limit(max_jobs)
        ).all()
        candidate_ids = list(rows)

    published: list[int] = []
    for j_id in candidate_ids:
        if publish_job_dispatch(factory, j_id, publisher_fn=publisher_fn):
            published.append(j_id)
    return published


def reconcile_orphaned_leases(
    factory: sessionmaker[Session],
    *,
    current_boot_id: UUID = _BOOT_ID,
    current_deployment_id: str | None = None,
    is_executor_terminated: Callable[[JobAttempt | None], bool] | None = None,
) -> list[int]:
    """Detecta y recupera jobs EN_CURSO cuya lease venció y cuyo ejecutor terminó.

    - Si la terminación del ejecutor no puede comprobarse, conserva el job EN_CURSO
      para no liberar su slot ni producir escrituras concurrentes (ADR-018).
    - Si existe una operación de cadena en vuelo o incierta, conserva el estado de espera
      para reconciliación del oráculo sin repetir el cómputo ni lanzar otro envío a ciegas.
    - Si el ejecutor terminó durante el cómputo off-chain, cierra el intento como
      'interrumpido' (conservando el intervalo abierto con finished_at=None) y devuelve
      el job a PENDIENTE conservando sus checkpoints (inference_id, proof_id, checkpoint_context),
      o lo marca FALLIDO si agotó max_attempts.
    """
    now_ts = now()
    recovered_ids: list[int] = []

    with session_scope(factory) as session:
        expired_Ids = list(
            session.scalars(
                select(Job.id)
                .where(
                    Job.status == JobStatus.EN_CURSO,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at < now_ts,
                )
                .order_by(Job.enqueued_at.asc(), Job.id.asc())
            ).all()
        )

    for job_id in expired_Ids:
        with session_scope(factory) as session:
            job = session.scalars(
                select(Job)
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.EN_CURSO,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at < now_ts,
                )
                .with_for_update(skip_locked=True)
            ).first()
            if job is None:
                continue

            attempt = session.scalars(
                select(JobAttempt)
                .where(
                    JobAttempt.job_id == job.id,
                    JobAttempt.fence_token == job.fence_token,
                )
                .with_for_update()
            ).first()

            check_terminated = is_executor_terminated or (
                lambda att: default_is_executor_terminated(
                    att,
                    current_boot_id=current_boot_id,
                    current_deployment_id=current_deployment_id,
                )
            )
            if not check_terminated(attempt):
                _log.warning(
                    "Lease vencida para job %s pero no se pudo comprobar terminación del ejecutor (fence %s)",
                    job.id,
                    job.fence_token,
                )
                continue

            # Verificar si hay operación de cadena pendiente o incierta para este reclamo/job
            claim = session.get(Claim, job.claim_id)
            unresolved_op = session.scalars(
                select(OracleOperation)
                .where(
                    OracleOperation.claim_id == job.claim_id,
                    OracleOperation.status.in_(
                        [
                            OracleOperationStatus.FIRMADA.value,
                            OracleOperationStatus.ENVIADA.value,
                            OracleOperationStatus.INCIERTA.value,
                        ]
                    ),
                )
                .limit(1)
            ).first()

            if unresolved_op is not None or (
                claim is not None and claim.pending_chain_action is not None
            ):
                if attempt is not None and attempt.finished_at is None:
                    attempt.finished_at = now_ts
                    attempt.outcome = "espera_cadena"
                _log.info(
                    "Job %s conserva reserva por operación de cadena pendiente/incierta",
                    job.id,
                )
                continue

            if attempt is not None and attempt.finished_at is None:
                attempt.finished_at = now_ts
                attempt.outcome = "interrumpido"

            job.worker_id = None
            job.lease_expires_at = None

            if job.attempts >= job.max_attempts:
                job.status = JobStatus.FALLIDO
                job.finished_at = now_ts
                if job.last_failure_cause is None:
                    job.last_failure_cause = JobFailureReason.MEMORIA_AGOTADA
            else:
                job.status = JobStatus.PENDIENTE
                job.started_at = None
                dispatch = session.get(JobDispatch, job.id)
                if dispatch is None:
                    dispatch = JobDispatch(
                        job_id=job.id,
                        next_publish_at=now_ts,
                        publish_attempts=0,
                    )
                    session.add(dispatch)
                else:
                    dispatch.next_publish_at = now_ts
                    dispatch.dispatch_token = None
                    dispatch.dispatch_expires_at = None

            recovered_ids.append(job.id)

    return recovered_ids


class ReconcilerService:
    """Ejecuta el ciclo autónomo de reconciliación y despacho cada 10s en el worker."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        interval_seconds: float = RECONCILE_INTERVAL_SECONDS,
        publisher_fn: Callable[[int], None] | None = None,
        is_executor_terminated: Callable[[JobAttempt | None], bool] | None = None,
    ) -> None:
        self.factory = factory
        self.interval_seconds = interval_seconds
        self.publisher_fn = publisher_fn
        self.is_executor_terminated = is_executor_terminated
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def run_cycle(self) -> tuple[list[int], list[int]]:
        deployment_id = os.environ.get("RAILWAY_DEPLOYMENT_ID")
        recovered = reconcile_orphaned_leases(
            self.factory,
            current_boot_id=_BOOT_ID,
            current_deployment_id=deployment_id,
            is_executor_terminated=self.is_executor_terminated,
        )
        published = dispatch_pending_outbox(
            self.factory,
            publisher_fn=self.publisher_fn,
        )
        return recovered, published

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.is_set():
                try:
                    self.run_cycle()
                except Exception as exc:
                    _log.warning(
                        "Error en ciclo del reconciliador: %s", type(exc).__name__
                    )
                self._stop_event.wait(self.interval_seconds)

        self._thread = threading.Thread(
            target=_loop,
            name="queue-reconciler-service",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


class WorkerReconcilerStep(bootsteps.StartStopStep):
    """Valida el pool real y arranca el reconciliador antes del consumidor Celery."""

    requires = ("celery.worker.components:Pool",)

    def __init__(self, worker, **kwargs) -> None:
        self.reconciler: ReconcilerService | None = None
        super().__init__(worker, **kwargs)

    def start(self, worker) -> None:
        from app.dominio.queue_acquisition import validate_queue_control
        from app.queue.celery_app import worker_concurrency

        expected = worker_concurrency()
        pool_size = getattr(getattr(worker, "pool", None), "limit", None)
        if not isinstance(pool_size, int) or isinstance(pool_size, bool) or pool_size != expected:
            raise RuntimeError("El pool Celery no coincide con WORKER_CONCURRENCY")

        ctx = get_worker_context()
        with session_scope(ctx.session_factory) as session:
            validate_queue_control(session, pool_concurrency=pool_size)

        if self.reconciler is None:
            service = ReconcilerService(ctx.session_factory)
            service.start()
            self.reconciler = service

    def stop(self, worker) -> None:
        if self.reconciler is not None:
            self.reconciler.stop()
            self.reconciler = None

    def terminate(self, worker) -> None:
        self.stop(worker)

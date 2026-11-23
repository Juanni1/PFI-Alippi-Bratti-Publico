"""Tareas Celery de procesamiento de reclamos (PFI-139 / ADR-018)."""
from __future__ import annotations

import logging
import os
import threading
import uuid

from app.adaptadores.database import session_scope
from app.adaptadores.models import Configuration, Job
from app.adaptadores.oracle_coordinator import (
    OracleOperationUncertainError,
    WalletBlockedError,
)
from app.dominio.queue_acquisition import (
    QueueOwnershipLostError,
    acquire_job_by_id,
    finish_job_attempt,
    renew_lease,
)
from app.dominio.vocabulary import JobStatus
from app.dominio.worker import StageFailed, fail_job, process_job
from app.queue.celery_app import celery_app
from app.queue.worker_context import get_worker_context

_log = logging.getLogger("app.queue.tasks")

_BOOT_ID = uuid.uuid4()
_WORKER_ID = uuid.uuid4()

LEASE_DURATION_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 10.0


def _start_heartbeat(
    factory,
    *,
    job_id: int,
    worker_id: uuid.UUID,
    fence_token: int,
    interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    lease_duration_seconds: int = LEASE_DURATION_SECONDS,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def _loop() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                with session_scope(factory) as session:
                    ok = renew_lease(
                        session,
                        job_id=job_id,
                        worker_id=worker_id,
                        fence_token=fence_token,
                        lease_duration_seconds=lease_duration_seconds,
                    )
                if not ok:
                    _log.warning(
                        "Renovación de lease rechazada para job %s (worker %s, fence %s)",
                        job_id,
                        worker_id,
                        fence_token,
                    )
                    return
            except Exception as exc:
                _log.warning(
                    "Error al renovar lease para job %s: %s", job_id, type(exc).__name__
                )

    thread = threading.Thread(
        target=_loop,
        name=f"job-heartbeat-{job_id}-{fence_token}",
        daemon=True,
    )
    thread.start()
    return stop_event, thread


@celery_app.task(name="app.queue.process_job", bind=True)
def process_job_task(self, job_id: int) -> dict:
    """Procesa un job específico mediante adquisición atómica en PostgreSQL.

    Mensaje mínimo: {"job_id": <int>}.
    """
    ctx = get_worker_context()
    factory = ctx.session_factory

    worker_id = uuid.uuid4()
    worker_pid = os.getpid()
    deployment_id = os.environ.get("RAILWAY_DEPLOYMENT_ID")

    # 1. Intentar adquisición atómica en PostgreSQL
    with session_scope(factory) as session:
        acq = acquire_job_by_id(
            session,
            job_id,
            worker_id=worker_id,
            worker_boot_id=_BOOT_ID,
            worker_pid=worker_pid,
            deployment_id=deployment_id,
            lease_duration_seconds=LEASE_DURATION_SECONDS,
        )

    if not acq.acquired:
        _log.info(
            "Job %s no adquirido por %s: %s (earliest: %s)",
            job_id,
            worker_id,
            acq.reason,
            acq.earliest_job_id,
        )
        return {
            "job_id": job_id,
            "status": (
                "deferred"
                if acq.reason in ("out_of_order", "concurrency_limit_reached")
                else "ignored"
            ),
            "reason": acq.reason,
            "earliest_job_id": acq.earliest_job_id,
        }

    fence_token = acq.fence_token

    # 2. Obtener configuración para el cliente de cadena
    with session_scope(factory) as session:
        config = session.get(Configuration, acq.configuration_id)
        threshold = config.derivation_threshold if config else None
        policy = config.policy_mode if config else None

    chain = ctx.build_chain(threshold=threshold, policy=policy)

    # 3. Iniciar renovación periódica de lease (cada 10s, lease de 60s)
    stop_hb, hb_thread = _start_heartbeat(
        factory,
        job_id=job_id,
        worker_id=worker_id,
        fence_token=fence_token,
    )

    # 4. Ejecutar procesamiento del pipeline con verificación de propiedad por etapa
    try:
        processed = process_job(
            factory,
            job_id,
            publisher=ctx.publisher,
            engine=ctx.inference_engine,
            prover=ctx.proof_generator,
            chain=chain,
            worker_id=worker_id,
            fence_token=fence_token,
        )
        return {
            "job_id": job_id,
            "status": "completed",
            "claim_status": processed.status.value,
        }
    except QueueOwnershipLostError as error:
        _log.warning("Propiedad perdida durante ejecución de job %s: %s", job_id, error)
        return {
            "job_id": job_id,
            "status": "aborted",
            "reason": "ownership_lost",
        }
    except (OracleOperationUncertainError, WalletBlockedError) as error:
        _log.warning("Espera de cadena en job %s: %s", job_id, type(error).__name__)
        try:
            with session_scope(factory) as session:
                finish_job_attempt(
                    session,
                    job_id=job_id,
                    worker_id=worker_id,
                    fence_token=fence_token,
                    status=JobStatus.EN_CURSO,
                    outcome="espera_cadena",
                )
        except QueueOwnershipLostError:
            pass
        return {
            "job_id": job_id,
            "status": "chain_wait",
            "reason": type(error).__name__,
        }
    except StageFailed as error:
        _log.warning(
            "Job %s falló en etapa %s: %s", job_id, error.stage.value, error.reason.value
        )
        try:
            with session_scope(factory) as session:
                job = session.get(Job, job_id, with_for_update=True)
                if job is None or job.worker_id != worker_id or job.fence_token != fence_token:
                    raise QueueOwnershipLostError()
                fail_job(session, job, error)
                final_status = job.status
                finish_job_attempt(
                    session,
                    job_id=job_id,
                    worker_id=worker_id,
                    fence_token=fence_token,
                    status=final_status,
                    outcome=(
                        "reintentable"
                        if final_status == JobStatus.PENDIENTE
                        else "fallido"
                    ),
                )
        except QueueOwnershipLostError:
            return {
                "job_id": job_id,
                "status": "aborted",
                "reason": "ownership_lost",
            }
        return {
            "job_id": job_id,
            "status": "failed",
            "stage": error.stage.value,
            "reason": error.reason.value,
        }
    finally:
        stop_hb.set()
        hb_thread.join(timeout=1.0)

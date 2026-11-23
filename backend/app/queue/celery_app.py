"""Configuración de la instancia de Celery (PFI-139 / ADR-018).

Aplica las directivas acordadas:
- Prefork pool para aislamiento de cómputo ML/ZK.
- Fair dispatching con worker_prefetch_multiplier=1 y task_acks_late=True.
- task_reject_on_worker_lost=True para reencolar tareas si un hijo colapsa.
- Formato de serialización exclusivo JSON.
- Visibilidad extendida de Redis (3600s) para evitar redelivery durante cómputo largo.
- Sin result backend de Celery (PostgreSQL es la fuente de verdad de dominio).
"""
from __future__ import annotations

import os
from celery import Celery

REDIS_URL = (
    os.environ.get("REDIS_URL")
    or os.environ.get("CELERY_BROKER_URL")
    or "redis://127.0.0.1:6379/0"
)

celery_app = Celery("pfi_worker", broker=REDIS_URL)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": 3600},
    task_default_queue="pfi_jobs",
    worker_proc_alive_timeout=60.0,
    result_backend=None,
    task_always_eager=False,
    imports=("app.queue.tasks", "app.queue.reconciler"),
)

# Auto-descubrir tareas en app.queue.tasks
celery_app.autodiscover_tasks(["app.queue"])
celery_app.steps["worker"].add("app.queue.reconciler:WorkerReconcilerStep")


def worker_concurrency() -> int:
    """Acepta exclusivamente los tamaños de pool autorizados para este corte."""
    value = os.environ.get("WORKER_CONCURRENCY")
    if value not in ("1", "2"):
        raise RuntimeError("WORKER_CONCURRENCY debe ser 1 o 2")
    return int(value)


def worker_preflight() -> None:
    """Verifica broker, esquema y límite persistido antes de consumir mensajes."""
    pool_concurrency = worker_concurrency()
    if not (os.environ.get("REDIS_URL") or os.environ.get("CELERY_BROKER_URL")):
        raise RuntimeError("REDIS_URL o CELERY_BROKER_URL es obligatoria para el worker")

    from app.adaptadores.database import session_scope
    from app.dominio.queue_acquisition import validate_queue_control
    from app.queue.worker_context import get_worker_context

    ctx = get_worker_context()
    with session_scope(ctx.session_factory) as session:
        validate_queue_control(session, pool_concurrency=pool_concurrency)


if __name__ == "__main__":
    worker_preflight()

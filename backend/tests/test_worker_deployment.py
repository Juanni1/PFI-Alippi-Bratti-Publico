"""Contrato local del servicio worker Railway y su arranque Celery."""
from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager

import pytest

from app.queue import reconciler
from app.queue import celery_app as worker_module
from app.dominio.queue_acquisition import QueueControlMismatchError

ROOT = Path(__file__).resolve().parents[1]


def test_worker_is_separately_selectable_and_consumes_existing_queue():
    """La configuración aislada no sobrescribe la API y consume la tarea registrada."""
    worker = tomllib.loads((ROOT / "railway.worker.toml").read_text())
    api = tomllib.loads((ROOT / "railway.toml").read_text())
    from app.queue.celery_app import celery_app

    assert worker["build"] == api["build"]
    deploy = worker["deploy"]
    assert deploy["numReplicas"] == 1
    assert deploy["sleepApplication"] is False
    assert "healthcheckPath" not in deploy
    assert "preDeployCommand" not in deploy
    assert "startCommand" not in api["deploy"]
    assert deploy["startCommand"] == (
        "sh -c 'python -m app.queue.celery_app && exec celery -A app.queue.celery_app:celery_app worker "
        "--pool=prefork --concurrency=\"$WORKER_CONCURRENCY\" --queues=pfi_jobs --loglevel=INFO'"
    )
    assert celery_app.conf.task_default_queue == "pfi_jobs"
    assert "app.queue.tasks" in celery_app.conf.imports
    assert "app.queue.reconciler" in celery_app.conf.imports


def test_worker_preflight_requires_broker_and_persistent_limit(monkeypatch):
    """El comando previo al consumo falla cerrado con broker ausente o límite distinto."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
    monkeypatch.setenv("WORKER_CONCURRENCY", "1")
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        worker_module.worker_preflight()

    monkeypatch.setenv("REDIS_URL", "redis://broker.internal:6379/0")
    monkeypatch.setattr("app.queue.worker_context.get_worker_context", lambda: SimpleNamespace(session_factory=object()))

    @contextmanager
    def fake_scope(_factory):
        yield object()

    monkeypatch.setattr("app.adaptadores.database.session_scope", fake_scope)
    seen = []

    def validate(_session, *, pool_concurrency):
        seen.append(pool_concurrency)
        raise QueueControlMismatchError("Desacuerdo entre queue_control.concurrency (2) y pool Celery (1)")

    monkeypatch.setattr("app.dominio.queue_acquisition.validate_queue_control", validate)
    with pytest.raises(QueueControlMismatchError, match="Desacuerdo entre queue_control"):
        worker_module.worker_preflight()
    assert seen == [1]


def test_worker_preflight_accepts_matching_persisted_limit(monkeypatch):
    """La configuración previa válida permite continuar al consumidor."""
    monkeypatch.setenv("REDIS_URL", "redis://broker.internal:6379/0")
    monkeypatch.setenv("WORKER_CONCURRENCY", "2")
    monkeypatch.setattr("app.queue.worker_context.get_worker_context", lambda: SimpleNamespace(session_factory=object()))
    calls = []

    @contextmanager
    def fake_scope(_factory):
        yield object()

    monkeypatch.setattr("app.adaptadores.database.session_scope", fake_scope)
    monkeypatch.setattr(
        "app.dominio.queue_acquisition.validate_queue_control",
        lambda _session, *, pool_concurrency: calls.append(pool_concurrency),
    )
    worker_module.worker_preflight()
    assert calls == [2]


@pytest.mark.parametrize("value", [None, "", "0", "3", "two", " 2 "])
def test_worker_preflight_rejects_missing_or_invalid_concurrency(monkeypatch, value):
    """Un valor omitido o fuera de 1/2 impide el arranque antes de abrir la base."""
    monkeypatch.setenv("REDIS_URL", "redis://broker.internal:6379/0")
    if value is None:
        monkeypatch.delenv("WORKER_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("WORKER_CONCURRENCY", value)
    with pytest.raises(RuntimeError, match="WORKER_CONCURRENCY"):
        worker_module.worker_preflight()


def test_worker_bootstep_starts_one_reconciler_and_stops_it(monkeypatch):
    """El bootstep del supervisor valida y detiene el reconciliador una vez."""
    monkeypatch.setenv("WORKER_CONCURRENCY", "2")
    factory = object()
    worker = SimpleNamespace(pool=SimpleNamespace(limit=2))
    seen = []
    monkeypatch.setattr(reconciler, "get_worker_context", lambda: SimpleNamespace(session_factory=factory))

    @contextmanager
    def fake_session_scope(received):
        assert received is factory
        yield object()

    monkeypatch.setattr(reconciler, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        "app.dominio.queue_acquisition.validate_queue_control",
        lambda session, *, pool_concurrency: seen.append(pool_concurrency),
    )

    class FakeReconciler:
        def __init__(self, received):
            assert received is factory

        def start(self):
            seen.append("started")

        def stop(self):
            seen.append("stopped")

    monkeypatch.setattr(reconciler, "ReconcilerService", FakeReconciler)
    step = reconciler.WorkerReconcilerStep(worker)
    step.start(worker)
    step.stop(worker)
    step.stop(worker)
    assert seen == [2, "started", "stopped"]


def test_worker_bootstep_failure_propagates_through_celery_worker(monkeypatch):
    """Una discrepancia de pool detiene el arranque real del blueprint Celery."""
    monkeypatch.setenv("WORKER_CONCURRENCY", "1")
    from app.queue.celery_app import celery_app
    worker = celery_app.Worker(concurrency=2, pool="solo", loglevel="ERROR")
    step = next(s for s in worker.steps if isinstance(s, reconciler.WorkerReconcilerStep))
    worker.pool = SimpleNamespace(limit=2)
    worker.blueprint.on_start = None
    for other in worker.steps:
        if other is not step:
            monkeypatch.setattr(other, "start", lambda _worker: None)
    monkeypatch.setattr(reconciler, "get_worker_context", lambda: SimpleNamespace(session_factory=object()))

    @contextmanager
    def fake_session_scope(_factory):
        yield object()

    monkeypatch.setattr(reconciler, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        "app.dominio.queue_acquisition.validate_queue_control",
        lambda *_args, **_kwargs: pytest.fail("No debe aceptar un pool divergente"),
    )
    with pytest.raises(RuntimeError, match="WORKER_CONCURRENCY"):
        worker.blueprint.start(worker)
    assert step.reconciler is None


def test_worker_bootstep_rejects_persisted_mismatch(monkeypatch):
    """Un límite persistido diferente impide iniciar la recuperación."""
    monkeypatch.setenv("WORKER_CONCURRENCY", "2")
    worker = SimpleNamespace(pool=SimpleNamespace(limit=2))
    monkeypatch.setattr(reconciler, "get_worker_context", lambda: SimpleNamespace(session_factory=object()))

    @contextmanager
    def fake_session_scope(_factory):
        yield object()

    monkeypatch.setattr(reconciler, "session_scope", fake_session_scope)

    def validate(_session, *, pool_concurrency):
        assert pool_concurrency == 2
        raise QueueControlMismatchError("Desacuerdo entre queue_control.concurrency (1) y pool Celery (2)")

    monkeypatch.setattr("app.dominio.queue_acquisition.validate_queue_control", validate)
    step = reconciler.WorkerReconcilerStep(worker)
    with pytest.raises(QueueControlMismatchError, match="Desacuerdo entre queue_control"):
        step.start(worker)
    assert step.reconciler is None


def test_imagen_incluye_despliegue_completo_y_validado():
    import hashlib
    import json
    from scripts.pfi165_lifecycle import _check_anchor

    artifact_path = ROOT / "config/base-sepolia-deployment.json"
    fixture_path = ROOT / "tests/fixtures/pfi165-base-sepolia-deployment.json"
    anchor = json.loads((ROOT / "config/pfi165-approved-deployment.json").read_text())
    artifact = artifact_path.read_bytes()
    assert artifact == fixture_path.read_bytes()
    assert hashlib.sha256(artifact).hexdigest() == anchor["provenance"]["deploymentSha256"]
    network = json.loads((ROOT / "tests/fixtures/pfi165-base-sepolia-network.json").read_text())
    _check_anchor(json.loads(artifact), network)
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY config/base-sepolia-deployment.json ./config/base-sepolia-deployment.json" in dockerfile.splitlines()

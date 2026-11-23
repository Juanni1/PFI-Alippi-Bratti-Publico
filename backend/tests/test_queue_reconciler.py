"""Pruebas del despachador outbox y reconciliador de leases huérfanas (PFI-139 / ADR-018)."""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
import pytest
from sqlalchemy import select

from app.adaptadores.models import Claim, Configuration, Inference, Job, JobAttempt, JobDispatch, Model, QueueControl
from app.dominio.queue_acquisition import acquire_job_by_id, now, record_stage_progress
from app.dominio.vocabulary import ClaimStatus, DamageClass, JobFailureReason, JobStatus, PolicyMode
from app.queue.reconciler import (
    ReconcilerService,
    dispatch_pending_outbox,
    publish_job_dispatch,
    reconcile_orphaned_leases,
)


def _setup_config_and_qc(session, concurrency=2):
    cfg = Configuration(
        version=f"v-rec-{uuid.uuid4().hex[:6]}",
        queue_concurrency=concurrency,
        policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO,
    )
    session.add(cfg)
    session.flush()
    qc = QueueControl(id=1, configuration_id=cfg.id, concurrency=concurrency, paused=False)
    session.add(qc)
    session.flush()
    return cfg


def _create_claim_and_job(session, cfg_id, tracking_prefix="SN-REC", max_attempts=3, with_dispatch=True):
    claim = Claim(
        tracking_number=f"{tracking_prefix}-{uuid.uuid4().hex[:6]}",
        access_token_hash="hash",
        declared_amount=Decimal("500.00"),
        damage_description="Prueba reconciliador",
        status=ClaimStatus.REGISTRADO,
    )
    session.add(claim)
    session.flush()

    job = Job(claim_id=claim.id, configuration_id=cfg_id, max_attempts=max_attempts)
    session.add(job)
    session.flush()

    if with_dispatch:
        dispatch = JobDispatch(job_id=job.id, next_publish_at=now(), publish_attempts=0)
        session.add(dispatch)
        session.flush()

    return claim, job


def test_outbox_dispatcher_publishes_in_fifo_and_schedules_recheck(sesiones):
    """dispatch_pending_outbox publica en orden FIFO y reprograma revisión a 60s."""
    factory = sesiones
    with factory() as session:
        cfg = _setup_config_and_qc(session)
        _, j1 = _create_claim_and_job(session, cfg.id, "SN-OUT-1")
        _, j2 = _create_claim_and_job(session, cfg.id, "SN-OUT-2")
        j1_id, j2_id = j1.id, j2.id
        session.commit()

    published_order: list[int] = []
    res = dispatch_pending_outbox(factory, publisher_fn=lambda jid: published_order.append(jid))

    assert res == [j1_id, j2_id]
    assert published_order == [j1_id, j2_id]

    with factory() as session:
        d1 = session.get(JobDispatch, j1_id)
        assert d1.last_published_at is not None
        assert d1.publish_attempts == 1
        assert d1.last_error_code is None
        assert d1.dispatch_token is None
        assert d1.next_publish_at > now() + timedelta(seconds=45)

    # Un segundo ciclo inmediato no vuelve a publicar porque next_publish_at está a +60s
    res_second = dispatch_pending_outbox(factory, publisher_fn=lambda jid: published_order.append(jid))
    assert res_second == []


def test_outbox_dispatcher_handles_broker_failure_with_bounded_backoff(sesiones):
    """Una publicación fallida registra last_error_code y aplica backoff entre 30s y 300s."""
    factory = sesiones
    with factory() as session:
        cfg = _setup_config_and_qc(session)
        _, j1 = _create_claim_and_job(session, cfg.id, "SN-BROK-FAIL")
        j1_id = j1.id
        session.commit()

    def failing_broker(_jid: int) -> None:
        raise ConnectionError("Redis connection refused")

    ok = publish_job_dispatch(factory, j1_id, publisher_fn=failing_broker)
    assert ok is False

    with factory() as session:
        d1 = session.get(JobDispatch, j1_id)
        assert d1.last_published_at is None
        assert d1.publish_attempts == 1
        assert d1.last_error_code == "ConnectionError"
        assert d1.dispatch_token is None
        # Primer fallo -> backoff de 30s
        delta = (d1.next_publish_at - now()).total_seconds()
        assert 25 <= delta <= 35


def test_reconcile_orphaned_lease_preserves_checkpoints_and_requeues(sesiones):
    """Un job con lease vencida y ejecutor terminado preserva checkpoints y vuelve a PENDIENTE."""
    factory = sesiones
    dead_boot_id = uuid.uuid4()
    worker_id = uuid.uuid4()

    with factory() as session:
        cfg = _setup_config_and_qc(session)
        claim, job = _create_claim_and_job(session, cfg.id, "SN-ORPHAN-1", max_attempts=3)
        job_id, claim_id = job.id, claim.id
        session.commit()

    with factory() as session:
        acq = acquire_job_by_id(
            session,
            job_id,
            worker_id=worker_id,
            worker_boot_id=dead_boot_id,
            worker_pid=999999,
            lease_duration_seconds=60,
        )
        assert acq.acquired is True

        # Simular avance hasta extracción con checkpoint persistido
        model = Model(
            version="v1",
            extractor_path="",
            extractor_sha256="hash",
            head_path="",
            head_sha256="",
            onnx_opset=13,
            engine_version="v1",
            current=True,
        )
        session.add(model)
        session.flush()
        inf = Inference(
            claim_id=claim_id,
            model_id=model.id,
            feature_vector=b"\x00" * 16,
            extractor_artifact_hash="hash",
            logits=b"\x00" * 8,
            predicted_class=DamageClass.ABOLLADURA,
            confidence=0.91,
        )
        session.add(inf)
        session.flush()

        record_stage_progress(
            session,
            job_id=job_id,
            worker_id=worker_id,
            fence_token=acq.fence_token,
            stage="generacion",
            inference_id=inf.id,
            checkpoint_context={"configuration_id": cfg.id},
        )

        # Forzar vencimiento de la lease en el pasado
        j = session.get(Job, job_id)
        j.lease_expires_at = now() - timedelta(seconds=10)
        inf_id = inf.id
        session.commit()

    # Ejecutar reconciliador con otro boot_id (simulando reinicio de worker)
    published: list[int] = []
    svc = ReconcilerService(factory, publisher_fn=lambda jid: published.append(jid))
    recovered, dispatched = svc.run_cycle()

    assert recovered == [job_id]
    assert dispatched == [job_id]
    assert published == [job_id]

    with factory() as session:
        j = session.get(Job, job_id)
        assert j.status == JobStatus.PENDIENTE
        assert j.worker_id is None
        assert j.lease_expires_at is None
        assert j.attempts == 1
        # Checkpoints intactos
        assert j.inference_id == inf_id
        assert j.checkpoint_context == {"configuration_id": cfg.id}

        att = session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id)).one()
        assert att.outcome == "interrumpido"
        assert att.finished_at is not None
        # El último intervalo abierto (generacion) conserva finished_at=None tras caída (ADR-018)
        assert att.stage_intervals[-1]["stage"] == "generacion"
        assert att.stage_intervals[-1]["finished_at"] is None


def test_reconcile_orphaned_lease_marks_failed_when_max_attempts_exhausted(sesiones):
    """Si el job huérfano ya alcanzó max_attempts, pasa a FALLIDO definitivo."""
    factory = sesiones
    with factory() as session:
        cfg = _setup_config_and_qc(session)
        _, job = _create_claim_and_job(session, cfg.id, "SN-ORPHAN-MAX", max_attempts=1)
        job_id = job.id
        session.commit()

    with factory() as session:
        acquire_job_by_id(
            session,
            job_id,
            worker_id=uuid.uuid4(),
            worker_boot_id=uuid.uuid4(),
            worker_pid=999999,
        )
        j = session.get(Job, job_id)
        j.lease_expires_at = now() - timedelta(seconds=5)
        session.commit()

    recovered = reconcile_orphaned_leases(factory, current_boot_id=uuid.uuid4())
    assert recovered == [job_id]

    with factory() as session:
        j = session.get(Job, job_id)
        assert j.status == JobStatus.FALLIDO
        assert j.finished_at is not None
        assert j.last_failure_cause == JobFailureReason.MEMORIA_AGOTADA


def test_reconcile_retains_slot_if_executor_termination_unconfirmed(sesiones):
    """Si no se puede comprobar la terminación del ejecutor anterior, no se libera el slot."""
    factory = sesiones
    with factory() as session:
        cfg = _setup_config_and_qc(session, concurrency=1)
        _, job = _create_claim_and_job(session, cfg.id, "SN-UNCERTAIN-EXEC")
        job_id = job.id
        session.commit()

    with factory() as session:
        acquire_job_by_id(
            session,
            job_id,
            worker_id=uuid.uuid4(),
            worker_boot_id=uuid.uuid4(),
            worker_pid=12345,
            deployment_id="deploy-old-unverified",
        )
        j = session.get(Job, job_id)
        j.lease_expires_at = now() - timedelta(seconds=30)
        session.commit()

    # Deployment distinto sin confirmación -> no debe reasignar
    recovered = reconcile_orphaned_leases(
        factory,
        current_boot_id=uuid.uuid4(),
        current_deployment_id="deploy-new",
    )
    assert recovered == []

    with factory() as session:
        j = session.get(Job, job_id)
        assert j.status == JobStatus.EN_CURSO
        assert j.worker_id is not None

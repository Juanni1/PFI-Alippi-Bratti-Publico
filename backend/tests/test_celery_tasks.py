"""Pruebas de la tarea Celery process_job (PFI-139 / ADR-018)."""
from __future__ import annotations

import uuid
from decimal import Decimal
import pytest
from sqlalchemy import select

from app.adaptadores.models import Claim, Configuration, Evidence, Job, JobAttempt, QueueControl
from app.dominio.ports import (
    ChainClient,
    ChainReceipt,
    EvidencePublisher,
    InferenceEngine,
    InferenceResult,
    ProofGenerator,
    ProofResult,
)
from app.dominio.vocabulary import (
    ClaimStatus,
    DamageClass,
    JobFailureReason,
    JobStage,
    JobStatus,
    PolicyMode,
    TransitionCause,
)
from app.queue.tasks import process_job_task
from app.queue.worker_context import WorkerContext, set_worker_context


class FakePublisher:
    def __init__(self):
        self.storage = {}

    def publish(self, content: bytes, filename: str = "evidencia") -> str:
        cid = f"cid-{uuid.uuid4().hex[:8]}"
        self.storage[cid] = content
        return cid

    def retrieve(self, cid: str) -> bytes:
        return self.storage.get(cid, b"mock-content")


class FakeEngine:
    def artifacts_identity(self) -> str:
        return "test-model-sha256"

    def run(self, contents: list[bytes]) -> InferenceResult:
        return InferenceResult(
            feature_vector=b"\x00" * 32,
            logits=b"\x00" * 8,
            predicted_class=DamageClass.ABOLLADURA,
            confidence=0.85,
            extractor_artifact_hash="extractor-sha256",
            model_version="v1.0",
        )


class FakeProver:
    def artifacts_identity(self) -> str:
        return "test-circuit-sha256"

    def prove(self, inference: InferenceResult) -> ProofResult:
        return ProofResult(
            proof=b"fake-proof-bytes",
            public_inputs=b"fake-public-inputs",
            proof_hash="0x" + "aa" * 32,
            circuit_parameters={"k": 9},
            declared_visibility={"feature_vector": "private"},
            verified_locally=True,
        )


class FakeChainClient:
    def register_claim(self, number: str, evidence_id: str) -> ChainReceipt:
        return ChainReceipt(
            transaction_hash="0x" + "11" * 32,
            block_number=100,
            status=ClaimStatus.REGISTRADO,
            cause=None,
        )

    def submit_inference(self, number: str, proof: ProofResult, **kwargs) -> ChainReceipt:
        return ChainReceipt(
            transaction_hash="0x" + "22" * 32,
            block_number=101,
            status=ClaimStatus.APROBABLE,
            cause=TransitionCause.CLASIFICACION_FAVORABLE,
        )

    def settle(self, number: str, automatic: bool = True) -> ChainReceipt:
        return ChainReceipt(
            transaction_hash="0x" + "33" * 32,
            block_number=102,
            status=ClaimStatus.APROBADO,
            cause=TransitionCause.LIQUIDACION_AUTOMATICA,
        )


@pytest.fixture()
def worker_ctx(sesiones):
    publisher = FakePublisher()
    engine = FakeEngine()
    prover = FakeProver()
    chain = FakeChainClient()

    class TestWorkerContext(WorkerContext):
        def build_chain(self, *, threshold=None, policy=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO):
            return chain

    ctx = TestWorkerContext(
        session_factory=sesiones,
        publisher=publisher,
        inference_engine=engine,
        proof_generator=prover,
    )
    set_worker_context(ctx)
    yield ctx
    set_worker_context(None)


def test_process_job_task_eager_success(worker_ctx):
    """process_job_task adquiere y procesa el reclamo exitosamente."""
    factory = worker_ctx.session_factory
    publisher = worker_ctx.publisher

    content = b"fake-image-bytes"
    cid = publisher.publish(content)
    import hashlib
    digest = hashlib.sha256(content).hexdigest()

    with factory() as session:
        cfg = Configuration(
            version="v1",
            queue_concurrency=2,
            auto_settlement_threshold=0.80,
            derivation_threshold=0.70,
            policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO,
        )
        session.add(cfg)
        session.flush()

        qc = QueueControl(id=1, configuration_id=cfg.id, concurrency=2, paused=False)
        session.add(qc)

        claim = Claim(
            tracking_number=f"SN-TASK-{uuid.uuid4().hex[:6]}",
            access_token_hash="hash",
            declared_amount=Decimal("500.00"),
            damage_description="Prueba Celery",
            status=ClaimStatus.REGISTRADO,
        )
        session.add(claim)
        session.flush()

        evidence = Evidence(
            claim_id=claim.id,
            cid=cid,
            sha256=digest,
            filename="foto.jpg",
            mime_type="image/jpeg",
            size_bytes=len(content),
        )
        session.add(evidence)

        job = Job(claim_id=claim.id, configuration_id=cfg.id, max_attempts=3)
        session.add(job)
        session.commit()
        job_id = job.id

    # Ejecutar tarea directamente
    result = process_job_task.apply(args=[job_id]).get()

    assert result["status"] == "completed"
    assert result["claim_status"] == ClaimStatus.APROBADO.value

    with factory() as session:
        j = session.get(Job, job_id)
        assert j.status == JobStatus.COMPLETADO
        assert j.fence_token == 1
        assert j.attempts == 1

        att = session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id)).one()
        assert att.outcome == "completado"
        assert len(att.stage_intervals) >= 5
        assert all(i["started_at"] is not None and i["finished_at"] is not None for i in att.stage_intervals)


def test_process_job_task_deferred_out_of_order(worker_ctx):
    """process_job_task devuelve status deferred si el job llega fuera de orden FIFO."""
    factory = worker_ctx.session_factory

    with factory() as session:
        cfg = Configuration(version="v-order", queue_concurrency=1, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
        session.add(cfg)
        session.flush()

        qc = QueueControl(id=1, configuration_id=cfg.id, concurrency=1, paused=False)
        session.add(qc)

        c1 = Claim(tracking_number=f"SN-O1-{uuid.uuid4().hex[:6]}", access_token_hash="h1", declared_amount=Decimal("100"), damage_description="D1", status=ClaimStatus.REGISTRADO)
        c2 = Claim(tracking_number=f"SN-O2-{uuid.uuid4().hex[:6]}", access_token_hash="h2", declared_amount=Decimal("200"), damage_description="D2", status=ClaimStatus.REGISTRADO)
        session.add_all([c1, c2])
        session.flush()

        job1 = Job(claim_id=c1.id, configuration_id=cfg.id, max_attempts=3)
        job2 = Job(claim_id=c2.id, configuration_id=cfg.id, max_attempts=3)
        session.add_all([job1, job2])
        session.commit()
        j1_id, j2_id = job1.id, job2.id

    # Ejecutar tarea para job2 mientras job1 sigue pendiente
    result = process_job_task.apply(args=[j2_id]).get()

    assert result["status"] == "deferred"
    assert result["reason"] == "out_of_order"
    assert result["earliest_job_id"] == j1_id

    with factory() as session:
        j2 = session.get(Job, j2_id)
        assert j2.status == JobStatus.PENDIENTE
        assert j2.attempts == 0


def test_process_job_task_stage_failure(worker_ctx):
    """process_job_task maneja StageFailed registrando intento fallido y reintentable."""
    factory = worker_ctx.session_factory
    publisher = worker_ctx.publisher

    # Contenido con hash corrupto para forzar fallo en recuperación
    content = b"corrupt-image"
    cid = publisher.publish(content)
    wrong_digest = "00" * 32

    with factory() as session:
        cfg = Configuration(version="v-fail", queue_concurrency=1, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
        session.add(cfg)
        session.flush()

        qc = QueueControl(id=1, configuration_id=cfg.id, concurrency=1, paused=False)
        session.add(qc)

        claim = Claim(tracking_number=f"SN-FAIL-{uuid.uuid4().hex[:6]}", access_token_hash="hf", declared_amount=Decimal("100"), damage_description="DF", status=ClaimStatus.REGISTRADO)
        session.add(claim)
        session.flush()

        evidence = Evidence(claim_id=claim.id, cid=cid, sha256=wrong_digest, filename="foto.jpg", mime_type="image/jpeg", size_bytes=len(content))
        session.add(evidence)

        job = Job(claim_id=claim.id, configuration_id=cfg.id, max_attempts=3)
        session.add(job)
        session.commit()
        job_id = job.id

    result = process_job_task.apply(args=[job_id]).get()

    assert result["status"] == "failed"
    assert result["stage"] == "recuperacion"
    assert result["reason"] == "evidencia_no_recuperada"

    with factory() as session:
        j = session.get(Job, job_id)
        # Max attempts = 3, attempts = 1 -> vuelve a PENDIENTE para reintento
        assert j.status == JobStatus.PENDIENTE
        assert j.attempts == 1
        assert j.last_failure_cause == JobFailureReason.EVIDENCIA_NO_RECUPERADA

        att = session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id)).one()
        assert att.outcome == "reintentable"


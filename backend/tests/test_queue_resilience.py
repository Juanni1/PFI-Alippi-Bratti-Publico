"""Suite de integración y resiliencia de la cola durable (PFI-139 / ADR-018 — S6).

Cubre:
1. Concurrencia acotada compartida (respeto estricto de C=1 y C=2).
2. Pérdida de worker durante cómputo, rechazo por fencing token del zombie y recuperación con preservación de checkpoints.
3. Caída de Redis post-commit en POST /reclamos, recuperación vía job_dispatches hacia Redis real y verificación de payload mínimo JSON.
4. Redelivery de tarea ya completada (idempotencia y descarte sin consumir intento ni repetir efectos).
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.adaptadores.database import session_scope
from app.adaptadores.models import (
    Claim,
    Configuration,
    Evidence,
    Inference,
    Job,
    JobAttempt,
    JobDispatch,
    QueueControl,
)
from app.dominio.ports import (
    ChainReceipt,
    InferenceResult,
    ProofResult,
)
from app.dominio.queue_acquisition import (
    QueueOwnershipLostError,
    acquire_job_by_id,
    now,
    record_stage_progress,
)
from app.dominio.vocabulary import (
    ClaimStatus,
    DamageClass,
    JobStatus,
    PolicyMode,
    TransitionCause,
)
from app.main import app as aplicacion
from app.queue.celery_app import celery_app
from app.queue.reconciler import ReconcilerService
from app.queue.tasks import process_job_task
from app.queue.worker_context import WorkerContext, set_worker_context
from tests.conftest import DAMAGE_DESCRIPTION, PIXEL_PNG


REDIS_TEST_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:56379/0")


class InstrumentedEngine:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def artifacts_identity(self) -> str:
        return "resilience-extractor-sha256"

    def run(self, contents: list[bytes]) -> InferenceResult:
        with self._lock:
            self.calls += 1
        return InferenceResult(
            feature_vector=b"\x00" * (576 * 4),
            logits=b"\x00" * (6 * 4),
            predicted_class=DamageClass.ABOLLADURA,
            confidence=0.96,
            extractor_artifact_hash="resilience-extractor-sha256",
            model_version="v1.0-resilience",
        )


class InstrumentedProver:
    def __init__(self, delay_seconds: float = 0.0) -> None:
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.delay_seconds = delay_seconds
        self._lock = threading.Lock()

    def artifacts_identity(self) -> str:
        return "resilience-circuit-sha256"

    def prove(self, inference: InferenceResult) -> ProofResult:
        with self._lock:
            self.calls += 1
            self.active += 1
            if self.active > self.max_active:
                self.max_active = self.active
        try:
            if self.delay_seconds > 0:
                time.sleep(self.delay_seconds)
            return ProofResult(
                proof=b"resilience-proof",
                public_inputs=b"resilience-inputs",
                proof_hash="0x" + uuid.uuid4().hex + uuid.uuid4().hex,
                circuit_parameters={"k": 9, "simulated": True},
                declared_visibility={"feature_vector": "public"},
                verified_locally=True,
            )
        finally:
            with self._lock:
                self.active -= 1


class InstrumentedChain:
    def __init__(self) -> None:
        self.registrations = 0
        self.submissions = 0
        self.settlements = 0
        self._lock = threading.Lock()

    def register_claim(self, number: str, evidence_id: str) -> ChainReceipt:
        with self._lock:
            self.registrations += 1
            seq = self.registrations
        return ChainReceipt(
            transaction_hash="0x" + f"{seq:064x}",
            block_number=100 + seq,
            status=ClaimStatus.REGISTRADO,
            cause=None,
        )

    def submit_inference(self, number: str, proof: ProofResult, **kwargs) -> ChainReceipt:
        with self._lock:
            self.submissions += 1
            seq = self.submissions
        return ChainReceipt(
            transaction_hash="0x" + f"{1000 + seq:064x}",
            block_number=200 + seq,
            status=ClaimStatus.APROBABLE,
            cause=TransitionCause.CLASIFICACION_FAVORABLE,
        )

    def settle(self, number: str, automatic: bool = True) -> ChainReceipt:
        with self._lock:
            self.settlements += 1
            seq = self.settlements
        return ChainReceipt(
            transaction_hash="0x" + f"{2000 + seq:064x}",
            block_number=300 + seq,
            status=ClaimStatus.APROBADO,
            cause=TransitionCause.LIQUIDACION_AUTOMATICA,
        )


def _seed_jobs(factory, publicador, *, concurrency: int, count: int) -> list[int]:
    content = PIXEL_PNG
    cid = publicador.publish(content)
    digest = hashlib.sha256(content).hexdigest()
    job_ids: list[int] = []

    with session_scope(factory) as session:
        cfg = Configuration(
            version=f"v-res-{concurrency}-{uuid.uuid4().hex[:6]}",
            queue_concurrency=concurrency,
            derivation_threshold=0.90,
            auto_settlement_threshold=0.95,
            policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO,
        )
        session.add(cfg)
        session.flush()

        qc = session.get(QueueControl, 1)
        if qc is None:
            qc = QueueControl(id=1, configuration_id=cfg.id, concurrency=concurrency, paused=False)
            session.add(qc)
        else:
            qc.configuration_id = cfg.id
            qc.concurrency = concurrency
            qc.paused = False
        session.flush()

        for i in range(count):
            claim = Claim(
                tracking_number=f"SN-RES-{concurrency}-{i}-{uuid.uuid4().hex[:4]}",
                access_token_hash="hash",
                declared_amount=Decimal("150000.00"),
                damage_description=DAMAGE_DESCRIPTION,
                status=ClaimStatus.REGISTRADO,
            )
            session.add(claim)
            session.flush()
            session.add(
                Evidence(
                    claim_id=claim.id,
                    cid=cid,
                    sha256=digest,
                    filename="frente.png",
                    mime_type="image/png",
                    size_bytes=len(content),
                )
            )
            job = Job(claim_id=claim.id, configuration_id=cfg.id, max_attempts=3)
            session.add(job)
            session.flush()
            session.add(JobDispatch(job_id=job.id, next_publish_at=now(), publish_attempts=0))
            job_ids.append(job.id)

    return job_ids


@pytest.mark.parametrize("concurrency_limit", [1, 2])
def test_resilience_bounded_concurrency_c1_and_c2(concurrency_limit, sesiones, publicador):
    """Con C=1 y C=2, múltiples workers concurrentes nunca superan el límite de queue_control."""
    engine = InstrumentedEngine()
    prover = InstrumentedProver(delay_seconds=0.25)
    chain = InstrumentedChain()

    class _Ctx(WorkerContext):
        def build_chain(self, *, threshold=None, policy=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO):
            return chain

    ctx = _Ctx(
        session_factory=sesiones,
        publisher=publicador,
        inference_engine=engine,
        proof_generator=prover,
    )
    set_worker_context(ctx)

    try:
        job_ids = _seed_jobs(sesiones, publicador, concurrency=concurrency_limit, count=4)

        def worker_attempt(jid: int) -> dict:
            for _ in range(80):
                res = process_job_task.apply(args=[jid]).get()
                if res["status"] != "deferred":
                    return res
                time.sleep(0.02)
            return res

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(worker_attempt, job_ids))

        assert all(r["status"] == "completed" for r in results)
        assert prover.calls == 4
        assert prover.max_active <= concurrency_limit
        if concurrency_limit == 2:
            assert prover.max_active == 2
    finally:
        set_worker_context(None)


def test_resilience_worker_crash_mid_compute_and_lease_recovery(sesiones, publicador):
    """Si un worker muere en generacion, el reconciliador recupera la lease, preserva inferencia y rechaza al zombie."""
    engine = InstrumentedEngine()
    prover = InstrumentedProver()
    chain = InstrumentedChain()

    class _Ctx(WorkerContext):
        def build_chain(self, *, threshold=None, policy=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO):
            return chain

    ctx = _Ctx(
        session_factory=sesiones,
        publisher=publicador,
        inference_engine=engine,
        proof_generator=prover,
    )
    set_worker_context(ctx)

    try:
        job_ids = _seed_jobs(sesiones, publicador, concurrency=1, count=1)
        job_id = job_ids[0]
        zombie_worker_id = uuid.uuid4()
        zombie_boot_id = uuid.uuid4()

        # 1. El worker 1 (zombie) adquiere el job y completa registro_cadena + recuperacion + extraccion
        with session_scope(sesiones) as session:
            acq = acquire_job_by_id(
                session,
                job_id,
                worker_id=zombie_worker_id,
                worker_boot_id=zombie_boot_id,
                worker_pid=999991,
                lease_duration_seconds=60,
            )
            assert acq.acquired is True
            zombie_fence = acq.fence_token
            job = session.get(Job, job_id)
            claim = session.get(Claim, job.claim_id)
            config = session.get(Configuration, job.configuration_id)
            evidence = [(e.id, e.cid, e.sha256) for e in sorted(claim.evidence, key=lambda e: e.id)]
            ctx_dict = {
                "configuration_id": config.id,
                "evidence": [list(e) for e in evidence],
                "engine": engine.artifacts_identity(),
                "prover": prover.artifacts_identity(),
                "derivation_threshold": config.derivation_threshold,
                "auto_settlement_threshold": config.auto_settlement_threshold,
                "policy_mode": config.policy_mode.value,
                "amount_limit": None,
            }

        # Ejecutar extracción una vez y persistir el checkpoint
        inf_res = engine.run([PIXEL_PNG])
        assert engine.calls == 1

        from app.dominio.worker import _ensure_model

        with session_scope(sesiones) as session:
            model = _ensure_model(
                session,
                version=inf_res.model_version,
                extractor_hash=inf_res.extractor_artifact_hash,
            )
            job = session.get(Job, job_id)
            inf_row = Inference(
                claim_id=job.claim_id,
                model_id=model.id,
                feature_vector=inf_res.feature_vector,
                extractor_artifact_hash=inf_res.extractor_artifact_hash,
                logits=inf_res.logits,
                predicted_class=inf_res.predicted_class,
                confidence=inf_res.confidence,
            )
            session.add(inf_row)
            session.flush()
            record_stage_progress(
                session,
                job_id=job_id,
                worker_id=zombie_worker_id,
                fence_token=zombie_fence,
                stage="generacion",
                inference_id=inf_row.id,
                checkpoint_context=ctx_dict,
            )
            # Simular caída del worker 1 y vencimiento de su lease
            job.lease_expires_at = now() - timedelta(seconds=15)

        # 2. El reconciliador detecta la lease vencida, marca el intento interrumpido y republica
        republished: list[int] = []
        reconciler = ReconcilerService(sesiones, publisher_fn=lambda jid: republished.append(jid))
        recovered, dispatched = reconciler.run_cycle()
        assert recovered == [job_id]
        assert dispatched == [job_id]
        assert republished == [job_id]

        # 3. El worker 2 toma el job republicado y completa sin recalcular la inferencia
        result = process_job_task.apply(args=[job_id]).get()
        assert result["status"] == "completed"
        # ¡El motor de inferencia NO volvió a correr gracias al checkpoint preservado!
        assert engine.calls == 1
        assert prover.calls == 1

        # 4. Si el worker 1 (zombie) despierta e intenta avanzar con su fence_token obsoleto, es rechazado
        with session_scope(sesiones) as session:
            with pytest.raises(QueueOwnershipLostError):
                record_stage_progress(
                    session,
                    job_id=job_id,
                    worker_id=zombie_worker_id,
                    fence_token=zombie_fence,
                    stage="envio",
                )

        with session_scope(sesiones) as session:
            j = session.get(Job, job_id)
            assert j.status == JobStatus.COMPLETADO
            assert j.fence_token == 2
            attempts = session.scalars(
                select(JobAttempt).where(JobAttempt.job_id == job_id).order_by(JobAttempt.fence_token)
            ).all()
            assert [a.outcome for a in attempts] == ["interrumpido", "completado"]
    finally:
        set_worker_context(None)


def test_resilience_redis_down_post_commit_and_outbox_recovery(sesiones, publicador, archivos, monkeypatch):
    """Si Redis está caído tras el commit de POST /reclamos, el reclamo devuelve 201 y el reconciliador lo publica en Redis al volver."""
    r_client = redis.Redis.from_url(REDIS_TEST_URL)
    r_client.flushdb()

    prev_eager = celery_app.conf.task_always_eager
    prev_broker = celery_app.conf.broker_url
    celery_app.conf.task_always_eager = False

    # 1. Apuntar el publicador del API a un broker caído
    import app.queue.reconciler as reconciler_mod

    def dead_broker(_jid: int) -> None:
        raise redis.ConnectionError("Redis broker unreachable during POST /reclamos")

    monkeypatch.setattr(reconciler_mod, "_default_publisher", dead_broker)

    aplicacion.state.session_factory = sesiones
    aplicacion.state.publisher = publicador
    try:
        with TestClient(aplicacion) as client:
            aplicacion.state.session_factory = sesiones
            aplicacion.state.publisher = publicador
            response = client.post(
                "/reclamos",
                files=archivos,
                data={
                    "declaredAmount": "185000",
                    "damageDescription": DAMAGE_DESCRIPTION,
                },
            )
            assert response.status_code == 201
            number = response.json()["number"]

        # Verificar que el reclamo, job y job_dispatch se persistieron y registraron el error del broker
        with session_scope(sesiones) as session:
            claim = session.scalars(select(Claim).where(Claim.tracking_number == number)).one()
            job = session.scalars(select(Job).where(Job.claim_id == claim.id)).one()
            dispatch = session.get(JobDispatch, job.id)
            assert job.status == JobStatus.PENDIENTE
            assert dispatch is not None
            assert dispatch.last_published_at is None
            assert dispatch.publish_attempts == 1
            assert dispatch.last_error_code == "ConnectionError"
            # Adelantar next_publish_at para simular que transcurrió la ventana de backoff (30s)
            dispatch.next_publish_at = now() - timedelta(seconds=1)
            job_id = job.id

        # 2. Restaurar Redis real y ejecutar el ciclo del reconciliador
        celery_app.conf.broker_url = REDIS_TEST_URL

        def real_redis_publisher(jid: int) -> None:
            with celery_app.connection_for_write(url=REDIS_TEST_URL) as conn:
                process_job_task.apply_async(
                    args=[jid],
                    expires=300,
                    connection=conn,
                )

        svc = ReconcilerService(sesiones, publisher_fn=real_redis_publisher)
        _, published = svc.run_cycle()
        assert published == [job_id]

        # 3. Inspeccionar la cola en Redis real: verificar payload mínimo JSON {"job_id": id}
        raw_msg = r_client.lpop("pfi_jobs")
        assert raw_msg is not None
        envelope = json.loads(raw_msg.decode("utf-8"))
        import base64

        body_bytes = (
            base64.b64decode(envelope["body"])
            if envelope.get("properties", {}).get("body_encoding") == "base64"
            else envelope["body"].encode("utf-8")
        )
        body = json.loads(body_bytes.decode("utf-8"))
        # Formato protocolo v2 de Celery: [args, kwargs, embed] -> args == [job_id]
        assert body[0] == [job_id]
        assert body[1] == {}
        raw_text = body_bytes.decode("utf-8")
        assert "bafkrei" not in raw_text
        assert DAMAGE_DESCRIPTION not in raw_text

        with session_scope(sesiones) as session:
            dispatch = session.get(JobDispatch, job_id)
            assert dispatch.last_published_at is not None
            assert dispatch.last_error_code is None
            assert dispatch.publish_attempts == 2
    finally:
        celery_app.conf.task_always_eager = prev_eager
        celery_app.conf.broker_url = prev_broker


def test_resilience_redelivery_of_completed_job_is_idempotent(sesiones, publicador):
    """Un mensaje duplicado de un job ya completado se descarta sin incrementar attempts ni repetir efectos."""
    engine = InstrumentedEngine()
    prover = InstrumentedProver()
    chain = InstrumentedChain()

    class _Ctx(WorkerContext):
        def build_chain(self, *, threshold=None, policy=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO):
            return chain

    ctx = _Ctx(
        session_factory=sesiones,
        publisher=publicador,
        inference_engine=engine,
        proof_generator=prover,
    )
    set_worker_context(ctx)

    try:
        job_ids = _seed_jobs(sesiones, publicador, concurrency=1, count=1)
        job_id = job_ids[0]

        first = process_job_task.apply(args=[job_id]).get()
        assert first["status"] == "completed"
        assert engine.calls == 1
        assert prover.calls == 1
        assert chain.submissions == 1

        # Redelivery 1 y 2 del mismo job_id
        second = process_job_task.apply(args=[job_id]).get()
        third = process_job_task.apply(args=[job_id]).get()

        assert second["status"] == "ignored"
        assert second["reason"] == "job_not_pending"
        assert third["status"] == "ignored"
        assert third["reason"] == "job_not_pending"

        # Ningún efecto ni intento adicional
        assert engine.calls == 1
        assert prover.calls == 1
        assert chain.submissions == 1

        with session_scope(sesiones) as session:
            j = session.get(Job, job_id)
            assert j.status == JobStatus.COMPLETADO
            assert j.attempts == 1
            assert j.fence_token == 1
            attempts = session.scalars(select(JobAttempt).where(JobAttempt.job_id == job_id)).all()
            assert len(attempts) == 1
    finally:
        set_worker_context(None)


def test_resilience_real_celery_prefork_worker_end_to_end(
    postgres_url, sesiones, archivos, tmp_path, monkeypatch
):
    """Un proceso worker Celery real (pool prefork) consume desde Redis y completa el job en PostgreSQL."""
    import subprocess
    import sys
    from app.adaptadores.local_storage import LocalEvidencePublisher

    r_client = redis.Redis.from_url(REDIS_TEST_URL)
    r_client.flushdb()

    local_pub = LocalEvidencePublisher(str(tmp_path))
    prev_eager = celery_app.conf.task_always_eager
    prev_broker = celery_app.conf.broker_url
    celery_app.conf.task_always_eager = False
    celery_app.conf.broker_url = REDIS_TEST_URL

    import app.queue.reconciler as reconciler_mod

    def real_redis_publisher(jid: int) -> None:
        with celery_app.connection_for_write(url=REDIS_TEST_URL) as conn:
            process_job_task.apply_async(
                args=[jid],
                expires=300,
                connection=conn,
            )

    monkeypatch.setattr(reconciler_mod, "_default_publisher", real_redis_publisher)

    env = os.environ.copy()
    env["DATABASE_URL"] = postgres_url
    env["REDIS_URL"] = REDIS_TEST_URL
    env["WORKER_CONCURRENCY"] = "1"
    env["PUBLICADOR_EVIDENCIA"] = "local"
    env["RUTA_EVIDENCIA_LOCAL"] = str(tmp_path)

    worker_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "app.queue.celery_app",
            "worker",
            "--pool=prefork",
            "--concurrency=1",
            "--queues=pfi_jobs",
            "--loglevel=INFO",
            "--without-gossip",
            "--without-mingle",
            "--without-heartbeat",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        aplicacion.state.session_factory = sesiones
        aplicacion.state.publisher = local_pub
        with TestClient(aplicacion) as client:
            aplicacion.state.session_factory = sesiones
            aplicacion.state.publisher = local_pub
            resp = client.post(
                "/reclamos",
                files=archivos,
                data={
                    "declaredAmount": "185000",
                    "damageDescription": DAMAGE_DESCRIPTION,
                },
            )
            assert resp.status_code == 201
            number = resp.json()["number"]

        # Esperar hasta 15s a que el proceso Celery prefork consuma de Redis y complete el job
        deadline = time.monotonic() + 15.0
        completed = False
        while time.monotonic() < deadline:
            with session_scope(sesiones) as session:
                claim = session.scalars(select(Claim).where(Claim.tracking_number == number)).one()
                job = session.scalars(select(Job).where(Job.claim_id == claim.id)).one()
                if job.status == JobStatus.COMPLETADO:
                    completed = True
                    assert job.fence_token == 1
                    assert job.attempts == 1
                    att = session.scalars(select(JobAttempt).where(JobAttempt.job_id == job.id)).one()
                    assert att.outcome == "completado"
                    break
            time.sleep(0.25)

        if not completed:
            worker_proc.terminate()
            out, _ = worker_proc.communicate(timeout=5)
            pytest.fail(f"El worker Celery prefork no completó el job a tiempo. Output:\n{out}")
    finally:
        celery_app.conf.task_always_eager = prev_eager
        celery_app.conf.broker_url = prev_broker
        if worker_proc.poll() is None:
            worker_proc.terminate()
            try:
                worker_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker_proc.kill()


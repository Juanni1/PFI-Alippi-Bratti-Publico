"""Procesamiento serial con sesiones propias y checkpoints entre etapas.

No se mantiene una sesión durante IPFS, ONNX, EZKL o RPC. El mutex del
proceso API sigue delimitando concurrencia uno hasta PFI-139. Una excepción
ambigua de cadena conserva la reserva; su reconciliación corresponde a PFI-138.
"""
from __future__ import annotations

import hashlib
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adaptadores.database import session_scope
from app.adaptadores.models import Claim, Configuration, Evidence, Inference, Job, Model, Proof, Transition
from app.adaptadores.oracle_coordinator import OracleCoordinator
from app.dominio.claims import current_configuration
from app.dominio.ports import ChainClient, ChainReceipt, EvidencePublisher, InferenceEngine, InferenceResult, ProofGenerator, ProofResult
from app.dominio.vocabulary import ClaimStatus, JobFailureReason, JobStage, JobStatus, PolicyMode, TransitionCause

def now() -> datetime:
    return datetime.now(timezone.utc)


class StageFailed(Exception):
    """Fallo local reintentable sin repetir efectos confirmados de cadena."""
    def __init__(self, reason: JobFailureReason, stage: JobStage) -> None:
        self.reason, self.stage = reason, stage
        super().__init__(f"{stage.value}: {reason.value}")


@dataclass(frozen=True)
class ProcessedJob:
    job_id: int
    claim_id: int
    status: ClaimStatus
    cause: TransitionCause


def _ensure_model(session: Session, *, version: str, extractor_hash: str) -> Model:
    model = session.scalars(select(Model).where(Model.version == version)).first()
    if model is None:
        model = Model(version=version, extractor_path="", extractor_sha256=extractor_hash,
                      head_path="", head_sha256="", onnx_opset=13, engine_version=version, current=True)
        session.add(model)
        session.flush()
    return model


def take_next_job(session: Session) -> Job | None:
    """Adquisición FIFO corta; la capacidad sigue protegida por el mutex local."""
    job = session.scalars(select(Job).where(Job.status == JobStatus.PENDIENTE)
                          .order_by(Job.enqueued_at, Job.id).limit(1).with_for_update()).first()
    if job is None:
        return None
    if job.configuration_id is None:
        job.configuration_id = current_configuration(session).id
    job.status = JobStatus.EN_CURSO
    job.started_at = now()
    job.attempts += 1
    job.current_stage = "registro_cadena"
    session.flush()
    return job


def _job(
    session: Session,
    job_id: int,
    *,
    worker_id: Any = None,
    fence_token: int | None = None,
) -> Job:
    job = session.get(Job, job_id, with_for_update=True)
    if job is None or job.status != JobStatus.EN_CURSO:
        if worker_id is not None or fence_token is not None:
            from app.dominio.queue_acquisition import QueueOwnershipLostError
            raise QueueOwnershipLostError(f"El job {job_id} ya no está en curso.")
        raise RuntimeError("el job ya no está adquirido")
    if worker_id is not None and job.worker_id != worker_id:
        from app.dominio.queue_acquisition import QueueOwnershipLostError
        raise QueueOwnershipLostError(f"El job {job_id} ya no pertenece al worker {worker_id}")
    if fence_token is not None and job.fence_token != fence_token:
        from app.dominio.queue_acquisition import QueueOwnershipLostError
        raise QueueOwnershipLostError(f"El job {job_id} avanzó de fence_token (actual: {job.fence_token}, esperado: {fence_token})")
    if (worker_id is not None or fence_token is not None) and job.lease_expires_at is not None and job.lease_expires_at < now():
        from app.dominio.queue_acquisition import QueueOwnershipLostError
        raise QueueOwnershipLostError(f"La lease del job {job_id} expiró en {job.lease_expires_at}")
    return job


def _stage(
    factory: sessionmaker[Session],
    job_id: int,
    stage: str,
    *,
    worker_id: Any = None,
    fence_token: int | None = None,
) -> None:
    with session_scope(factory) as session:
        if worker_id is not None and fence_token is not None:
            from app.dominio.queue_acquisition import record_stage_progress
            record_stage_progress(
                session,
                job_id=job_id,
                worker_id=worker_id,
                fence_token=fence_token,
                stage=stage,
            )
        else:
            _job(session, job_id).current_stage = stage

def _load_inference(session: Session, inference_id: int) -> InferenceResult:
    row = session.get(Inference, inference_id)
    return InferenceResult(row.feature_vector, row.logits, row.predicted_class,
                           row.confidence, row.extractor_artifact_hash, row.model.version)


def _load_proof(session: Session, proof_id: int) -> ProofResult:
    row = session.get(Proof, proof_id)
    return ProofResult(row.proof, row.public_inputs, row.proof_hash,
                       row.circuit_parameters, row.declared_visibility, row.verified_locally)


def process_job(factory: sessionmaker[Session], job_id: int, *, publisher: EvidencePublisher,
                engine: InferenceEngine, prover: ProofGenerator, chain: ChainClient,
                worker_id: Any = None, fence_token: int | None = None) -> ProcessedJob:
    """Solo valores/IDs salen de una sesión; nunca una lectura ORM a través del I/O."""
    # La identidad se obtiene fuera de toda conexión; puede leer artefactos grandes.
    engine_identity, prover_identity = engine.artifacts_identity(), prover.artifacts_identity()
    with session_scope(factory) as session:
        job = _job(session, job_id, worker_id=worker_id, fence_token=fence_token)
        claim = session.get(Claim, job.claim_id)
        config = session.get(Configuration, job.configuration_id)
        number, claim_id = claim.tracking_number, claim.id
        evidence = [(e.id, e.cid, e.sha256) for e in sorted(claim.evidence, key=lambda e: e.id)]
        entry = session.scalars(select(Transition).where(
            Transition.claim_id == claim_id, Transition.from_status.is_(None)
        ).order_by(Transition.id)).first()
        registered = entry is not None and entry.transaction_hash is not None
        amount_above = config.amount_limit is not None and claim.declared_amount > config.amount_limit
        auto_threshold, policy_mode = config.auto_settlement_threshold, config.policy_mode
        context = {"configuration_id": config.id, "evidence": [list(e) for e in evidence],
                   "engine": engine_identity, "prover": prover_identity,
                   "derivation_threshold": config.derivation_threshold,
                   "auto_settlement_threshold": auto_threshold, "policy_mode": policy_mode.value,
                   "amount_limit": str(config.amount_limit) if config.amount_limit is not None else None}
        if job.checkpoint_context != context:
            job.inference_id = job.proof_id = None
            job.checkpoint_context = context
        inference = _load_inference(session, job.inference_id) if job.inference_id else None
        proof = _load_proof(session, job.proof_id) if job.proof_id else None

    if not evidence:
        raise StageFailed(JobFailureReason.EVIDENCIA_NO_RECUPERADA, JobStage.RECUPERACION)
    coordinator = chain if isinstance(chain, OracleCoordinator) else OracleCoordinator(factory, chain)
    if not registered:
        _stage(factory, job_id, "registro_cadena", worker_id=worker_id, fence_token=fence_token)
        receipt = coordinator.register_claim(
            claim_id=claim_id,
            number=number,
            evidence_id=evidence[0][1],
            configuration_id=config.id,
            job_id=job_id,
        )
    _stage(factory, job_id, "recuperacion", worker_id=worker_id, fence_token=fence_token)
    try:
        recovered = [(eid, publisher.retrieve(cid), digest) for eid, cid, digest in evidence]
    except Exception as error:
        raise StageFailed(JobFailureReason.EVIDENCIA_NO_RECUPERADA, JobStage.RECUPERACION) from error
    checks = [(eid, hashlib.sha256(content).hexdigest() == digest) for eid, content, digest in recovered]
    with session_scope(factory) as session:
        _job(session, job_id, worker_id=worker_id, fence_token=fence_token)
        for eid, matches in checks:
            session.get(Evidence, eid).digest_verified = matches
    if not all(matches for _, matches in checks):
        raise StageFailed(JobFailureReason.EVIDENCIA_NO_RECUPERADA, JobStage.RECUPERACION)

    if inference is None:
        _stage(factory, job_id, "extraccion", worker_id=worker_id, fence_token=fence_token)
        try:
            inference = engine.run([content for _, content, _ in recovered])
        except Exception as error:
            raise StageFailed(JobFailureReason.ARTEFACTO_NO_DECLARADO, JobStage.EXTRACCION) from error
        with session_scope(factory) as session:
            job = _job(session, job_id, worker_id=worker_id, fence_token=fence_token)
            model = _ensure_model(session, version=inference.model_version, extractor_hash=inference.extractor_artifact_hash)
            row = Inference(claim_id=claim_id, model_id=model.id, feature_vector=inference.feature_vector,
                            extractor_artifact_hash=inference.extractor_artifact_hash, logits=inference.logits,
                            predicted_class=inference.predicted_class, confidence=inference.confidence)
            session.add(row)
            session.flush()
            job.inference_id = row.id

    if proof is None:
        _stage(factory, job_id, "generacion", worker_id=worker_id, fence_token=fence_token)
        try:
            proof = prover.prove(inference)
        except Exception as error:
            raise StageFailed(JobFailureReason.MEMORIA_AGOTADA, JobStage.GENERACION) from error
        _stage(factory, job_id, "verificacion_local", worker_id=worker_id, fence_token=fence_token)
        if not proof.verified_locally:
            raise StageFailed(JobFailureReason.VERIFICACION_LOCAL_FALLIDA, JobStage.VERIFICACION_LOCAL)
        with session_scope(factory) as session:
            job = _job(session, job_id, worker_id=worker_id, fence_token=fence_token)
            row = Proof(inference_id=job.inference_id, proof=proof.proof, public_inputs=proof.public_inputs,
                        proof_hash=proof.proof_hash, circuit_parameters=proof.circuit_parameters,
                        declared_visibility=proof.declared_visibility, verified_locally=True)
            session.add(row)
            session.flush()
            job.proof_id = row.id

    _stage(factory, job_id, "envio", worker_id=worker_id, fence_token=fence_token)
    receipt = coordinator.submit_inference(
        claim_id=claim_id,
        number=number,
        proof=proof,
        confidence=inference.confidence,
        amount_above_threshold=amount_above,
        rules_consistent=True,
        configuration_id=config.id,
        job_id=job_id,
    )
    if (receipt.status == ClaimStatus.APROBABLE and auto_threshold is not None
            and inference.confidence >= auto_threshold
            and policy_mode != PolicyMode.CONFIRMACION_HUMANA_OBLIGATORIA):
        _stage(factory, job_id, "liquidacion", worker_id=worker_id, fence_token=fence_token)
        receipt = coordinator.settle(
            claim_id=claim_id,
            number=number,
            automatic=True,
            configuration_id=config.id,
            job_id=job_id,
        )
    with session_scope(factory) as session:
        job = _job(session, job_id, worker_id=worker_id, fence_token=fence_token)
        job.last_failure_cause = None
        if worker_id is not None and fence_token is not None:
            from app.dominio.queue_acquisition import finish_job_attempt
            finish_job_attempt(
                session,
                job_id=job_id,
                worker_id=worker_id,
                fence_token=fence_token,
                status=JobStatus.COMPLETADO,
                outcome="completado",
            )
        else:
            job.status, job.finished_at = JobStatus.COMPLETADO, now()
    return ProcessedJob(job_id, claim_id, receipt.status, receipt.cause)


def fail_job(session: Session, job: Job, error: StageFailed) -> None:
    """Conserva checkpoints, intentos y FIFO; no revierte etapas confirmadas."""
    job.last_failure_cause = error.reason
    if job.attempts >= job.max_attempts:
        job.status, job.finished_at = JobStatus.FALLIDO, now()
    else:
        job.status, job.started_at = JobStatus.PENDIENTE, None
    job.current_stage = error.stage.value


def run_next(factory: sessionmaker[Session], *, publisher: EvidencePublisher,
             engine: InferenceEngine, prover: ProofGenerator, chain: ChainClient) -> ProcessedJob | None:
    with session_scope(factory) as session:
        job = take_next_job(session)
        if job is None:
            return None
        job_id = job.id
    try:
        return process_job(factory, job_id, publisher=publisher, engine=engine, prover=prover, chain=chain)
    except StageFailed as error:
        with session_scope(factory) as session:
            fail_job(session, _job(session, job_id), error)
        return None


def decode_vector(raw: bytes) -> list[float]:
    return array("f", raw).tolist()

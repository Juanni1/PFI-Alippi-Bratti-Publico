"""Contexto de adaptadores y dependencias para el worker Celery (PFI-139 / ADR-018)."""
from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy.orm import Session, sessionmaker

from app.adaptadores.chain import build_chain_client
from app.adaptadores.database import (
    create_database_engine,
    create_session_factory,
    verify_schema,
)
from app.adaptadores.inference import build_inference_engine
from app.adaptadores.local_storage import build_evidence_publisher
from app.adaptadores.proving import build_proof_generator
from app.dominio.ports import (
    ChainClient,
    EvidencePublisher,
    InferenceEngine,
    ProofGenerator,
)
from app.dominio.vocabulary import PolicyMode


@dataclass
class WorkerContext:
    session_factory: sessionmaker[Session]
    publisher: EvidencePublisher
    inference_engine: InferenceEngine
    proof_generator: ProofGenerator

    def build_chain(
        self,
        *,
        threshold: float | None = None,
        policy: PolicyMode = PolicyMode.AUTOMATICO_CON_ESCALAMIENTO,
    ) -> ChainClient:
        return build_chain_client(derivation_threshold=threshold, policy_mode=policy)


_CURRENT_CONTEXT: WorkerContext | None = None


def get_worker_context() -> WorkerContext:
    global _CURRENT_CONTEXT
    if _CURRENT_CONTEXT is None:
        motor = create_database_engine()
        verify_schema(motor)
        factory = create_session_factory(motor)
        publisher = build_evidence_publisher()
        engine = build_inference_engine()
        prover = build_proof_generator()
        _CURRENT_CONTEXT = WorkerContext(
            session_factory=factory,
            publisher=publisher,
            inference_engine=engine,
            proof_generator=prover,
        )
    return _CURRENT_CONTEXT


def set_worker_context(ctx: WorkerContext | None) -> None:
    global _CURRENT_CONTEXT
    _CURRENT_CONTEXT = ctx

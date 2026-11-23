"""Esquemas de la frontera, espejo de `contract/openapi.yaml`.

Hay un esquema de respuesta **por perfil** y no uno solo con campos
opcionales. La diferencia no es de estilo: sobre un esquema único con todo
opcional, la restricción de la matriz de visibilidad no se puede afirmar ni
negar, y RF-46 pide justamente que se pueda.

Las propiedades van en inglés y en camelCase porque son identificadores de la
frontera; los valores de estado y de causa van en español porque son
vocabulario del producto.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, WithJsonSchema
from pydantic.alias_generators import to_camel

from app.dominio.vocabulary import ClaimStatus, DamageClass, PolicyMode, TransitionCause


def _iso_utc(value: datetime) -> str:
    """Marca de tiempo con huso explícito (formato Instant con terminación 'Z').

    El repositorio guarda las marcas de tiempo con zona horaria UTC (`models.now`).
    Para preservar compatibilidad con marcas legacy sin zona horaria, si tzinfo es
    None se interpreta como UTC. La salida serializada garantiza siempre el huso UTC
    con sufijo 'Z'.
    """
    con_huso = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return con_huso.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

Instant = Annotated[
    datetime,
    PlainSerializer(_iso_utc, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]


class Schema(BaseModel):
    """Base común: alias en camelCase y campos desconocidos rechazados, que es
    la traducción de `additionalProperties: false` del contrato."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        use_enum_values=True,
    )


# ---------------------------------------------------------------------------
# Valores sin medir
# ---------------------------------------------------------------------------


class UnmeasuredNumber(Schema):
    """Traducción al contrato de la marca [VERIFICAR] de la bóveda: `value` nulo
    con `measured: false`, y `requirement` nombrando la medición que lo
    cerraría. Nunca un número plausible."""

    value: float | None
    measured: bool
    requirement: str | None = None
    unit: str | None = None

    @classmethod
    def de(
        cls, valor: float | None, *, requirement: str | None = None, unit: str | None = None
    ) -> UnmeasuredNumber:
        return cls(
            value=valor, measured=valor is not None, requirement=requirement, unit=unit
        )


class UnmeasuredString(Schema):
    value: str | None
    measured: bool
    requirement: str | None = None

    @classmethod
    def de(cls, valor: str | None, *, requirement: str | None = None) -> UnmeasuredString:
        return cls(value=valor, measured=valor is not None, requirement=requirement)


class Problem(Schema):
    title: str
    detail: str | None = None


# ---------------------------------------------------------------------------
# Vocabulario compartido
# ---------------------------------------------------------------------------


class ClaimantStatus(Schema):
    """Etiqueta y causa ya redactadas. Se copian del diccionario y no se
    reescriben por pantalla: es lo que hace que RNF-35 se aplique del lado del
    servidor."""

    label: str
    cause: str


class TimelineEntry(Schema):
    occurred_at: Instant
    label: str
    cause: str


class TransitionOut(Schema):
    """`Transition` del contrato. El nombre lleva sufijo para no chocar con el
    modelo relacional homónimo."""

    from_: ClaimStatus | None = Field(default=None, alias="from")
    to: ClaimStatus
    cause: TransitionCause | None = None
    occurred_at: Instant
    block_number: int | None = None
    transaction_hash: str | None = None
    proof_hash: str | None = None


class DerivationCause(Schema):
    id: TransitionCause
    text: str | None = None
    amount_was_not_the_cause: bool | None = None


# ---------------------------------------------------------------------------
# MK-01 y MK-02
# ---------------------------------------------------------------------------


class RejectedFileOut(Schema):
    filename: str
    reason: Literal["formato_no_admitido", "excede_tamano_maximo", "cantidad_excedida"]
    message: str


class EvidenceRejection(Schema):
    rejected: list[RejectedFileOut]
    accepted_count: int | None = None


class RegistrationFailure(Schema):
    message: str
    retryable: bool
    submission_preserved: bool | None = None


class Receipt(Schema):
    evidence_id: str
    transaction_hashes: list[str]
    proof_hash: str | None = None
    public_trace_url: str | None = None


class SubmittedSummary(Schema):
    damage_description: str
    photo_count: int
    declared_amount: float

class RegistrationReceipt(Schema):
    number: str
    registered_at: Instant
    status: ClaimantStatus
    submitted: SubmittedSummary
    receipt: Receipt
    access_token: str
    on_chain_scope: str | None = None


# ---------------------------------------------------------------------------
# MK-03 — por perfil
# ---------------------------------------------------------------------------


class ClaimantClaimDetail(Schema):
    incident_date: date | None = None
    damage_description: str | None
    photo_count: int
    declared_amount: float


class QueuePosition(Schema):
    position: int | None = None
    estimated_wait: UnmeasuredString | None = None


class ClaimantResolution(Schema):
    """Del revisor figura el rol y nunca la identidad: es la celda que la matriz
    le niega al reclamante."""

    role: str | None = None
    justification: str | None = None
    justification_hash: str | None = None


class ClaimantClaim(Schema):
    number: str
    status: ClaimantStatus
    timeline: list[TimelineEntry]
    claim: ClaimantClaimDetail
    queue: QueuePosition | None = None
    resolution: ClaimantResolution | None = None
    appeal_notice: str | None = None
    explanation_available: bool | None = None
    receipt: Receipt


class EvidenceFile(Schema):
    filename: str
    size_bytes: int | None = None
    sha256: str
    url: str | None = None


class EvidenceDetail(Schema):
    evidence_id: str
    files: list[EvidenceFile]


class OperatorInference(Schema):
    predicted_class: DamageClass
    confidence: float
    model_version: str | None = None
    artifact_hashes: dict[str, str] | None = None


class ReviewerInference(OperatorInference):
    """Único esquema con logits: cuando la confianza cae bajo el umbral, la
    segunda clase más probable es la información que sostiene la decisión."""

    logits: list[float] = Field(min_length=6, max_length=6)


class InternalResolution(Schema):
    """Nulo mientras el reclamo no esté resuelto, y nulo también cuando el
    cierre fue una liquidación automática: esa transición no tiene autor a quién
    registrar, que es lo que el vocabulario de causa permite reconocer."""

    reviewer: str | None = None
    role: str | None = None
    recorded_at: Instant | None = None
    justification: str | None = None
    justification_hash: str | None = None


class Assignment(Schema):
    unopened: bool | None = None
    assigned_at: Instant | None = None


class ReviewerClaim(Schema):
    number: str
    status_id: ClaimStatus
    status: ClaimantStatus
    evidence: EvidenceDetail
    inference: ReviewerInference | None = None
    cause: DerivationCause | None = None
    trace: list[TransitionOut]
    declared_amount: float
    damage_description: str | None
    registered_by: str | None
    assignment: Assignment | None = None
    available_resolutions: list[Literal["favorable", "desfavorable"]] | None = None
    resolution: InternalResolution | None = None


class OperatorClaim(Schema):
    """Mismo reclamo que el del revisor y sin el vector de logits: es la celda
    de la matriz que da sentido a filtrar por campo y no por pantalla (RF-46)."""

    number: str
    status_id: ClaimStatus
    status: ClaimantStatus
    evidence: EvidenceDetail
    inference: OperatorInference | None = None
    declared_amount: float
    damage_description: str | None
    registered_by: str | None
    trace: list[TransitionOut]
    settleable: bool | None = None
    resolution: InternalResolution | None = None


# ---------------------------------------------------------------------------
# MK-04 — explicación
# ---------------------------------------------------------------------------


class PublishedFeatureVector(Schema):
    published: bool
    dimension: int
    notice: str | None = None


class ExplanationEvidence(Schema):
    file_count: int
    submitted_at: Instant
    published_feature_vector: PublishedFeatureVector


class ExplanationModel(Schema):
    predicted_class: DamageClass
    confidence: float
    accredited: bool | None = None


class ExplanationProof(Schema):
    proven_segment: str
    replicated_segment: str
    disclaimer: str
    verification_failed: bool | None = None
    transaction_hash: str | None = None


class ThresholdComparison(Schema):
    """El valor observado siempre existe; el umbral puede no estar medido, y en
    ese caso viaja nulo y la pantalla dibuja la marca."""

    observed: float
    threshold: UnmeasuredNumber
    satisfied: bool | None = None


class ExplanationRule(Schema):
    confidence_against_threshold: ThresholdComparison | None = None
    amount_against_limit: ThresholdComparison | None = None
    auto_settlement_applied: bool | None = None


class ClaimExplanation(Schema):
    evidence: ExplanationEvidence
    model: ExplanationModel
    proof: ExplanationProof
    rule: ExplanationRule
    outcome: ClaimantStatus


# ---------------------------------------------------------------------------
# MK-05 — bandeja
# ---------------------------------------------------------------------------


class PublicConfiguration(Schema):
    max_files: UnmeasuredNumber
    max_file_size_mb: UnmeasuredNumber
    amount_limit: UnmeasuredNumber
    accepted_formats: list[Literal["image/jpeg", "image/png"]]


class Configuration(Schema):
    version: str
    policy_mode: PolicyMode
    policy_mode_label: str | None = None
    derivation_threshold: UnmeasuredNumber
    auto_settlement_threshold: UnmeasuredNumber
    amount_limit: UnmeasuredNumber
    max_file_size_mb: UnmeasuredNumber
    max_files: UnmeasuredNumber
    max_retries: UnmeasuredNumber
    accepted_formats: list[Literal["image/jpeg", "image/png"]] | None = None


class InboxRow(Schema):
    number: str
    status_id: ClaimStatus
    cause: DerivationCause
    predicted_class: DamageClass | None = None
    confidence: float | None = None
    accredited: bool | None = None
    runner_up_class: DamageClass | None = None
    declared_amount: float | None = None
    unopened: bool
    arrived_at: Instant | None = None


class ReviewInbox(Schema):
    assigned_total: int
    unopened: int
    ordering: Literal["llegada"]
    human_confirmation_banner: str | None = None
    rows: list[InboxRow]
    configuration: Configuration


class ResolutionRequest(Schema):
    favorable: bool
    justification: str = Field(min_length=1)


class TransitionReceipt(Schema):
    number: str
    status_id: ClaimStatus
    transition: TransitionOut


# ---------------------------------------------------------------------------
# MK-07 — cola
# ---------------------------------------------------------------------------


class QueueCounters(Schema):
    pending: int
    running: int
    failed: int
    last_position_wait: UnmeasuredString | None = None


class RunningJob(Schema):
    number: str
    stage: str
    elapsed_seconds: float
    typical_seconds: float | None = None


class PendingJob(Schema):
    number: str
    position: int
    enqueued_at: Instant
    estimated_start_at: Instant | None = None


class FailedJob(Schema):
    number: str
    reason: str
    attempt: int
    max_attempts: UnmeasuredNumber | None = None
    claim_status_id: ClaimStatus
    definitive: bool | None = None


class SettleableClaim(Schema):
    """Un reclamo en `Aprobable`, esperando la liquidación de RF-37.

    El operador no tenía por dónde llegar a liquidar: la bandeja de RF-36 es del
    revisor y CU-06 no pide su equivalente, así que la única vía era conocer el
    número de antemano. La lista viaja con el estado de la cola porque es lo que
    el operador ya mira, y no agrega un control: sigue siendo lectura.
    """

    number: str
    declared_amount: float | None = None
    approvable_since: Instant | None = None


class OracleOperation(Schema):
    """Operación persistida del oráculo para trazabilidad y reconciliación (ADR-018)."""

    operation_id: str
    number: str
    operation: Literal["register_claim", "submit_inference", "resolve", "settle"]
    status: Literal["preparada", "firmada", "enviada", "confirmada", "revertida", "incierta", "fallida_pre_envio"]
    transaction_hash: str | None = None
    block_number: int | None = None
    last_error_code: str | None = None


class ReconciliationRequest(Schema):
    operation_id: UUID


class QueueState(Schema):
    counters: QueueCounters
    concurrency: int | None = None
    running: list[RunningJob]
    pending: list[PendingJob]
    failed: list[FailedJob]
    settleable: list[SettleableClaim] = []
    processor_stopped: bool | None = None
    oracle_blocked: bool = False
    oracle_operations: list[OracleOperation] = []

# ---------------------------------------------------------------------------
# MK-08 — traza pública
# ---------------------------------------------------------------------------


class PublicEvidence(Schema):
    evidence_id: str
    retrievable: bool
    digest_matches: bool | None = None


class ProofArtifacts(Schema):
    proof_hash: str | None = None
    proof_url: str | None = None
    public_inputs_url: str | None = None
    circuit_parameters: dict[str, Any] | None = None
    verifying_key_url: str | None = None
    verifier_address: str | None = None


class ReplicationArtifacts(Schema):
    """Artefactos públicos para replicar la extracción fuera del circuito
    (RF-53, RNF-27, RNF-32 y RNF-34). Con el motor simulado (`stub:`), las URL
    viajan nulas porque no existe artefacto publicado que descargar."""

    extractor_onnx_url: str | None = None
    extractor_onnx_hash: str | None = None
    preprocessing_spec_url: str | None = None
    runtime_version: str | None = None


class PublicArtifacts(Schema):
    proof: ProofArtifacts
    replication: ReplicationArtifacts


class PublicTrace(Schema):
    number: str
    status_id: ClaimStatus
    status_is_terminal: bool | None = None
    provenance: str
    transitions: list[TransitionOut]
    evidence: PublicEvidence
    feature_vector: list[float]
    artifacts: PublicArtifacts
    not_exposed: list[str]
    image_disclosure_notice: str | None = None

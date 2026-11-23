"""Modelos del repositorio relacional: las nueve entidades del DER.

Correspondencia de nombres. El DER del documento nombra sus atributos en
español; el código los nombra en inglés siguiendo la misma convención que el
contrato OpenAPI para la frontera (propiedades en inglés, valores de
vocabulario en español). Cada clase declara en su docstring de qué entidad del
DER sale, para que la correspondencia siga siendo mecánica.

Dos propiedades del modelo que no se leen del esquema y conviene tener a la
vista. La primera es que `Claim.status` es réplica y no fuente: el contrato
define el estado y la base lo copia para responder consultas sin una lectura
de la cadena por pantalla, de modo que una divergencia entre ambos no es un
empate a resolver por criterio sino un defecto, y vale el de la cadena. La
segunda es que la asociación entre `Transition` y `Configuration` deja
asentado bajo qué versión de umbrales se clasificó cada reclamo; sin ella un
cambio de configuración vuelve irreproducible toda decisión anterior.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import uuid
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.dominio.vocabulary import (
    ClaimStatus,
    DamageClass,
    JobFailureReason,
    JobStatus,
    OracleOperationStatus,
    OracleOperationType,
    PinStatus,
    PolicyMode,
    TransitionCause,
)


def now() -> datetime:
    """Marca temporal en UTC aware. Las fechas se guardan siempre en UTC."""
    return datetime.now(timezone.utc)

def enum_column(enum_class: type[StrEnum]) -> SqlEnum:
    """Columna de enum que guarda el valor y no el nombre del miembro.

    Importa cuál de los dos: el valor es el identificador del vocabulario
    (`Registrado`, `fijado`) y es lo que viaja por la API y lo que el
    diccionario de frontera comparte con Solidity y con TypeScript. El nombre
    del miembro es una convención de Python y no significa nada afuera.

    Sin `native_enum=False` SQLite guardaría un CHECK con los valores fijados al
    crear la tabla, y agregar un estado obligaría a recrearla.
    """
    return SqlEnum(
        enum_class,
        native_enum=False,
        values_callable=lambda clase: [miembro.value for miembro in clase],
        length=40,
    )


class Base(DeclarativeBase):
    pass


class Claim(Base):
    """Entidad Reclamo del DER. Raíz del modelo: de ella cuelgan la evidencia,
    las inferencias, los jobs, las transiciones y las revisiones."""

    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tracking_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    # Token de capacidad de RF-47. Se guarda el digesto y nunca el token: quien
    # lea la base no puede hacerse pasar por el reclamante.
    access_token_hash: Mapped[str] = mapped_column(String(64))

    declared_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    # Identidad privada del operador que cargó el relato en nombre del
    # reclamante. Las altas anónimas y los reclamos anteriores quedan nulos.
    registered_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=None
    )
    # Texto privado del relato del reclamante. Nulo únicamente en reclamos
    # anteriores a la migración; ningún artefacto público lo recibe.
    damage_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ClaimStatus] = mapped_column(enum_column(ClaimStatus), index=True)

    # Identificador del reclamo en la cadena. Nulo mientras el registro on-chain
    # no se haya asentado.
    onchain_claim_id: Mapped[str | None] = mapped_column(String(66), default=None)

    # Reserva previa al RPC. Un timeout no la libera ni autoriza otro envío.
    pending_chain_action: Mapped[str | None] = mapped_column(String(32), default=None)

    # Modela la apelación. La máquina de estados no ofrece vía de reenvío dentro
    # del mismo expediente, así que el recurso del asegurado es un reclamo nuevo
    # enlazado al anterior. Entra ahora aunque su caso de uso quede fuera del
    # prototipo: agregarlo después obligaría a una migración.
    origin_claim_id: Mapped[int | None] = mapped_column(
        ForeignKey("claims.id"), default=None
    )

    # La asignación se guarda acá y no en Review: la revisión registra la
    # decisión, que ocurre después y puede no ocurrir nunca, así que apoyar la
    # bandeja sobre ella dejaría sin representar el reclamo que espera.
    assigned_reviewer: Mapped[str | None] = mapped_column(String(64), default=None)
    assigned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )

    evidence: Mapped[list[Evidence]] = relationship(back_populates="claim")
    inferences: Mapped[list[Inference]] = relationship(back_populates="claim")
    jobs: Mapped[list[Job]] = relationship(back_populates="claim")
    transitions: Mapped[list[Transition]] = relationship(back_populates="claim")
    reviews: Mapped[list[Review]] = relationship(back_populates="claim")
    oracle_operations: Mapped[list[OracleOperation]] = relationship(back_populates="claim")


class Evidence(Base):
    """Entidad Evidencia del DER. De una a cinco fotografías por reclamo."""

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"), index=True)

    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    cid: Mapped[str] = mapped_column(String(80), index=True)

    # La fijación se registra dos veces porque hay dos custodios. El nodo propio
    # publica y fija; el servicio remoto recibe los mismos bytes y computa el CID
    # por su cuenta, y el adaptador aborta la publicación si los dos no
    # coinciden. Sin este registro, RNF-20 no tiene contra qué comprobar el
    # segundo origen. Los tres campos son elaboración propia: el catálogo pide
    # registrar la fijación local y nada dice del servicio remoto.
    pin_status: Mapped[PinStatus] = mapped_column(
        enum_column(PinStatus), default=PinStatus.PENDIENTE
    )
    remote_pin_status: Mapped[PinStatus] = mapped_column(
        enum_column(PinStatus), default=PinStatus.PENDIENTE
    )
    remote_cid_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    # Resultado de la comprobación de RF-10: si el digesto del contenido
    # recuperado por su identificador coincide con el que se calculó al
    # recibirlo. Nulo mientras nadie lo haya recuperado todavía, que es
    # distinto de haberlo recuperado y encontrado una diferencia.
    digest_verified: Mapped[bool | None] = mapped_column(Boolean, default=None)

    tensor_commitment: Mapped[str | None] = mapped_column(String(80), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )

    claim: Mapped[Claim] = relationship(back_populates="evidence")


class Inference(Base):
    """Entidad Inferencia del DER.

    Guarda el vector de características junto con el hash del artefacto que lo
    produjo, y esa combinación es lo que vuelve ejecutable la replicación del
    extractor: sin saber con qué versión se produjo el vector, el tercero que
    reejecuta no tendría contra qué contrastar su resultado.
    """

    __tablename__ = "inferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"), index=True)
    model_id: Mapped[int] = mapped_column(ForeignKey("models.id"))

    feature_vector: Mapped[bytes] = mapped_column(LargeBinary)
    extractor_artifact_hash: Mapped[str] = mapped_column(String(64))
    logits: Mapped[bytes] = mapped_column(LargeBinary)
    predicted_class: Mapped[DamageClass] = mapped_column(enum_column(DamageClass))
    confidence: Mapped[float] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )

    claim: Mapped[Claim] = relationship(back_populates="inferences")
    model: Mapped[Model] = relationship(back_populates="inferences")
    proof: Mapped[Proof | None] = relationship(back_populates="inference")


class Proof(Base):
    """Entidad Prueba del DER. Cuelga de la inferencia que acredita."""

    __tablename__ = "proofs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inference_id: Mapped[int] = mapped_column(
        ForeignKey("inferences.id"), unique=True, index=True
    )

    proof: Mapped[bytes] = mapped_column(LargeBinary)
    public_inputs: Mapped[bytes] = mapped_column(LargeBinary)
    proof_hash: Mapped[str] = mapped_column(String(66), index=True)
    circuit_parameters: Mapped[dict] = mapped_column(JSON)

    # Qué se declaró público y qué privado al preparar el circuito. Sin este
    # registro la garantía que la prueba sostiene no se puede reconstruir.
    declared_visibility: Mapped[dict] = mapped_column(JSON)

    verified_locally: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )

    inference: Mapped[Inference] = relationship(back_populates="proof")


class Job(Base):
    """Entidad Job del DER. La cola vive en la base y no en un servicio aparte."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"), index=True)

    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus), default=JobStatus.PENDIENTE, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer)
    last_failure_cause: Mapped[JobFailureReason | None] = mapped_column(
        enum_column(JobFailureReason), default=None
    )

    current_stage: Mapped[str | None] = mapped_column(String(32), default=None)
    configuration_id: Mapped[int | None] = mapped_column(
        ForeignKey("configurations.id"), default=None
    )
    inference_id: Mapped[int | None] = mapped_column(
        ForeignKey("inferences.id"), default=None
    )
    proof_id: Mapped[int | None] = mapped_column(ForeignKey("proofs.id"), default=None)
    checkpoint_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    worker_id: Mapped[UUID | None] = mapped_column(Uuid, default=None)
    fence_token: Mapped[int] = mapped_column(BigInteger, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    available_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )

    claim: Mapped[Claim] = relationship(back_populates="jobs")
    oracle_operations: Mapped[list[OracleOperation]] = relationship(back_populates="job")
    dispatch: Mapped[JobDispatch | None] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )
    attempts_history: Mapped[list[JobAttempt]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class Transition(Base):
    """Entidad Transicion del DER. Copia local de lo que el contrato asentó."""

    __tablename__ = "transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"), index=True)
    configuration_id: Mapped[int] = mapped_column(ForeignKey("configurations.id"))

    # El origen es nulo en la entrada al sistema: adr-001 enumera el registro
    # aparte de la matriz y no es una transición.
    from_status: Mapped[ClaimStatus | None] = mapped_column(
        enum_column(ClaimStatus), default=None
    )
    to_status: Mapped[ClaimStatus] = mapped_column(enum_column(ClaimStatus))
    cause: Mapped[TransitionCause | None] = mapped_column(
        enum_column(TransitionCause), default=None
    )

    proof_hash: Mapped[str | None] = mapped_column(String(66), default=None)
    transaction_hash: Mapped[str | None] = mapped_column(String(66), default=None)
    block_number: Mapped[int | None] = mapped_column(Integer, default=None)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )

    claim: Mapped[Claim] = relationship(back_populates="transitions")
    configuration: Mapped[Configuration] = relationship(back_populates="transitions")
    review: Mapped[Review | None] = relationship(back_populates="transition")


class Review(Base):
    """Entidad Revision del DER. La identidad del revisor, la marca temporal y
    la justificación quedan acá; en la cadena va solo su digesto (RF-39)."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"), index=True)
    transition_id: Mapped[int | None] = mapped_column(
        ForeignKey("transitions.id"), default=None
    )

    reviewer_identity: Mapped[str] = mapped_column(String(64))
    decision: Mapped[bool] = mapped_column(Boolean)
    justification: Mapped[str] = mapped_column(Text)
    justification_hash: Mapped[str] = mapped_column(String(66))
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )

    claim: Mapped[Claim] = relationship(back_populates="reviews")
    transition: Mapped[Transition | None] = relationship(back_populates="review")


class Model(Base):
    """Entidad Modelo del DER. Versiona los dos artefactos de features-afuera:
    el extractor, que corre fuera del circuito y se acredita por replicación, y
    la cabeza, que es lo único que entra al circuito y se acredita por prueba."""

    __tablename__ = "models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[str] = mapped_column(String(32), unique=True)

    extractor_path: Mapped[str] = mapped_column(String(255))
    extractor_sha256: Mapped[str] = mapped_column(String(64))
    head_path: Mapped[str] = mapped_column(String(255))
    head_sha256: Mapped[str] = mapped_column(String(64))
    onnx_opset: Mapped[int] = mapped_column(Integer)
    engine_version: Mapped[str] = mapped_column(String(32))
    current: Mapped[bool] = mapped_column(Boolean, default=False)

    inferences: Mapped[list[Inference]] = relationship(back_populates="model")


class Configuration(Base):
    """Entidad Configuracion del DER.

    Los umbrales son nulos cuando la medición que los cerraría todavía no se
    hizo. Es la traducción al modelo de la marca [VERIFICAR] de la bóveda, y la
    razón por la que la columna admite nulo en lugar de traer un valor por
    defecto plausible: un número inventado acá saldría por la API como si
    estuviera medido.
    """

    __tablename__ = "configurations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[str] = mapped_column(String(32), unique=True)

    derivation_threshold: Mapped[float | None] = mapped_column(default=None)
    auto_settlement_threshold: Mapped[float | None] = mapped_column(default=None)
    amount_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), default=None)

    policy_mode: Mapped[PolicyMode] = mapped_column(
        enum_column(PolicyMode), default=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO
    )

    max_files: Mapped[int] = mapped_column(Integer, default=5)
    max_file_size_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    max_job_attempts: Mapped[int | None] = mapped_column(Integer, default=None)

    # Uno en el prototipo. Dejó de ser consecuencia de una medición: con el
    # circuito actual el pico es de 0,79 GB y la imposición aritmética
    # desapareció, así que hoy es decisión de diseño y su umbral efectivo está
    # sin medir.
    queue_concurrency: Mapped[int] = mapped_column(Integer, default=1)

    contract_address: Mapped[str | None] = mapped_column(String(42), default=None)
    verifier_address: Mapped[str | None] = mapped_column(String(42), default=None)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now
    )

    transitions: Mapped[list[Transition]] = relationship(back_populates="configuration")


class OracleOperation(Base):
    """Entidad Intencion del DER (ADR-018).

    Representa una intención inmutable previa a cualquier I/O externo.
    Registra los bytes firmados, el nonce y el hash calculados antes del
    broadcast.
    """

    __tablename__ = "oracle_operations"
    __table_args__ = (
        Index(
            "uq_oracle_operations_nonce",
            "chain_id",
            "sender_address",
            "nonce",
            unique=True,
            postgresql_where=text("nonce IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    operation_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), default=None, index=True)
    configuration_id: Mapped[int] = mapped_column(ForeignKey("configurations.id"))
    operation: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    chain_id: Mapped[int] = mapped_column(BigInteger)
    sender_address: Mapped[str] = mapped_column(String(42))
    contract_address: Mapped[str] = mapped_column(String(42))
    nonce: Mapped[int | None] = mapped_column(BigInteger, default=None)
    raw_signed_transaction: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    transaction_hash: Mapped[str | None] = mapped_column(String(66), unique=True, default=None, index=True)
    status: Mapped[str] = mapped_column(String(32), default=OracleOperationStatus.PREPARADA.value)
    broadcast_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    block_number: Mapped[int | None] = mapped_column(BigInteger, default=None)
    last_error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    claim: Mapped[Claim] = relationship(back_populates="oracle_operations")
    job: Mapped[Job | None] = relationship(back_populates="oracle_operations")
    configuration: Mapped[Configuration] = relationship()


class OracleWallet(Base):
    """Entidad Wallet del DER (ADR-018).

    Conserva la exclusión durable de la wallet por red/dirección. Sobrevive a
    la pérdida del proceso o del lock consultivo de sesión.
    """

    __tablename__ = "oracle_wallets"

    chain_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sender_address: Mapped[str] = mapped_column(String(42), primary_key=True)
    active_operation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("oracle_operations.id"), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    active_operation: Mapped[OracleOperation | None] = relationship()


class QueueControl(Base):
    """Control de concurrencia y pausa de la cola (ADR-018).

    Tabla singleton (id=1) que define la concurrencia efectiva compartida entre
    procesos y si la cola está pausada.
    """

    __tablename__ = "queue_control"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    configuration_id: Mapped[int] = mapped_column(ForeignKey("configurations.id"))
    concurrency: Mapped[int] = mapped_column(Integer, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )

    configuration: Mapped[Configuration] = relationship()


class JobDispatch(Base):
    """Registro de despacho outbox por job (ADR-018).

    Creado en la misma transacción que el reclamo y el job. Permite desacoplar
    el commit HTTP del envío a Redis y recuperar mensajes perdidos.
    """

    __tablename__ = "job_dispatches"

    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    next_publish_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, index=True
    )
    last_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    dispatch_token: Mapped[UUID | None] = mapped_column(Uuid, default=None)
    dispatch_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    job: Mapped[Job] = relationship(back_populates="dispatch")


class JobAttempt(Base):
    """Historial de adquisiciones e intentos de procesamiento (ADR-018).

    Cada fila registra un intento con su token de fencing, worker ejecutor,
    tiempos de cada etapa y resultado final.
    """

    __tablename__ = "job_attempts"
    __table_args__ = (
        Index("uq_job_attempts_job_fence", "job_id", "fence_token", unique=True),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    fence_token: Mapped[int] = mapped_column(BigInteger)
    worker_id: Mapped[UUID] = mapped_column(Uuid)
    configuration_id: Mapped[int] = mapped_column(ForeignKey("configurations.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    outcome: Mapped[str | None] = mapped_column(String(32), default=None)
    stage_intervals: Mapped[list | dict] = mapped_column(JSON)
    worker_boot_id: Mapped[UUID] = mapped_column(Uuid)
    worker_pid: Mapped[int] = mapped_column(Integer)
    deployment_id: Mapped[str | None] = mapped_column(String(128), default=None)

    job: Mapped[Job] = relationship(back_populates="attempts_history")
    configuration: Mapped[Configuration] = relationship()


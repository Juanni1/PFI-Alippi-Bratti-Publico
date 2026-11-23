"""Servicio de reclamos: registro, consulta, resolución y liquidación.

Lo que el contrato decide, este módulo lo replica en la base para poder
responder consultas sin una lectura de la cadena por pantalla. Las funciones
que asientan una transición reciben el cliente de cadena y guardan su recibo.
El adaptador simulado devuelve `transaction_hash` nulo; esa marca distingue
una transición sin respaldo on-chain y no se rellena con un valor plausible.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.adaptadores.oracle_coordinator import OracleCoordinator
from app.dominio.ports import ChainClient, ChainReceipt, EvidencePublisher
from app.dominio.state_machine import (
    TransitionNotAllowed,
    resolution_cause,
    settlement_cause,
    validate_transition,
)
from app.dominio.vocabulary import ClaimStatus, JobStatus, OracleOperationType, PinStatus, TransitionCause
from app.adaptadores.models import Claim, Configuration, Evidence, Job, JobDispatch, Review, Transition, now

FORMATOS_ADMITIDOS = frozenset({"image/jpeg", "image/png"})
MINIMO_DE_FOTOGRAFIAS = 1
MAX_DAMAGE_DESCRIPTION_CODE_POINTS = 2000


class InvalidDamageDescription(Exception):
    """El relato del daño falta, queda vacío o supera el máximo permitido."""


class ClaimNotFound(Exception):
    """No existe un reclamo con ese número."""


@dataclass(frozen=True)
class RejectedFile:
    """Rechazo de un archivo (RF-02). El rechazo es por archivo y no por envío,
    para que corregir el que falla no obligue a recargar los que pasaron."""

    filename: str
    reason: str
    message: str


class EvidenceRejected(Exception):
    """Uno o más archivos no se admiten. Lleva la lista completa: un error con
    un único texto obligaría a la SPA a interpretarlo para saber qué fila
    marcar."""

    def __init__(self, rejected: list[RejectedFile], accepted_count: int) -> None:
        self.rejected = rejected
        self.accepted_count = accepted_count
        super().__init__(f"{len(rejected)} archivo(s) rechazado(s)")


@dataclass
class UploadedFile:
    filename: str
    mime_type: str
    content: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.content)


@dataclass
class RegisteredClaim:
    """Lo que el registro devuelve. El token en claro sale una sola vez, acá:
    de la base solo se puede recuperar su digesto."""

    claim: Claim
    access_token: str
    evidence: list[Evidence] = field(default_factory=list)
    job: Job | None = None


# ---------------------------------------------------------------------------
# Configuración vigente
# ---------------------------------------------------------------------------


# Umbrales de la configuración inicial, adoptados en adr-014 sobre la corrida
# del 2026-08-22. No son valores plausibles elegidos por criterio: salen de la
# curva de RNF-15 medida sobre las 785 muestras de prueba que el clasificador
# no vio, y su fundamento está en el archivo de evidencia de la bóveda.
#
# Derivación en 0,90: resuelve solo el 47,6 % de los casos con 1,60 % de error
# medido (cota superior del 2,88 % con 95 % de confianza). Subir a 0,95 no
# compra garantía —su cero errores sobre 111 casos solo permite afirmar «por
# debajo de 2,70 %»— y deja la cobertura en un tercio.
#
# Liquidación automática en 0,95, estrictamente por encima: RF-51 prohíbe que
# quede por debajo del de derivación, y adr-007 necesita que sean distintos
# para que exista el tramo en que el reclamo queda Aprobable y lo liquida una
# persona.
UMBRAL_DE_DERIVACION = 0.90
UMBRAL_DE_LIQUIDACION_AUTOMATICA = 0.95

# Los tres de abajo no salen de una medición sino de una decisión, tomada el
# 2026-08-23 y registrada en `70-mockups/00-criterios.md`. Viajaban nulos y las
# pantallas dibujaban la marca [VERIFICAR] en su lugar, que era correcto
# mientras nadie los hubiera elegido y dejó de serlo cuando se eligieron.
#
# 10 MB por archivo cubre con margen la fotografía de un celular actual, que va
# de 3 a 8 MB, y deja en pie el rechazo de 14,2 MB que MK-01b dibuja.
LIMITE_DE_TAMANO_POR_ARCHIVO_BYTES = 10 * 1024 * 1024
# Tres intentos por job: absorbe el fallo transitorio de red o de recuperación
# de la evidencia sin dejar un job condenado girando en la cola, que con
# concurrencia 1 le bloquea el turno a todos los demás.
MAXIMO_DE_INTENTOS_POR_JOB = 3
# El monto por encima del cual el reclamo se deriva a revisión humana (RF-34).
# El predicado se evalúa fuera de la cadena y entra al contrato como booleano,
# porque el monto no puede viajar en la calldata (RNF-24).
LIMITE_DE_MONTO = Decimal("1000000.00")

VERSION_DE_CONFIGURACION_INICIAL = "v2-configuracion-decidida"


def ensure_declared_configuration(session: Session) -> Configuration:
    """Garantiza que la configuración que el código declara exista en la base.

    Crear la fila solo cuando la tabla está vacía alcanzaba en desarrollo y
    fallaba en el despliegue: la base vive en un volumen persistente, así que
    a partir del segundo despliegue la tabla nunca está vacía y los umbrales
    nuevos no llegaban nunca. El síntoma es silencioso, que es lo peor que
    puede tener: la API sigue respondiendo, con los valores viejos.

    La configuración es versionada por diseño, de modo que la forma correcta no
    es actualizar la fila existente sino insertar la versión nueva. Las
    transiciones ya asentadas siguen apuntando a la suya, que es justamente la
    propiedad que sostiene que un cambio de umbrales no vuelva irreproducible
    ninguna decisión anterior.

    Cambiar los umbrales exige cambiar también la versión: es la constante la
    que decide si hay que insertar algo.
    """
    declarada = session.scalars(
        select(Configuration).where(
            Configuration.version == VERSION_DE_CONFIGURACION_INICIAL
        )
    ).first()
    if declarada is None:
        declarada = Configuration(
            version=VERSION_DE_CONFIGURACION_INICIAL,
            derivation_threshold=UMBRAL_DE_DERIVACION,
            auto_settlement_threshold=UMBRAL_DE_LIQUIDACION_AUTOMATICA,
            amount_limit=LIMITE_DE_MONTO,
            max_file_size_bytes=LIMITE_DE_TAMANO_POR_ARCHIVO_BYTES,
            max_job_attempts=MAXIMO_DE_INTENTOS_POR_JOB,
        )
        session.add(declarada)
        session.flush()
    return declarada


def current_configuration(session: Session) -> Configuration:
    """Configuración con la que se clasifica hoy: la más reciente por vigencia.

    Los umbrales que siguen sin medir se quedan en nulo, que es lo que los
    distingue de una decisión tomada.
    """
    ensure_declared_configuration(session)
    configuracion = session.scalars(
        select(Configuration).order_by(
            Configuration.effective_from.desc(), Configuration.id.desc()
        )
    ).first()
    _check_threshold_order(configuracion)
    return configuracion


class InvalidConfiguration(Exception):
    """La configuración vigente viola una invariante del catálogo."""


def _check_threshold_order(configuracion: Configuration) -> None:
    """RF-51: el umbral de liquidación automática nunca por debajo del de
    derivación.

    Invertidos, el sistema liquidaría sin intervención casos que no alcanzaron
    la confianza mínima para resolverse solos, que es exactamente lo contrario
    de lo que los dos umbrales significan. Se comprueba al leer la
    configuración y no se documenta como advertencia: una configuración así no
    tiene una lectura razonable, así que no se la deja correr.
    """
    derivacion = configuracion.derivation_threshold
    liquidacion = configuracion.auto_settlement_threshold
    if derivacion is None or liquidacion is None:
        return
    if liquidacion < derivacion:
        raise InvalidConfiguration(
            f"el umbral de liquidación automática ({liquidacion}) está por "
            f"debajo del de derivación ({derivacion}), y RF-51 lo prohíbe"
        )


# ---------------------------------------------------------------------------
# Identificadores
# ---------------------------------------------------------------------------


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_justification(text: str) -> str:
    """Digesto de la justificación humana. A la cadena va esto y no el texto
    (RNF-24).

    Ninguna regla del proyecto fija todavía cómo se normaliza el texto antes de
    calcularlo, de modo que un auditor que quisiera recalcularlo no tiene la
    forma canónica. Acá se usa UTF-8 sin normalizar y queda declarado: es un
    hueco del diseño, no una decisión tomada.
    """
    return "0x" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def next_tracking_number(session: Session) -> str:
    """Número de seguimiento correlativo. No es la llave del recurso: RF-47
    separó identificar de abrir, y por eso hace falta además el token."""
    total = session.scalar(select(func.count()).select_from(Claim)) or 0
    return f"SN-{4100 + total + 1}"


def issue_access_token() -> str:
    """Token de capacidad de RF-47: aleatorio y no derivable del número."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Registro (CU-01)
# ---------------------------------------------------------------------------


def validate_files(
    files: list[UploadedFile], configuration: Configuration
) -> list[RejectedFile]:
    """Controles de RF-02, en el orden en que la pantalla los muestra.

    El límite de tamaño puede no estar medido (RNF-10). Cuando es nulo el
    control no corre: rechazar contra un umbral inventado sería peor que no
    rechazar, porque le daría a la marca [VERIFICAR] la apariencia de un valor.
    """
    rechazados: list[RejectedFile] = []
    for archivo in files:
        if archivo.mime_type not in FORMATOS_ADMITIDOS:
            rechazados.append(
                RejectedFile(
                    filename=archivo.filename,
                    reason="formato_no_admitido",
                    message=f"{archivo.mime_type} no admitido. Subí JPEG o PNG.",
                )
            )
            continue
        maximo = configuration.max_file_size_bytes
        if maximo is not None and archivo.size_bytes > maximo:
            rechazados.append(
                RejectedFile(
                    filename=archivo.filename,
                    reason="excede_tamano_maximo",
                    message=(
                        f"Pesa {archivo.size_bytes / (1024 * 1024):.1f}".replace(".", ",")
                        + f" MB y el máximo es {maximo // (1024 * 1024)} MB."
                    ),
                )
            )
    return rechazados


def normalize_damage_description(text: str) -> str:
    """Normaliza saltos a LF, recorta espacios y valida antes de producir efectos.

    `len` cuenta puntos de código en Python, no bytes ni grafemas. `str.strip`
    define el mismo conjunto de espacios Unicode que debe aplicar la SPA.
    Multipart puede convertir cada LF del navegador en CRLF; se mide y
    persiste el relato con un único punto de código por salto de línea.
    """
    if not isinstance(text, str):
        raise InvalidDamageDescription("la descripción del daño es obligatoria")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise InvalidDamageDescription("la descripción del daño es obligatoria")
    if len(normalized) > MAX_DAMAGE_DESCRIPTION_CODE_POINTS:
        raise InvalidDamageDescription(
            "la descripción del daño no puede superar los 2000 caracteres"
        )
    return normalized


def register_claim(
    session: Session,
    publisher: EvidencePublisher,
    *,
    damage_description: str,
    files: list[UploadedFile],
    declared_amount: Decimal,
    registered_by: str | None = None,
) -> RegisteredClaim:
    """CU-01. Valida relato y evidencia, publica fotos, crea el reclamo y lo
    encola.

    El relato se normaliza antes de leer la configuración, publicar fotografías
    o crear filas del reclamo. Si la publicación falla no queda un reclamo a
    medio crear, que es lo que la excepción 7a de CU-01 exige declarar («sin
    número, sin constancia y sin duplicado»).
    """
    normalized_description = normalize_damage_description(damage_description)

    configuracion = current_configuration(session)

    rechazados = validate_files(files, configuracion)
    admitidos = [
        archivo
        for archivo in files
        if archivo.filename not in {r.filename for r in rechazados}
    ]

    if len(files) > configuracion.max_files:
        rechazados.append(
            RejectedFile(
                filename=files[configuracion.max_files].filename,
                reason="cantidad_excedida",
                message=f"El máximo es de {configuracion.max_files} fotografías.",
            )
        )
    if rechazados or len(admitidos) < MINIMO_DE_FOTOGRAFIAS:
        raise EvidenceRejected(rechazados, accepted_count=len(admitidos))


    # No se conserva ni siquiera la lectura de configuración durante IPFS.
    configuration_id = configuracion.id
    max_attempts = configuracion.max_job_attempts
    session.commit()
    session.close()
    # La publicación va antes de tocar la base: si el servicio externo no
    # responde, no se crea nada.
    publicados = [
        (archivo, publisher.publish(archivo.content, archivo.filename))
        for archivo in admitidos
    ]

    token = issue_access_token()
    reclamo = Claim(
        tracking_number=next_tracking_number(session),
        access_token_hash=hash_token(token),
        declared_amount=declared_amount,
        damage_description=normalized_description,
        registered_by=registered_by,
        status=ClaimStatus.REGISTRADO,
    )
    session.add(reclamo)
    session.flush()

    evidencias = [
        Evidence(
            claim_id=reclamo.id,
            filename=archivo.filename,
            mime_type=archivo.mime_type,
            size_bytes=archivo.size_bytes,
            sha256=hashlib.sha256(archivo.content).hexdigest(),
            cid=cid,
            # `publish` aborta si el nodo propio y el servicio remoto no
            # computan el mismo CID, de modo que haber llegado hasta acá
            # significa que los dos custodios quedaron con el contenido y con
            # el mismo identificador. Registrarlo es lo que RF-07 pedía y lo
            # que le da a RNF-20 contra qué comprobar el segundo origen.
            pin_status=PinStatus.FIJADO,
            remote_pin_status=PinStatus.FIJADO,
            remote_cid_verified=True,
        )
        for archivo, cid in publicados
    ]
    session.add_all(evidencias)

    # La entrada al sistema no es una transición de la matriz: adr-001 la
    # enumera aparte y el contrato la emite con su propio evento, sin causa.
    session.add(
        Transition(
            claim_id=reclamo.id,
            configuration_id=configuration_id,
            from_status=None,
            to_status=ClaimStatus.REGISTRADO,
            cause=None,
        )
    )
    job = Job(
        claim_id=reclamo.id,
        configuration_id=configuration_id,
        max_attempts=max_attempts or 1,
    )
    session.add(job)
    session.flush()
    session.add(
        JobDispatch(
            job_id=job.id,
            next_publish_at=now(),
            publish_attempts=0,
        )
    )
    session.flush()
    return RegisteredClaim(claim=reclamo, access_token=token, evidence=evidencias, job=job)


# ---------------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------------


def get_claim(session: Session, number: str) -> Claim:
    reclamo = session.scalars(
        select(Claim).where(Claim.tracking_number == number)
    ).first()
    if reclamo is None:
        raise ClaimNotFound(number)
    return reclamo


def timeline(session: Session, claim: Claim) -> list[Transition]:
    return list(
        session.scalars(
            select(Transition)
            .where(Transition.claim_id == claim.id)
            .order_by(Transition.recorded_at, Transition.id)
        )
    )


# ---------------------------------------------------------------------------
# Transiciones que dispara una persona
# ---------------------------------------------------------------------------


def _record_transition(
    session: Session,
    claim: Claim,
    *,
    to_status: ClaimStatus,
    cause: TransitionCause,
    receipt: ChainReceipt,
    configuration_id: int,
) -> Transition:
    """Asienta en la base lo que la cadena decidió.

    Se usa el recibo con la configuración reservada antes del envío. Un cambio
    de vigencia durante el RPC no altera la procedencia de esta transición.
    """
    validate_transition(claim.status, to_status, cause)
    to_status = receipt.status
    cause = receipt.cause or cause
    transicion = Transition(
        claim_id=claim.id,
        configuration_id=configuration_id,
        from_status=claim.status,
        to_status=to_status,
        cause=cause,
        transaction_hash=receipt.transaction_hash,
        block_number=receipt.block_number,
    )
    session.add(transicion)
    claim.status = to_status
    session.flush()
    return transicion


def record_resolution(
    session: Session,
    number: str,
    *,
    favorable: bool,
    justification: str,
    reviewer_identity: str,
    chain: ChainClient,
) -> Transition:
    """RF-38 y RF-39. La justificación es obligatoria y a la cadena va su hash.

    Desde `InconsistenciaEvidencia` solo se admite la resolución desfavorable;
    no hace falta comprobarlo acá porque la matriz no ofrece la otra salida y
    `validate_transition` la rechaza.
    """
    if not justification.strip():
        raise ValueError("la justificación es obligatoria (RF-39)")

    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False, future=True)
    coordinator = chain if isinstance(chain, OracleCoordinator) else OracleCoordinator(factory, chain)
    reclamo = session.scalars(
        select(Claim).where(Claim.tracking_number == number).with_for_update()
    ).first()
    if reclamo is None:
        raise ClaimNotFound(number)
    causa = resolution_cause(favorable=favorable)
    destino = ClaimStatus.APROBADO if favorable else ClaimStatus.RECHAZADO
    validate_transition(reclamo.status, destino, causa)
    if reclamo.pending_chain_action is not None:
        raise TransitionNotAllowed(reclamo.status, destino, causa)
    digesto = hash_justification(justification)
    configuration_id = current_configuration(session).id
    review = Review(claim_id=reclamo.id, reviewer_identity=reviewer_identity,
                    decision=favorable, justification=justification, justification_hash=digesto)
    session.add(review)
    session.flush()
    review_id, claim_id = review.id, reclamo.id
    coordinator.prepare_operation_in_session(
        session,
        claim_id=claim_id,
        operation_type=OracleOperationType.RESOLVE.value,
        payload={
            "number": number, "favorable": favorable, "decision_hash": digesto,
            "reviewer_identity": reviewer_identity, "justification": justification,
            "review_id": review_id,
        },
        configuration_id=configuration_id,
    )
    session.commit()
    session.close()

    coordinator.resolve(
        claim_id=claim_id,
        number=number,
        favorable=favorable,
        decision_hash=digesto,
        reviewer_identity=reviewer_identity,
        justification=justification,
        configuration_id=configuration_id,
        review_id=review_id,
    )
    transicion = session.scalars(
        select(Transition).where(Transition.claim_id == claim_id).order_by(Transition.id.desc())
    ).first()
    return transicion

def settle_claim(
    session: Session,
    number: str,
    *,
    automatic: bool,
    chain: ChainClient,
) -> Transition:
    """RF-37. Un solo control y ningún campo de texto.

    CU-06 no tiene equivalente de RF-39, de modo que esta transición no registra
    autor: es el hueco que 00-criterios.md §4 anota sobre la responsabilidad de
    la liquidación. Lo que sí queda asentado es por cuál de los dos caminos de
    adr-007 se cerró, que es lo que el auditor externo puede leer.
    """
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False, future=True)
    coordinator = chain if isinstance(chain, OracleCoordinator) else OracleCoordinator(factory, chain)
    reclamo = session.scalars(
        select(Claim).where(Claim.tracking_number == number).with_for_update()
    ).first()
    if reclamo is None:
        raise ClaimNotFound(number)
    cause = settlement_cause(automatic=automatic)
    validate_transition(reclamo.status, ClaimStatus.APROBADO, cause)
    if reclamo.pending_chain_action is not None:
        raise TransitionNotAllowed(reclamo.status, ClaimStatus.APROBADO, cause)
    claim_id = reclamo.id
    configuration_id = current_configuration(session).id
    coordinator.prepare_operation_in_session(
        session,
        claim_id=claim_id,
        operation_type=OracleOperationType.SETTLE.value,
        payload={"number": number, "automatic": automatic},
        configuration_id=configuration_id,
    )
    session.commit()
    session.close()

    coordinator.settle(
        claim_id=claim_id,
        number=number,
        automatic=automatic,
        configuration_id=configuration_id,
    )
    transition = session.scalars(
        select(Transition).where(Transition.claim_id == claim_id).order_by(Transition.id.desc())
    ).first()
    return transition


# ---------------------------------------------------------------------------
# Cola (CU-10)
# ---------------------------------------------------------------------------


def queue_counters(session: Session) -> dict[str, int]:
    filas = session.execute(
        select(Job.status, func.count()).group_by(Job.status)
    ).all()
    por_estado = {estado: cantidad for estado, cantidad in filas}
    return {
        "pending": por_estado.get(JobStatus.PENDIENTE, 0),
        "running": por_estado.get(JobStatus.EN_CURSO, 0),
        "failed": por_estado.get(JobStatus.FALLIDO, 0),
    }


def jobs_by_status(session: Session, status: JobStatus) -> list[Job]:
    return list(
        session.scalars(
            select(Job).where(Job.status == status).order_by(Job.enqueued_at, Job.id)
        )
    )


__all__ = [
    "ClaimNotFound",
    "EvidenceRejected",
    "InvalidConfiguration",
    "InvalidDamageDescription",
    "RegisteredClaim",
    "RejectedFile",
    "TransitionNotAllowed",
    "UploadedFile",
    "current_configuration",
    "ensure_declared_configuration",
    "get_claim",
    "hash_justification",
    "jobs_by_status",
    "normalize_damage_description",
    "record_resolution",
    "register_claim",
    "settle_claim",
    "timeline",
    "validate_files",
]

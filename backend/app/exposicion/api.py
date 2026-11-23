"""Los nueve recursos de `contract/openapi.yaml`.

El recorte por perfil ocurre acá y no en el cliente: cada operación arma el
esquema que la columna de la matriz de visibilidad le concede a quien pide
(RF-46), y el perfil sale del token y nunca de lo que el cliente declare
(RF-48).
"""

from __future__ import annotations

import hashlib
import os
import re
from array import array
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.adaptadores.ipfs_client import IpfsError
from app.adaptadores.models import (
    Claim,
    Configuration as ConfigurationRow,
    Evidence,
    Inference,
    Job,
    OracleOperation as OracleOperationRow,
    OracleWallet,
    Proof,
    QueueControl,
    Review,
)
from app.adaptadores.models import Transition as TransitionRow
from app.adaptadores.models import now
from app.adaptadores.chain import build_chain_client
from app.adaptadores.inference import (
    HASH_DECLARADO_DEL_EXTRACTOR,
    URL_PUBLICADA_DEL_EXTRACTOR,
    URL_PUBLICADA_DEL_PREPROCESAMIENTO,
)
from app.adaptadores.oracle_coordinator import (
    OracleCoordinator,
    OracleOperationUncertainError,
    WalletBlockedError,
)
from app.dominio import claims as servicio
from app.dominio.state_machine import TransitionNotAllowed
from app.queue.reconciler import publish_job_dispatch
from app.dominio.vocabulary import (
    ClaimStatus,
    JobStatus,
    OracleOperationStatus,
    PolicyMode,
    TransitionCause,
    claimant_cause_text,
    claimant_label,
    terminal_states,
)
from app.exposicion import schemas as s
from app.exposicion.security import (
    Forbidden,
    Principal,
    Profile,
    Unauthorized,
    authorize_claim_access,
    require_profile,
    resolve_principal,
)

router = APIRouter()

DIMENSION_DEL_VECTOR = 576

CAMPOS_NO_EXPUESTOS = [
    "monto declarado",
    "imagen de la evidencia",
    "clase predicha",
    "confianza",
    "identidad del revisor",
    "texto de la justificación",
]


# ---------------------------------------------------------------------------
# Dependencias
# ---------------------------------------------------------------------------


def get_session(request: Request) -> Iterator[Session]:
    fabrica = request.app.state.session_factory
    sesion = fabrica()
    try:
        yield sesion
        sesion.commit()
    except Exception:
        sesion.rollback()
        raise
    finally:
        sesion.close()


def get_principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    encabezado = request.headers.get("Authorization", "")
    token = encabezado.removeprefix("Bearer ").strip() or None
    return resolve_principal(session, token)


# ---------------------------------------------------------------------------
# Traductores del modelo a la frontera
# ---------------------------------------------------------------------------


def decode_feature_vector(crudo: bytes) -> list[float]:
    """Las 576 características, como las guardó el extractor.

    Se persisten como flotantes de simple precisión en orden nativo, que es lo
    que ONNX Runtime devuelve; convertirlas a texto para guardarlas perdería
    dígitos y rompería la comparación byte a byte que RF-53 le pide al tercero
    que reejecuta.
    """
    return array("f", crudo).tolist()


def _ultima_causa(transiciones: list[TransitionRow]) -> TransitionCause | None:
    for transicion in reversed(transiciones):
        if transicion.cause is not None:
            return transicion.cause
    return None


def _claimant_status(claim: Claim, transiciones: list[TransitionRow]) -> s.ClaimantStatus:
    causa = _ultima_causa(transiciones)
    texto = claimant_cause_text(claim.status, causa)
    return s.ClaimantStatus(label=claimant_label(claim.status), cause=texto or "")


def _timeline(transiciones: list[TransitionRow]) -> list[s.TimelineEntry]:
    entradas = []
    for transicion in transiciones:
        texto = claimant_cause_text(transicion.to_status, transicion.cause)
        entradas.append(
            s.TimelineEntry(
                occurred_at=transicion.recorded_at,
                label=claimant_label(transicion.to_status),
                cause=texto or "",
            )
        )
    return entradas


def _transition_out(transicion: TransitionRow) -> s.TransitionOut:
    return s.TransitionOut(
        from_=transicion.from_status,
        to=transicion.to_status,
        cause=transicion.cause,
        occurred_at=transicion.recorded_at,
        block_number=transicion.block_number,
        transaction_hash=transicion.transaction_hash,
        proof_hash=transicion.proof_hash,
    )


def _receipt(claim: Claim, evidencias: list[Evidence], transiciones: list[TransitionRow]) -> s.Receipt:
    return s.Receipt(
        evidence_id=evidencias[0].cid if evidencias else "",
        transaction_hashes=[
            t.transaction_hash for t in transiciones if t.transaction_hash
        ],
        # Nulo mientras el reclamo esté en Registrado: la prueba no se generó.
        proof_hash=next((t.proof_hash for t in reversed(transiciones) if t.proof_hash), None),
        public_trace_url=f"/publico/reclamos/{claim.tracking_number}/traza",
    )


def _evidence_detail(evidencias: list[Evidence], *, con_imagen: bool) -> s.EvidenceDetail:
    return s.EvidenceDetail(
        evidence_id=evidencias[0].cid if evidencias else "",
        files=[
            s.EvidenceFile(
                filename=evidencia.filename,
                size_bytes=evidencia.size_bytes,
                sha256=evidencia.sha256,
                url=(
                    f"/reclamos/{evidencia.claim.tracking_number}/evidencia/{evidencia.id}"
                    if con_imagen else None
                ),
            )
            for evidencia in evidencias
        ],
    )


def _configuration_out(fila: ConfigurationRow) -> s.Configuration:
    etiquetas = {
        PolicyMode.AUTOMATICO_CON_ESCALAMIENTO: (
            "Resolución automática, con escalamiento por excepción."
        ),
        PolicyMode.CONFIRMACION_HUMANA_OBLIGATORIA: (
            "Esta aseguradora revisa todos los reclamos antes de resolverlos."
        ),
    }
    maximo_mb = (
        fila.max_file_size_bytes / (1024 * 1024)
        if fila.max_file_size_bytes is not None
        else None
    )
    return s.Configuration(
        version=fila.version,
        policy_mode=fila.policy_mode,
        policy_mode_label=etiquetas[fila.policy_mode],
        derivation_threshold=s.UnmeasuredNumber.de(
            fila.derivation_threshold, requirement="RNF-15"
        ),
        auto_settlement_threshold=s.UnmeasuredNumber.de(
            fila.auto_settlement_threshold, requirement="RNF-15"
        ),
        amount_limit=s.UnmeasuredNumber.de(
            float(fila.amount_limit) if fila.amount_limit is not None else None,
            requirement="RF-34",
        ),
        max_file_size_mb=s.UnmeasuredNumber.de(
            maximo_mb, requirement="RNF-10", unit="MB"
        ),
        max_files=s.UnmeasuredNumber.de(float(fila.max_files)),
        max_retries=s.UnmeasuredNumber.de(
            float(fila.max_job_attempts) if fila.max_job_attempts is not None else None,
            requirement="RF-13",
        ),
        accepted_formats=["image/jpeg", "image/png"],
    )


def _confirmed_review(claim: Claim) -> Review | None:
    """La decisión pendiente es privada hasta confirmar su transición."""
    if claim.status not in terminal_states():
        return None
    return max(
        (review for review in claim.reviews if review.transition_id is not None),
        key=lambda review: (review.transition_id, review.id),
        default=None,
    )


def _confirmed_inference(
    session: Session, claim: Claim, transitions: list[TransitionRow]
) -> tuple[Inference, TransitionRow] | None:
    """Vincula la clasificación confirmada con su prueba y checkpoint vigente.

    Los registros anteriores a los checkpoints se reconstruyen por el hash
    confirmado; nunca se elige una inferencia por su posición en el historial.
    """
    classification = next(
        (transition for transition in reversed(transitions)
         if transition.from_status is ClaimStatus.REGISTRADO and transition.proof_hash),
        None,
    )
    if classification is None:
        return None
    query = select(Inference).join(Proof).where(
        Inference.claim_id == claim.id,
        Proof.proof_hash == classification.proof_hash,
    )
    job = session.scalar(
        select(Job).where(Job.claim_id == claim.id).order_by(Job.id.desc()).limit(1)
    )
    if job is not None and job.checkpoint_context is not None:
        query = query.where(Inference.id == job.inference_id, Proof.id == job.proof_id)
    inference = session.scalars(query.order_by(Inference.id.desc()).limit(1)).first()
    return (inference, classification) if inference is not None else None


def _internal_resolution(session: Session, claim: Claim) -> s.InternalResolution | None:
    """Nulo mientras el reclamo no esté resuelto, y nulo también cuando el
    cierre fue una liquidación automática: esa transición no tiene autor a quién
    registrar, y el vocabulario de causa es lo que permite reconocerlo."""
    revision = _confirmed_review(claim)
    if revision is None:
        return None
    return s.InternalResolution(
        reviewer=revision.reviewer_identity,
        role="analista",
        recorded_at=revision.timestamp,
        justification=revision.justification,
        justification_hash=revision.justification_hash,
    )


def _cliente_de_cadena(session: Session):
    """El cliente que corresponda, construido con la configuración vigente.

    Las transiciones que dispara una persona —la resolución del revisor y la
    liquidación del operador— pasan por el contrato igual que las del worker.
    Dejarlas fuera rompería lo que la traza pública afirma de sí misma, que es
    estar reconstruida desde los eventos del contrato.
    """
    configuracion = servicio.current_configuration(session)
    threshold, policy = configuracion.derivation_threshold, configuracion.policy_mode
    session.commit()
    session.close()
    return build_chain_client(derivation_threshold=threshold, policy_mode=policy)


# ---------------------------------------------------------------------------
# CU-01 — registrar
# ---------------------------------------------------------------------------


@router.post(
    "/reclamos",
    status_code=201,
    response_model=s.RegistrationReceipt,
    tags=["reclamante", "operador"],
)
async def register_claim(
    request: Request,
    photos: list[UploadFile] = File(..., alias="photos"),
    declared_amount: str = Form(..., alias="declaredAmount"),
    damage_description: str = Form(..., alias="damageDescription"),
    session: Session = Depends(get_session),
) -> s.RegistrationReceipt:
    principal = None
    if "Authorization" in request.headers:
        principal = get_principal(request, session)
        require_profile(principal, Profile.OPERADOR)

    try:
        monto = Decimal(declared_amount)
    except InvalidOperation:
        raise HTTPException(422, detail="monto declarado inválido") from None
    if monto <= 0:
        raise HTTPException(422, detail="el monto declarado debe ser mayor que cero")

    archivos = [
        servicio.UploadedFile(
            filename=foto.filename or "sin-nombre",
            mime_type=foto.content_type or "application/octet-stream",
            content=await foto.read(),
        )
        for foto in photos
    ]

    registrado = servicio.register_claim(
        session,
        request.app.state.publisher,
        damage_description=damage_description,
        files=archivos,
        declared_amount=monto,
        registered_by=principal.identity if principal is not None else None,
    )
    reclamo = registrado.claim
    transiciones = servicio.timeline(session, reclamo)
    job_id = registrado.job.id if registrado.job is not None else None

    # Confirmar reclamo, job y job_dispatch atómicamente antes de intentar publicar
    # en el broker. Si Redis falla después del commit, el reclamo permanece asentado
    # y el reconciliador del worker republica el despacho pendiente (ADR-018).
    session.commit()

    if job_id is not None:
        publish_job_dispatch(request.app.state.session_factory, job_id)

    return s.RegistrationReceipt(
        number=reclamo.tracking_number,
        registered_at=reclamo.created_at,
        status=_claimant_status(reclamo, transiciones),
        submitted=s.SubmittedSummary(
            damage_description=registrado.claim.damage_description,
            photo_count=len(registrado.evidence),
            declared_amount=float(reclamo.declared_amount),
        ),
        receipt=_receipt(reclamo, registrado.evidence, transiciones),
        access_token=registrado.access_token,
        on_chain_scope=(
            "Quedó asentado el identificador de la evidencia. Las imágenes no "
            "se publican en la cadena."
        ),
    )


# ---------------------------------------------------------------------------
# CU-02 — consultar, por perfil
# ---------------------------------------------------------------------------


@router.get("/reclamos/{numero}", tags=["reclamante", "revisor", "operador"])
def get_claim(
    numero: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> s.ClaimantClaim | s.ReviewerClaim | s.OperatorClaim:
    reclamo = servicio.get_claim(session, numero)
    authorize_claim_access(principal, reclamo)

    transiciones = servicio.timeline(session, reclamo)
    evidencias = list(reclamo.evidence)

    if principal.profile is Profile.RECLAMANTE:
        sin_clasificar = reclamo.status is ClaimStatus.REGISTRADO
        resolucion = None
        revision = _confirmed_review(reclamo)
        if revision is not None:
            resolucion = s.ClaimantResolution(
                role="analista",
                justification=revision.justification,
                justification_hash=revision.justification_hash,
            )
        return s.ClaimantClaim(
            number=reclamo.tracking_number,
            status=_claimant_status(reclamo, transiciones),
            timeline=_timeline(transiciones),
            claim=s.ClaimantClaimDetail(
                photo_count=len(evidencias),
                damage_description=reclamo.damage_description,
                declared_amount=float(reclamo.declared_amount),
            ),
            queue=(
                s.QueuePosition(
                    position=None,
                    estimated_wait=s.UnmeasuredString.de(None, requirement="RNF-06"),
                )
                if sin_clasificar
                else None
            ),
            resolution=resolucion,
            appeal_notice=(
                "Si no estás de acuerdo con la resolución, podés presentar un "
                "reclamo nuevo enlazado a este."
                if reclamo.status in terminal_states()
                else None
            ),
            explanation_available=_confirmed_inference(session, reclamo, transiciones) is not None,
            receipt=_receipt(reclamo, evidencias, transiciones),
        )

    require_profile(principal, Profile.REVISOR, Profile.OPERADOR)
    estado = _claimant_status(reclamo, transiciones)
    detalle = _evidence_detail(evidencias, con_imagen=True)

    if principal.profile is Profile.REVISOR:
        salidas = (
            ["desfavorable"]
            if reclamo.status is ClaimStatus.INCONSISTENCIA_EVIDENCIA
            else ["favorable", "desfavorable"]
            if reclamo.status is ClaimStatus.REQUIERE_REVISION
            else []
        )
        return s.ReviewerClaim(
            number=reclamo.tracking_number,
            status_id=reclamo.status,
            status=estado,
            evidence=detalle,
            inference=None,
            cause=(
                s.DerivationCause(
                    id=_ultima_causa(transiciones),
                    text=claimant_cause_text(reclamo.status, _ultima_causa(transiciones)),
                    amount_was_not_the_cause=(
                        _ultima_causa(transiciones)
                        is not TransitionCause.MONTO_SOBRE_UMBRAL
                    ),
                )
                if _ultima_causa(transiciones) is not None
                else None
            ),
            trace=[_transition_out(t) for t in transiciones],
            declared_amount=float(reclamo.declared_amount),
            damage_description=reclamo.damage_description,
            registered_by=reclamo.registered_by,
            assignment=s.Assignment(
                unopened=reclamo.assigned_at is None,
                assigned_at=reclamo.assigned_at,
            ),
            available_resolutions=salidas or None,
            resolution=_internal_resolution(session, reclamo),
        )

    return s.OperatorClaim(
        number=reclamo.tracking_number,
        status_id=reclamo.status,
        status=estado,
        evidence=detalle,
        inference=None,
        declared_amount=float(reclamo.declared_amount),
        damage_description=reclamo.damage_description,
        registered_by=reclamo.registered_by,
        trace=[_transition_out(t) for t in transiciones],
        settleable=reclamo.status is ClaimStatus.APROBABLE,
        resolution=_internal_resolution(session, reclamo),
    )


@router.get(
    "/reclamos/{numero}/evidencia/{evidence_file_id}",
    response_class=Response,
    responses={
        200: {"content": {"image/png": {}, "image/jpeg": {}}},
        401: {"description": "Acceso no acreditado."},
        403: {"description": "El perfil no admite esta operación."},
        404: {"description": "Fotografía no encontrada en el reclamo."},
        502: {"description": "No se pudo recuperar la fotografía."},
        504: {"description": "Se agotó el tiempo para recuperar la fotografía."},
    },
    tags=["revisor", "operador"],
)
def get_claim_photo(
    numero: str,
    evidence_file_id: int,
    request: Request,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> Response:
    """Entrega el original verificado, sin mantener la conexión SQL durante I/O."""
    claim = servicio.get_claim(session, numero)
    authorize_claim_access(principal, claim)
    require_profile(principal, Profile.REVISOR, Profile.OPERADOR)
    evidence = session.scalar(select(Evidence).where(
        Evidence.claim_id == claim.id, Evidence.id == evidence_file_id,
    ))
    if evidence is None:
        raise HTTPException(404, detail="Fotografía no encontrada en el reclamo.")
    cid, size, digest, mime = (
        evidence.cid, evidence.size_bytes, evidence.sha256, evidence.mime_type,
    )
    maximum = servicio.current_configuration(session).max_file_size_bytes
    failure = "No se pudo recuperar la fotografía."
    if (
        not isinstance(size, int) or size <= 0
        or not isinstance(maximum, int) or maximum <= 0 or size > maximum
        or mime not in servicio.FORMATOS_ADMITIDOS
        or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(cid, str) or not cid
    ):
        raise HTTPException(502, detail=failure)
    # Solo escalares a partir de acá: el finalizador de get_session conserva su ciclo.
    session.commit()
    session.close()
    try:
        content = request.app.state.publisher.retrieve(cid, max_bytes=size)
    except (httpx.TimeoutException, TimeoutError):
        raise HTTPException(
            504, detail="Se agotó el tiempo para recuperar la fotografía."
        ) from None
    except (IpfsError, httpx.HTTPError, OSError):
        raise HTTPException(502, detail=failure) from None
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise HTTPException(502, detail=failure)
    return Response(content, media_type=mime, headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })


# ---------------------------------------------------------------------------
# MK-04 — explicación (RF-42, RF-43)
# ---------------------------------------------------------------------------


@router.get(
    "/reclamos/{numero}/explicacion",
    response_model=s.ClaimExplanation,
    tags=["reclamante", "revisor"],
)
def get_claim_explanation(
    numero: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> s.ClaimExplanation:
    reclamo = servicio.get_claim(session, numero)
    authorize_claim_access(principal, reclamo)
    require_profile(principal, Profile.RECLAMANTE, Profile.REVISOR)

    transiciones = servicio.timeline(session, reclamo)
    confirmed = _confirmed_inference(session, reclamo, transiciones)
    if confirmed is None:
        raise HTTPException(409, detail="el reclamo todavía no fue clasificado")
    inferencia, clasificacion = confirmed

    configuracion = clasificacion.configuration
    evidencias = list(reclamo.evidence)
    fallo = clasificacion.to_status is ClaimStatus.VERIFICACION_FALLIDA

    return s.ClaimExplanation(
        evidence=s.ExplanationEvidence(
            file_count=len(evidencias),
            submitted_at=reclamo.created_at,
            published_feature_vector=s.PublishedFeatureVector(
                published=True,
                dimension=DIMENSION_DEL_VECTOR,
                notice=(
                    "Las fotos quedan accesibles por su identificador en IPFS. "
                    "El análisis utiliza un resumen numérico calculado a partir de ellas."
                ),
            ),
        ),
        model=s.ExplanationModel(
            predicted_class=inferencia.predicted_class,
            confidence=inferencia.confidence,
            # En la variante de verificación fallida la clase se muestra
            # rotulada como sin acreditar (MK-04b).
            accredited=not fallo,
        ),
        proof=s.ExplanationProof(
            proven_segment="de las características a los logits, acreditado por prueba",
            replicated_segment="del píxel a las características, acreditado por replicación",
            disclaimer=(
                "La prueba acredita que el modelo declarado se ejecutó de forma "
                "correcta; no justifica por qué llegó a ese resultado."
            ),
            verification_failed=fallo,
            transaction_hash=clasificacion.transaction_hash,
        ),
        rule=s.ExplanationRule(
            confidence_against_threshold=s.ThresholdComparison(
                observed=inferencia.confidence,
                threshold=s.UnmeasuredNumber.de(
                    configuracion.derivation_threshold, requirement="RNF-15"
                ),
                satisfied=(
                    inferencia.confidence >= configuracion.derivation_threshold
                    if configuracion.derivation_threshold is not None
                    else None
                ),
            ),
            amount_against_limit=s.ThresholdComparison(
                observed=float(reclamo.declared_amount),
                threshold=s.UnmeasuredNumber.de(
                    float(configuracion.amount_limit)
                    if configuracion.amount_limit is not None
                    else None,
                    requirement="RF-34",
                ),
                satisfied=(
                    float(reclamo.declared_amount) <= float(configuracion.amount_limit)
                    if configuracion.amount_limit is not None
                    else None
                ),
            ),
            auto_settlement_applied=(
                _ultima_causa(transiciones) is TransitionCause.LIQUIDACION_AUTOMATICA
            ),
        ),
        outcome=_claimant_status(reclamo, transiciones),
    )


# ---------------------------------------------------------------------------
# MK-05 — bandeja del revisor (RF-36)
# ---------------------------------------------------------------------------


@router.get("/revision/bandeja", response_model=s.ReviewInbox, tags=["revisor"])
def get_review_inbox(
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> s.ReviewInbox:
    require_profile(principal, Profile.REVISOR)
    configuracion = servicio.current_configuration(session)

    derivados = [
        reclamo
        for reclamo in session.query(Claim)
        .filter(
            Claim.status.in_(
                [ClaimStatus.REQUIERE_REVISION, ClaimStatus.INCONSISTENCIA_EVIDENCIA]
            )
        )
        # Orden de llegada, declarado sobre la lista: RF-36 no fija criterio de
        # reparto ni orden, y MK-05 lo asume como elaboración propia.
        .order_by(Claim.created_at, Claim.id)
        .all()
    ]

    filas = []
    for reclamo in derivados:
        transiciones = servicio.timeline(session, reclamo)
        causa = _ultima_causa(transiciones)
        confirmed = _confirmed_inference(session, reclamo, transiciones)
        inferencia = confirmed[0] if confirmed is not None else None
        filas.append(
            s.InboxRow(
                number=reclamo.tracking_number,
                status_id=reclamo.status,
                cause=s.DerivationCause(
                    id=causa,
                    text=claimant_cause_text(reclamo.status, causa),
                    amount_was_not_the_cause=causa
                    is not TransitionCause.MONTO_SOBRE_UMBRAL,
                )
                if causa is not None
                else s.DerivationCause(id=TransitionCause.ESCALAMIENTO_OBLIGATORIO),
                predicted_class=inferencia.predicted_class if inferencia else None,
                confidence=inferencia.confidence if inferencia else None,
                # Falso cuando el reclamo llegó por escalamiento y la
                # comprobación criptográfica no se completó: sin ese rótulo, la
                # clase tendría en la bandeja el mismo peso que una verificada.
                accredited=(
                    confirmed is not None
                    and confirmed[1].to_status is not ClaimStatus.VERIFICACION_FALLIDA
                ),
                declared_amount=float(reclamo.declared_amount),
                unopened=reclamo.assigned_at is None,
                arrived_at=reclamo.created_at,
            )
        )

    modo_humano = (
        configuracion.policy_mode is PolicyMode.CONFIRMACION_HUMANA_OBLIGATORIA
    )
    return s.ReviewInbox(
        assigned_total=len(filas),
        unopened=sum(1 for fila in filas if fila.unopened),
        ordering="llegada",
        human_confirmation_banner=(
            "El modo de confirmación humana obligatoria está activo: la "
            "clasificación no emite Aprobable y la liquidación automática queda "
            "sin efecto."
            if modo_humano
            else None
        ),
        rows=filas,
        configuration=_configuration_out(configuracion),
    )


# ---------------------------------------------------------------------------
# CU-05 — resolución humana (RF-38, RF-39)
# ---------------------------------------------------------------------------


@router.post(
    "/reclamos/{numero}/resolucion",
    response_model=s.TransitionReceipt,
    tags=["revisor"],
)
def record_resolution(
    numero: str,
    cuerpo: s.ResolutionRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> s.TransitionReceipt:
    require_profile(principal, Profile.REVISOR)
    transicion = servicio.record_resolution(
        session,
        numero,
        favorable=cuerpo.favorable,
        justification=cuerpo.justification,
        reviewer_identity=principal.identity,
        chain=_cliente_de_cadena(session),
    )
    reclamo = servicio.get_claim(session, numero)
    return s.TransitionReceipt(
        number=reclamo.tracking_number,
        status_id=reclamo.status,
        transition=_transition_out(transicion),
    )


# ---------------------------------------------------------------------------
# CU-06 — liquidación (RF-37)
# ---------------------------------------------------------------------------


@router.post(
    "/reclamos/{numero}/liquidacion",
    response_model=s.TransitionReceipt,
    tags=["operador"],
)
def settle_claim(
    numero: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> s.TransitionReceipt:
    require_profile(principal, Profile.OPERADOR)
    # El operador liquida a mano: el camino automático lo dispara el worker
    # cuando la confianza supera el umbral de adr-007.
    transicion = servicio.settle_claim(
        session, numero, automatic=False, chain=_cliente_de_cadena(session)
    )
    reclamo = servicio.get_claim(session, numero)
    return s.TransitionReceipt(
        number=reclamo.tracking_number,
        status_id=reclamo.status,
        transition=_transition_out(transicion),
    )


# ---------------------------------------------------------------------------
# CU-10 — monitor de la cola (RF-14)
# ---------------------------------------------------------------------------


def _paso_a_aprobable(session: Session, reclamo: Claim):
    """Cuándo entró a `Aprobable`, leído de su transición y no de `updated_at`.

    `updated_at` se mueve con cualquier escritura sobre la fila; la transición
    es el hecho que la pantalla nombra. Si el reclamo llegó al estado sin dejar
    transición, viaja nulo antes que con una fecha que significaría otra cosa.
    """
    return session.scalars(
        select(TransitionRow.recorded_at)
        .where(
            TransitionRow.claim_id == reclamo.id,
            TransitionRow.to_status == ClaimStatus.APROBABLE,
        )
        .order_by(TransitionRow.recorded_at.desc(), TransitionRow.id.desc())
    ).first()


@router.get("/cola", response_model=s.QueueState, tags=["operador"])
def get_queue_state(
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> s.QueueState:
    require_profile(principal, Profile.OPERADOR)
    configuracion = servicio.current_configuration(session)
    contadores = servicio.queue_counters(session)

    en_curso = servicio.jobs_by_status(session, JobStatus.EN_CURSO)
    pendientes = servicio.jobs_by_status(session, JobStatus.PENDIENTE)
    fallidos = servicio.jobs_by_status(session, JobStatus.FALLIDO)

    aprobables = (
        session.query(Claim)
        .filter(Claim.status == ClaimStatus.APROBABLE)
        .order_by(Claim.updated_at, Claim.id)
        .all()
    )

    unresolved_ops = (
        session.query(OracleOperationRow)
        .filter(
            OracleOperationRow.status.notin_(
                [
                    OracleOperationStatus.CONFIRMADA.value,
                    OracleOperationStatus.REVERTIDA.value,
                    OracleOperationStatus.FALLIDA_PRE_ENVIO.value,
                ]
            )
        )
        .order_by(OracleOperationRow.created_at)
        .all()
    )
    active_wallets = (
        session.query(OracleWallet)
        .filter(OracleWallet.active_operation_id.isnot(None))
        .all()
    )
    oracle_blocked = bool(active_wallets) or any(
        op.status == OracleOperationStatus.INCIERTA.value for op in unresolved_ops
    )
    qc = session.get(QueueControl, 1)
    effective_concurrency = (
        qc.concurrency if qc is not None else configuracion.queue_concurrency
    )
    qc_mismatch = qc is not None and (
        qc.configuration_id != configuracion.id
        or qc.concurrency != configuracion.queue_concurrency
    )
    processor_stopped = bool((qc is not None and qc.paused) or qc_mismatch)
    return s.QueueState(
        counters=s.QueueCounters(
            pending=contadores["pending"],
            running=contadores["running"],
            failed=contadores["failed"],
            # La espera del último de la lista es el número que ningún
            # requerimiento acota: RNF-06 mide con la cola vacía.
            last_position_wait=s.UnmeasuredString.de(None, requirement="RNF-06"),
        ),
        concurrency=effective_concurrency,
        running=[
            s.RunningJob(
                number=job.claim.tracking_number,
                stage=(
                    "recuperacion" if job.current_stage == "registro_cadena"
                    else "envio" if job.current_stage == "liquidacion"
                    else job.current_stage or "recuperacion"
                ),
                elapsed_seconds=(
                    (now() - job.started_at).total_seconds()
                    if job.started_at is not None
                    else 0.0
                ),
                # Nueve segundos y seis décimas por prueba, medido el 14/08 sobre
                # la cabeza ajustada. Es dato propio y por eso viaja como número
                # y no como valor sin medir.
                typical_seconds=9.6,
            )
            for job in en_curso
        ],
        pending=[
            s.PendingJob(
                number=job.claim.tracking_number,
                position=posicion,
                enqueued_at=job.enqueued_at,
            )
            for posicion, job in enumerate(pendientes, start=1)
        ],
        failed=[
            s.FailedJob(
                number=job.claim.tracking_number,
                reason=job.last_failure_cause or "verificacion_local_fallida",
                attempt=job.attempts,
                max_attempts=s.UnmeasuredNumber.de(
                    float(configuracion.max_job_attempts)
                    if configuracion.max_job_attempts is not None
                    else None,
                    requirement="RF-13",
                ),
                claim_status_id=job.claim.status,
                definitive=job.attempts >= job.max_attempts,
            )
            for job in fallidos
        ],
        # Los que esperan la liquidación de RF-37. No son trabajos de la cola y
        # por eso no entran en los contadores: la cola termina cuando hay prueba
        # enviada, y esto es lo que sigue después. Van acá porque es la pantalla
        # que el operador mira, y son lectura como todo lo demás de esta ruta.
        settleable=[
            s.SettleableClaim(
                number=reclamo.tracking_number,
                declared_amount=float(reclamo.declared_amount),
                approvable_since=_paso_a_aprobable(session, reclamo),
            )
            for reclamo in aprobables
        ],
        # Reanudar es operación de infraestructura y queda fuera de la pantalla.
        processor_stopped=processor_stopped,
        oracle_blocked=oracle_blocked,
        oracle_operations=[
            s.OracleOperation(
                operation_id=str(op.id),
                number=op.claim.tracking_number if op.claim else str(op.claim_id),
                operation=op.operation,
                status=op.status,
                transaction_hash=op.transaction_hash,
                block_number=op.block_number,
                last_error_code=op.last_error_code,
            )
            for op in unresolved_ops
        ],
    )


@router.post(
    "/cola/reconciliacion",
    response_model=s.OracleOperation,
    tags=["operador"],
)
def reconcile_oracle_operation(
    cuerpo: s.ReconciliationRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> s.OracleOperation:
    """Reconcilia una operación incierta o pendiente del oráculo (ADR-018)."""
    require_profile(principal, Profile.OPERADOR)
    op = session.get(OracleOperationRow, cuerpo.operation_id)
    if op is None:
        raise HTTPException(status_code=404, detail="Operación no encontrada.")
    claim_number = op.claim.tracking_number if op.claim else str(op.claim_id)

    coordinator = OracleCoordinator(
        sessionmaker(bind=session.get_bind(), expire_on_commit=False, future=True),
        _cliente_de_cadena(session),
    )
    reconciled = coordinator.reconcile_operation(cuerpo.operation_id)
    return s.OracleOperation(
        operation_id=str(reconciled.id),
        number=claim_number,
        operation=reconciled.operation,
        status=reconciled.status,
        transaction_hash=reconciled.transaction_hash,
        block_number=reconciled.block_number,
        last_error_code=reconciled.last_error_code,
    )

# ---------------------------------------------------------------------------
# Configuración vigente (RF-32, RF-50, RF-51)
# ---------------------------------------------------------------------------


@router.get(
    "/configuracion/publica",
    response_model=s.PublicConfiguration,
    tags=["publico"],
)
def get_public_configuration(session: Session = Depends(get_session)) -> s.PublicConfiguration:
    """Proyección cerrada para preparar el formulario sin token de reclamo."""
    config = _configuration_out(servicio.current_configuration(session))
    return s.PublicConfiguration(
        max_files=config.max_files,
        max_file_size_mb=config.max_file_size_mb,
        amount_limit=config.amount_limit,
        accepted_formats=config.accepted_formats,
    )


@router.get(
    "/configuracion",
    response_model=s.Configuration,
    tags=["revisor", "operador", "reclamante"],
)
def get_configuration(
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
) -> s.Configuration:
    return _configuration_out(servicio.current_configuration(session))


# ---------------------------------------------------------------------------
# CU-08 — traza pública (RF-44, RF-45, RF-53)
# ---------------------------------------------------------------------------


@router.get(
    "/publico/reclamos/{numero}/traza",
    response_model=s.PublicTrace,
    tags=["publico"],
)
def get_public_trace(
    numero: str, session: Session = Depends(get_session)
) -> s.PublicTrace:
    """Lista de campos cerrada y sin acreditación de titularidad.

    El prefijo de la ruta la separa en la tabla de ruteo para que sea auditable
    de un vistazo: todo lo que cuelga de `/publico` no pide token.
    """
    reclamo = servicio.get_claim(session, numero)
    transiciones = servicio.timeline(session, reclamo)
    evidencias = list(reclamo.evidence)
    confirmed = _confirmed_inference(session, reclamo, transiciones)
    if confirmed is None:
        raise HTTPException(409, detail="el reclamo todavía no tiene prueba que exhibir")
    inferencia, _ = confirmed
    prueba = inferencia.proof
    modelo = inferencia.model

    return s.PublicTrace(
        number=reclamo.tracking_number,
        status_id=reclamo.status,
        status_is_terminal=reclamo.status in terminal_states(),
        provenance=(
            "Reconstruida desde los eventos del contrato. La base de la "
            "aseguradora es réplica y no fuente."
        ),
        transitions=[_transition_out(t) for t in transiciones],
        evidence=s.PublicEvidence(
            evidence_id=evidencias[0].cid if evidencias else "",
            retrievable=bool(evidencias),
            # RF-10, mitad de CU-08: qué encontró el sistema al recuperar la
            # evidencia por su identificador y comparar el digesto. Nulo
            # mientras nadie la haya recuperado, que no es lo mismo que
            # haberla recuperado y encontrado una diferencia.
            digest_matches=(
                all(e.digest_verified for e in evidencias)
                if evidencias and all(e.digest_verified is not None for e in evidencias)
                else None
            ),
        ),
        feature_vector=decode_feature_vector(inferencia.feature_vector),
        artifacts=s.PublicArtifacts(
            proof=s.ProofArtifacts(
                proof_hash=prueba.proof_hash if prueba else None,
                circuit_parameters=prueba.circuit_parameters if prueba else None,
            ),
            # Cuando el reclamo corrió con el extractor ONNX real, las dos URL
            # apuntan a los CID publicados en IPFS (RF-53, adr-011). Si corrió
            # con el motor simulado (`stub:`), siguen nulas porque no existe
            # artefacto publicado que descargar.
            replication=s.ReplicationArtifacts(
                extractor_onnx_url=(
                    os.environ.get("URL_EXTRACTOR_ONNX", URL_PUBLICADA_DEL_EXTRACTOR)
                    if modelo and modelo.extractor_sha256 == HASH_DECLARADO_DEL_EXTRACTOR
                    else None
                ),
                extractor_onnx_hash=modelo.extractor_sha256 if modelo else None,
                preprocessing_spec_url=(
                    os.environ.get(
                        "URL_PREPROCESAMIENTO_EXTRACTOR",
                        URL_PUBLICADA_DEL_PREPROCESAMIENTO,
                    )
                    if modelo and modelo.extractor_sha256 == HASH_DECLARADO_DEL_EXTRACTOR
                    else None
                ),
                runtime_version=modelo.version if modelo else None,
            ),
        ),
        not_exposed=CAMPOS_NO_EXPUESTOS,
        image_disclosure_notice=(
            "Que esta pantalla no muestre la imagen es decisión de presentación: "
            "el identificador la recupera igual."
        ),
    )

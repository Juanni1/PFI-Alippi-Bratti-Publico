"""Punto de entrada del servicio de aplicación (API + worker en un proceso)."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from threading import Thread

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.adaptadores.database import (
    create_database_engine,
    create_session_factory,
    verify_schema,
)
from app.adaptadores.inference import build_inference_engine
from app.adaptadores.ipfs_client import IpfsError
from app.adaptadores.local_storage import build_evidence_publisher
from app.adaptadores.proving import build_proof_generator
from app.adaptadores.oracle_coordinator import (
    OracleOperationUncertainError,
    OracleOperationRejectedError,
    WalletBlockedError,
)
from app.dominio.claims import (
    ClaimNotFound,
    EvidenceRejected,
    InvalidDamageDescription,
)
from app.dominio.state_machine import TransitionNotAllowed
from app.dominio.vocabulary import load_dictionary
from app.exposicion import schemas as s
from app.exposicion.api import router
from app.exposicion.security import Forbidden, Unauthorized


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Abre la base y deja listos los adaptadores que las rutas consumen.

    La base debe estar migrada previamente. `verify_schema` comprueba la revisión
    vigente de Alembic y rechaza el arranque si la base no está migrada.
    """

    # El diccionario de estados se lee en tiempo de ejecución. Si falta, la
    # aplicación arrancaba igual y reventaba recién al primer reclamo, con el
    # healthcheck en verde y el servicio marcado como sano. Cargarlo acá
    # convierte ese fallo en un arranque que no prospera, que es lo que la
    # plataforma sabe interpretar.
    load_dictionary()

    motor = create_database_engine()
    try:
        verify_schema(motor)
        app.state.session_factory = create_session_factory(motor)
        app.state.publisher = build_evidence_publisher()
        app.state.inference_engine = build_inference_engine()
        app.state.proof_generator = build_proof_generator()
        from sqlalchemy import select
        from app.adaptadores.models import OracleOperation
        from app.dominio.vocabulary import OracleOperationStatus

        with app.state.session_factory() as session:
            has_pending_oracle_operation = session.scalar(
                select(OracleOperation.id).where(
                    OracleOperation.status.in_(
                        [status.value for status in OracleOperationStatus
                         if status not in (OracleOperationStatus.CONFIRMADA, OracleOperationStatus.REVERTIDA)]
                    )
                ).limit(1)
            ) is not None
        if has_pending_oracle_operation:
            def reconcile_once() -> None:
                try:
                    from app.adaptadores.reconcile import build_coordinator
                    coord = build_coordinator(app.state.session_factory)
                    coord.reconcile_pending(max_operations=50)
                except Exception as err:
                    logging.getLogger(__name__).warning(
                        "Oracle startup reconciliation unavailable: %s", type(err).__name__
                    )

            # A receipt wait must not block the API lifespan or healthcheck.
            Thread(target=reconcile_once, name="oracle-startup-reconcile", daemon=True).start()
        yield
    finally:
        motor.dispose()


app = FastAPI(title="PFI Backend", version="0.1.0", lifespan=lifespan)

# Orígenes habilitados para CORS, separados por coma. La SPA corre en otro
# dominio (Cloudflare Pages); sin la variable no se habilita ninguno (RNF-29).
allowed_origins = [
    origin.strip()
    for origin in os.environ.get("ORIGENES_PERMITIDOS", "").split(",")
    if origin.strip()
]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _problem(status: int, title: str, detail: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=s.Problem(title=title, detail=detail).model_dump(
            by_alias=True, exclude_none=True
        ),
    )


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Pydantic incluye el valor rechazado en `input`; en esta ruta podría ser
    # una firma, un token o bytes crudos que nunca deben volver en la respuesta.
    # Las demás rutas conservan exactamente la semántica HTTP 422 previa.
    if request.url.path == "/cola/reconciliacion":
        return _problem(422, "La solicitud de reconciliación es inválida.")
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Unauthorized)
async def _unauthorized(_: Request, exc: Unauthorized) -> JSONResponse:
    # No distingue entre reclamo inexistente y token inválido: hacerlo
    # convertiría el error en un oráculo que confirma qué números existen.
    return _problem(401, "No se pudo acreditar el acceso a este reclamo.")


@app.exception_handler(Forbidden)
async def _forbidden(_: Request, exc: Forbidden) -> JSONResponse:
    return _problem(403, "El perfil no admite esta operación.", str(exc))


@app.exception_handler(ClaimNotFound)
async def _not_found(_: Request, exc: ClaimNotFound) -> JSONResponse:
    return _problem(404, "No existe un reclamo con ese número.")


@app.exception_handler(TransitionNotAllowed)
async def _invalid_transition(_: Request, exc: TransitionNotAllowed) -> JSONResponse:
    return _problem(
        409, "La transición no figura en la matriz para el estado vigente.", str(exc)
    )

@app.exception_handler(OracleOperationUncertainError)
async def _oracle_uncertain(_: Request, exc: OracleOperationUncertainError) -> JSONResponse:
    return _problem(409, "Operación del oráculo incierta.", str(exc))


@app.exception_handler(OracleOperationRejectedError)
async def _oracle_rejected(_: Request, exc: OracleOperationRejectedError) -> JSONResponse:
    return _problem(409, "El contrato rechazó la operación antes del envío.", str(exc))


@app.exception_handler(WalletBlockedError)
async def _wallet_blocked(_: Request, exc: WalletBlockedError) -> JSONResponse:
    return _problem(409, "La wallet del oráculo se encuentra bloqueada.", str(exc))




@app.exception_handler(InvalidDamageDescription)
async def _invalid_damage_description(
    _: Request, exc: InvalidDamageDescription
) -> JSONResponse:
    return _problem(422, "La descripción del daño no es válida.", str(exc))


@app.exception_handler(EvidenceRejected)
async def _evidence_rejected(_: Request, exc: EvidenceRejected) -> JSONResponse:
    """Rechazo por archivo y no por envío: corregir el que falla no obliga a
    recargar los que pasaron (RF-02, MK-01b)."""
    cuerpo = s.EvidenceRejection(
        rejected=[
            s.RejectedFileOut(
                filename=rechazado.filename,
                reason=rechazado.reason,
                message=rechazado.message,
            )
            for rechazado in exc.rejected
        ],
        accepted_count=exc.accepted_count,
    )
    return JSONResponse(status_code=422, content=cuerpo.model_dump(by_alias=True))


@app.exception_handler(IpfsError)
async def _publication_failed(_: Request, exc: IpfsError) -> JSONResponse:
    """Excepción 7a de CU-01: un servicio externo no respondió y el reclamo no
    se creó. Sin número, sin constancia y sin duplicado (MK-02b)."""
    cuerpo = s.RegistrationFailure(
        message=(
            "No pudimos registrar el reclamo porque un servicio externo no "
            "respondió. No quedó ningún reclamo a medias: volvé a intentar."
        ),
        retryable=True,
        submission_preserved=True,
    )
    return JSONResponse(status_code=502, content=cuerpo.model_dump(by_alias=True))


@app.get("/health")
def health() -> dict[str, str]:
    """Estado del servicio, para consultas entrantes. Este servicio no
    emite sondeos salientes propios."""
    return {"status": "ok"}


app.include_router(router)

"""Resolución de perfil y autorización.

La autenticación y la gestión de identidades quedan fuera del prototipo
(adr-006); lo que sí se aplica es autorización. La regla que sostiene RF-48 es
que **el perfil lo resuelve siempre el servidor sobre el token** y nunca lo
declara el cliente: la SPA elige qué sesión presenta, no qué rol tiene.

Dos clases de token, con semánticas distintas. El del reclamante es una
capacidad emitida para un reclamo (RF-47): habilita ese expediente y ningún
otro, y no es derivable del número de seguimiento. El del personal interno es
una sesión configurada cuyo rol es un dato del propio token.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adaptadores.models import Claim
from app.dominio.claims import hash_token


class Profile(StrEnum):
    """Los cuatro perfiles de la matriz de visibilidad de 00-criterios.md §2."""

    RECLAMANTE = "reclamante"
    REVISOR = "revisor"
    OPERADOR = "operador"
    PUBLICO = "publico"


@dataclass(frozen=True)
class Principal:
    """Quién pide. `claim_id` solo lo tiene el reclamante: su token abre un
    expediente y no una cuenta."""

    profile: Profile
    identity: str
    claim_id: int | None = None


# Sesiones internas del prototipo. Mientras adr-006 deje la autenticación
# afuera, el conjunto es fijo y entra por entorno para no tener literales de
# configuración en el código (RNF-29). El formato es
# `token:perfil:identidad`, separado por comas.
#
# Los tokens arrancan con el nombre del perfil a propósito: los mocks de MSW de
# la SPA resuelven el perfil por prefijo, así que con esta forma el mismo token
# sirve contra los mocks y contra esta API, y pasar la SPA de uno a otro no
# obliga a cambiar credenciales. Las identidades son las que dibujan las
# láminas de MK-05 y MK-07.
SESIONES_POR_DEFECTO = "revisor-rev-04:revisor:rev-04,operador-op-02:operador:op-02"


def internal_sessions() -> dict[str, Principal]:
    crudo = os.environ.get("SESIONES_INTERNAS", SESIONES_POR_DEFECTO)
    sesiones: dict[str, Principal] = {}
    for entrada in crudo.split(","):
        entrada = entrada.strip()
        if not entrada:
            continue
        token, perfil, identidad = entrada.split(":")
        sesiones[token] = Principal(profile=Profile(perfil), identity=identidad)
    return sesiones


class Unauthorized(Exception):
    """Token ausente, vencido o emitido para otro reclamo (RF-47).

    No distingue entre reclamo inexistente y token inválido: hacerlo
    convertiría el error en un oráculo que confirma qué números existen.
    """


class Forbidden(Exception):
    """El perfil del token no admite la operación (RF-48)."""


def resolve_principal(session: Session, token: str | None) -> Principal:
    """Traduce el token al perfil que el servidor le reconoce."""
    if not token:
        raise Unauthorized("falta el token")

    interno = internal_sessions().get(token)
    if interno is not None:
        return interno

    reclamo = session.scalars(
        select(Claim).where(Claim.access_token_hash == hash_token(token))
    ).first()
    if reclamo is None:
        raise Unauthorized("token no reconocido")
    return Principal(
        profile=Profile.RECLAMANTE, identity=reclamo.tracking_number, claim_id=reclamo.id
    )


def require_profile(principal: Principal, *admitidos: Profile) -> None:
    if principal.profile not in admitidos:
        raise Forbidden(f"el perfil {principal.profile.value} no admite la operación")


def authorize_claim_access(principal: Principal, claim: Claim) -> None:
    """El reclamante solo abre el expediente para el que se emitió su token.

    Sin esta comprobación el token seguiría siendo una capacidad por nombre y
    no por efecto: alcanzaría con conocer otro número de seguimiento.
    """
    if principal.profile is Profile.RECLAMANTE and principal.claim_id != claim.id:
        raise Unauthorized("el token fue emitido para otro reclamo")

"""Máquina de estados del reclamo, derivada de `contract/states.yaml`.

Réplica de lo que `ClaimRegistry.sol` decide en la cadena. Es réplica y no
fuente: el contrato es el que asienta el estado, y esto existe para que el
backend sepa qué va a pasar antes de mandar la transacción y para rechazar
temprano lo que la matriz no admite. Ante divergencia entre las dos, vale la
cadena y la diferencia se cuenta como defecto.

La matriz de transiciones y el orden de precedencia se leen del diccionario;
acá no hay una segunda copia de ninguno de los dos.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.dominio.vocabulary import (
    ClaimStatus,
    PolicyMode,
    TransitionCause,
    allowed_transitions,
    cause_precedence,
    terminal_states,
)


class TransitionNotAllowed(Exception):
    """La transición pedida no figura en la matriz de adr-001 (RF-31)."""

    def __init__(
        self,
        from_status: ClaimStatus | None,
        to_status: ClaimStatus,
        cause: TransitionCause | None,
    ) -> None:
        self.from_status = from_status
        self.to_status = to_status
        self.cause = cause
        origen = from_status.value if from_status else "<entrada>"
        causa = cause.value if cause else "<sin causa>"
        super().__init__(
            f"transición no permitida: {origen} -> {to_status.value} por {causa}"
        )


@dataclass(frozen=True)
class Classification:
    """Destino y causa que produce una inferencia ya verificada."""

    status: ClaimStatus
    cause: TransitionCause


def is_terminal(status: ClaimStatus) -> bool:
    return status in terminal_states()


def validate_transition(
    from_status: ClaimStatus,
    to_status: ClaimStatus,
    cause: TransitionCause,
) -> None:
    """Levanta `TransitionNotAllowed` si la terna no está en la matriz.

    Los estados terminales se rechazan por la matriz misma, que no les da
    ninguna fila de salida; el chequeo explícito existe para que el mensaje
    diga que el reclamo ya está cerrado en lugar de hablar de una terna."""
    if is_terminal(from_status):
        raise TransitionNotAllowed(from_status, to_status, cause)
    if (from_status, to_status, cause) not in allowed_transitions():
        raise TransitionNotAllowed(from_status, to_status, cause)


def classify(
    *,
    confidence: float,
    derivation_threshold: float,
    amount_above_limit: bool,
    rules_consistent: bool,
    policy_mode: PolicyMode,
) -> Classification:
    """Reglas deterministas de clasificación (RF-28 a RF-30, RF-34, RF-35).

    Solo corre después de una verificación exitosa: las reglas se evalúan sobre
    una inferencia acreditada y nunca sobre una que falló (RF-27).

    Cuando concurre más de una condición de derivación, RF-26 emite una sola
    causa y ningún requerimiento dice cuál. El orden lo fija
    `cause_precedence` del diccionario, que replica el que `ClaimRegistry.sol`
    dejó asentado; iterarlo en vez de encadenar condiciones es lo que evita que
    las dos implementaciones se separen en silencio.
    """
    condiciones = {
        TransitionCause.INCONSISTENCIA_CON_REGLAS: (
            not rules_consistent,
            ClaimStatus.INCONSISTENCIA_EVIDENCIA,
        ),
        TransitionCause.CONFIANZA_BAJO_UMBRAL: (
            confidence < derivation_threshold,
            ClaimStatus.REQUIERE_REVISION,
        ),
        TransitionCause.MONTO_SOBRE_UMBRAL: (
            amount_above_limit,
            ClaimStatus.REQUIERE_REVISION,
        ),
        TransitionCause.POLITICA_CONFIRMACION_HUMANA: (
            policy_mode is PolicyMode.CONFIRMACION_HUMANA_OBLIGATORIA,
            ClaimStatus.REQUIERE_REVISION,
        ),
    }

    for causa in cause_precedence():
        se_cumple, destino = condiciones[causa]
        if se_cumple:
            return Classification(status=destino, cause=causa)

    return Classification(
        status=ClaimStatus.APROBABLE,
        cause=TransitionCause.CLASIFICACION_FAVORABLE,
    )


def settlement_cause(*, automatic: bool) -> TransitionCause:
    """Cuál de los dos caminos de Aprobable a Aprobado se asienta (adr-007).

    El backend declara el camino y el contrato lo registra; de esa distinción
    dependen los textos que ven el reclamante y el auditor externo."""
    return (
        TransitionCause.LIQUIDACION_AUTOMATICA
        if automatic
        else TransitionCause.LIQUIDACION_DEL_OPERADOR
    )


def resolution_cause(*, favorable: bool) -> TransitionCause:
    """Causa de la resolución humana asentada por el revisor (RF-38)."""
    return (
        TransitionCause.RESOLUCION_FAVORABLE
        if favorable
        else TransitionCause.RESOLUCION_DESFAVORABLE
    )

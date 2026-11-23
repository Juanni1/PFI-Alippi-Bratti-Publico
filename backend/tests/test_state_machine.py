"""Máquina de estados: matriz, precedencia de causa y las cinco invariantes.

Las invariantes se comprueban sobre el grafo que sale del diccionario, no sobre
una lista escrita a mano: si alguien agrega una transición que rompe una de
ellas, la prueba la encuentra sin que haya que acordarse de actualizarla.
"""

import pytest

from app.dominio.state_machine import (
    Classification,
    TransitionNotAllowed,
    classify,
    is_terminal,
    resolution_cause,
    settlement_cause,
    validate_transition,
)
from app.dominio.vocabulary import (
    ClaimStatus,
    PolicyMode,
    TransitionCause,
    allowed_transitions,
    terminal_states,
)

UMBRAL = 0.70


def _salidas(estado: ClaimStatus) -> set[ClaimStatus]:
    return {destino for origen, destino, _ in allowed_transitions() if origen is estado}


def _entradas(estado: ClaimStatus) -> set[ClaimStatus]:
    return {origen for origen, destino, _ in allowed_transitions() if destino is estado}


# ---------------------------------------------------------------------------
# Las cinco invariantes de adr-001
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estado", sorted(terminal_states()))
def test_invariante_1_los_terminales_no_tienen_salida(estado):
    assert _salidas(estado) == set()


def test_invariante_2_verificacion_fallida_es_la_unica_sin_prueba_valida():
    """Ninguna transición a un estado de clasificación ocurre sin prueba
    verificada; `VerificacionFallida` es la excepción por construcción, y es el
    único destino al que se llega desde `Registrado` sin haber verificado."""
    desde_registrado = _salidas(ClaimStatus.REGISTRADO)
    assert ClaimStatus.VERIFICACION_FALLIDA in desde_registrado
    clasificacion = desde_registrado - {ClaimStatus.VERIFICACION_FALLIDA}
    assert clasificacion == {
        ClaimStatus.APROBABLE,
        ClaimStatus.REQUIERE_REVISION,
        ClaimStatus.INCONSISTENCIA_EVIDENCIA,
    }


def test_invariante_3_la_clasificacion_automatica_no_produce_un_terminal():
    """Ningún destino alcanzable desde `Registrado` es terminal: la máquina no
    cierra un reclamo sin que intervenga una persona o una liquidación."""
    assert _salidas(ClaimStatus.REGISTRADO) & terminal_states() == set()


def test_invariante_4_todo_camino_a_aprobado_pasa_por_aprobable_o_revision():
    assert _entradas(ClaimStatus.APROBADO) == {
        ClaimStatus.APROBABLE,
        ClaimStatus.REQUIERE_REVISION,
    }


def test_invariante_5_desde_verificacion_fallida_solo_se_sale_por_revision():
    """Un reclamo que pasó por `VerificacionFallida` no llega a `Aprobado` sin
    intervención humana registrada: su única salida es `RequiereRevision`, y de
    ahí a `Aprobado` se llega solo por `resolve`."""
    assert _salidas(ClaimStatus.VERIFICACION_FALLIDA) == {ClaimStatus.REQUIERE_REVISION}


# ---------------------------------------------------------------------------
# Matriz de transiciones (RF-31, RF-40)
# ---------------------------------------------------------------------------


def test_una_transicion_de_la_matriz_se_acepta():
    validate_transition(
        ClaimStatus.APROBABLE,
        ClaimStatus.APROBADO,
        TransitionCause.LIQUIDACION_AUTOMATICA,
    )


def test_una_terna_que_no_esta_en_la_matriz_se_rechaza():
    with pytest.raises(TransitionNotAllowed):
        validate_transition(
            ClaimStatus.REGISTRADO,
            ClaimStatus.APROBADO,
            TransitionCause.CLASIFICACION_FAVORABLE,
        )


def test_el_destino_correcto_con_la_causa_equivocada_tambien_se_rechaza():
    """La matriz liga las tres cosas. Llegar a `Aprobado` desde `Aprobable` es
    válido, pero no por una causa de resolución humana."""
    with pytest.raises(TransitionNotAllowed):
        validate_transition(
            ClaimStatus.APROBABLE,
            ClaimStatus.APROBADO,
            TransitionCause.RESOLUCION_FAVORABLE,
        )


@pytest.mark.parametrize("estado", sorted(terminal_states()))
def test_ninguna_operacion_sale_de_un_estado_terminal(estado):
    assert is_terminal(estado)
    with pytest.raises(TransitionNotAllowed):
        validate_transition(
            estado, ClaimStatus.REQUIERE_REVISION, TransitionCause.ESCALAMIENTO_OBLIGATORIO
        )


# ---------------------------------------------------------------------------
# Clasificación (RF-28 a RF-30, RF-34, RF-35)
# ---------------------------------------------------------------------------


def _clasificar(**cambios) -> Classification:
    argumentos = {
        "confidence": 0.95,
        "derivation_threshold": UMBRAL,
        "amount_above_limit": False,
        "rules_consistent": True,
        "policy_mode": PolicyMode.AUTOMATICO_CON_ESCALAMIENTO,
    }
    argumentos.update(cambios)
    return classify(**argumentos)


def test_el_camino_feliz_llega_a_aprobable():
    assert _clasificar() == Classification(
        ClaimStatus.APROBABLE, TransitionCause.CLASIFICACION_FAVORABLE
    )


def test_la_confianza_bajo_el_umbral_deriva_a_revision():
    assert _clasificar(confidence=0.50) == Classification(
        ClaimStatus.REQUIERE_REVISION, TransitionCause.CONFIANZA_BAJO_UMBRAL
    )


def test_la_confianza_justo_en_el_umbral_no_deriva():
    """El umbral no es exclusivo: `confidence < threshold` deriva, la igualdad
    no. Replica el `confidenceBps < confidenceThresholdBps` del contrato."""
    assert _clasificar(confidence=UMBRAL).status is ClaimStatus.APROBABLE


def test_el_monto_sobre_el_limite_deriva_a_revision():
    assert _clasificar(amount_above_limit=True) == Classification(
        ClaimStatus.REQUIERE_REVISION, TransitionCause.MONTO_SOBRE_UMBRAL
    )


def test_la_politica_de_confirmacion_obligatoria_vacia_el_camino_automatico():
    """RF-35: con el modo activo ningún reclamo clasifica en `Aprobable`."""
    assert _clasificar(
        policy_mode=PolicyMode.CONFIRMACION_HUMANA_OBLIGATORIA
    ) == Classification(
        ClaimStatus.REQUIERE_REVISION, TransitionCause.POLITICA_CONFIRMACION_HUMANA
    )


def test_las_reglas_inconsistentes_van_a_inconsistencia_de_evidencia():
    assert _clasificar(rules_consistent=False) == Classification(
        ClaimStatus.INCONSISTENCIA_EVIDENCIA, TransitionCause.INCONSISTENCIA_CON_REGLAS
    )


@pytest.mark.parametrize(
    ("cambios", "esperada"),
    [
        (
            {"rules_consistent": False, "confidence": 0.10, "amount_above_limit": True,
             "policy_mode": PolicyMode.CONFIRMACION_HUMANA_OBLIGATORIA},
            TransitionCause.INCONSISTENCIA_CON_REGLAS,
        ),
        (
            {"confidence": 0.10, "amount_above_limit": True,
             "policy_mode": PolicyMode.CONFIRMACION_HUMANA_OBLIGATORIA},
            TransitionCause.CONFIANZA_BAJO_UMBRAL,
        ),
        (
            {"amount_above_limit": True,
             "policy_mode": PolicyMode.CONFIRMACION_HUMANA_OBLIGATORIA},
            TransitionCause.MONTO_SOBRE_UMBRAL,
        ),
        (
            {"policy_mode": PolicyMode.CONFIRMACION_HUMANA_OBLIGATORIA},
            TransitionCause.POLITICA_CONFIRMACION_HUMANA,
        ),
    ],
)
def test_cuando_concurren_varias_condiciones_se_asienta_una_sola(cambios, esperada):
    """RF-26 emite una sola causa. El orden es el de `cause_precedence` del
    diccionario, que replica el que `ClaimRegistry.sol` dejó asentado."""
    assert _clasificar(**cambios).cause is esperada


# ---------------------------------------------------------------------------
# Causas de liquidación y de resolución
# ---------------------------------------------------------------------------


def test_los_dos_caminos_de_liquidacion_dan_causas_distintas():
    """adr-007: de esta distinción dependen los textos de MK-03 y MK-04 y la
    traza que MK-08 le muestra al auditor externo."""
    assert settlement_cause(automatic=True) is TransitionCause.LIQUIDACION_AUTOMATICA
    assert settlement_cause(automatic=False) is TransitionCause.LIQUIDACION_DEL_OPERADOR


def test_las_dos_causas_de_liquidacion_estan_en_la_matriz():
    for automatica in (True, False):
        validate_transition(
            ClaimStatus.APROBABLE,
            ClaimStatus.APROBADO,
            settlement_cause(automatic=automatica),
        )


def test_la_resolucion_favorable_y_la_desfavorable_llegan_a_estados_distintos():
    validate_transition(
        ClaimStatus.REQUIERE_REVISION,
        ClaimStatus.APROBADO,
        resolution_cause(favorable=True),
    )
    validate_transition(
        ClaimStatus.REQUIERE_REVISION,
        ClaimStatus.RECHAZADO,
        resolution_cause(favorable=False),
    )

"""Conformidad entre los enums del código y `contract/states.yaml`.

Los enums se declaran a mano para que el editor los resuelva; estas pruebas son
la contrapartida de esa decisión. Sin ellas, una fila agregada al diccionario
quedaría sin representación en Python y el desvío aparecería recién en
ejecución.
"""

import pytest

from app.dominio import vocabulary as v


def _ids(clave: str) -> set[str]:
    return {entrada["id"] for entrada in v.load_dictionary()[clave]}


def test_los_estados_del_enum_son_exactamente_los_del_diccionario():
    assert {estado.value for estado in v.ClaimStatus} == _ids("states")


def test_las_causas_del_enum_son_exactamente_las_del_diccionario():
    assert {causa.value for causa in v.TransitionCause} == _ids("causes")


def test_los_dos_estados_terminales_son_aprobado_y_rechazado():
    assert v.terminal_states() == {v.ClaimStatus.APROBADO, v.ClaimStatus.RECHAZADO}


def test_toda_transicion_del_diccionario_usa_vocabulario_declarado():
    for transicion in v.load_dictionary()["transitions"]:
        if transicion["from"] is not None:
            v.ClaimStatus(transicion["from"])
        v.ClaimStatus(transicion["to"])
        for causa in transicion["causes"]:
            v.TransitionCause(causa)


def test_la_precedencia_solo_enumera_causas_de_derivacion():
    derivacion = {
        v.TransitionCause.INCONSISTENCIA_CON_REGLAS,
        v.TransitionCause.CONFIANZA_BAJO_UMBRAL,
        v.TransitionCause.MONTO_SOBRE_UMBRAL,
        v.TransitionCause.POLITICA_CONFIRMACION_HUMANA,
    }
    assert set(v.cause_precedence()) == derivacion


def test_ninguna_causa_quedo_sin_su_valor_de_solidity():
    """Guarda de regresión del desvío que adr-007 arrastró hasta el 21/08.

    Mientras `ClaimRegistry.sol` tuvo un único valor `Settlement`, dos causas
    viajaban con `solidity: null` y las pantallas del reclamante no podían
    elegir su texto. Si alguien vuelve a dejar una causa sin correspondencia en
    la cadena, esto lo frena acá y no en la demo.
    """
    sin_solidity = [
        entrada["id"]
        for entrada in v.load_dictionary()["causes"]
        if entrada.get("solidity") is None
    ]
    assert sin_solidity == []


@pytest.mark.parametrize("estado", list(v.ClaimStatus))
def test_todo_estado_tiene_etiqueta_para_el_reclamante(estado):
    """RNF-35: la etiqueta se sirve ya redactada desde el servidor, para que el
    identificador técnico no tenga que llegar a las pantallas del reclamante."""
    assert v.claimant_label(estado)


@pytest.mark.parametrize(
    "estado", [e for e in v.ClaimStatus if e not in v.terminal_states()]
)
def test_los_estados_no_terminales_no_exponen_su_identificador(estado):
    """En los cinco estados intermedios la etiqueta dice otra cosa que el
    identificador: «Registrado» se le muestra como «Recibido, en análisis».

    Los dos terminales quedan afuera a propósito. Sus etiquetas son «Aprobado» y
    «Rechazado», que coinciden con el identificador porque la palabra correcta en
    lenguaje natural resulta ser la misma; no es una filtración del vocabulario
    técnico.
    """
    assert v.claimant_label(estado) != estado.value


def test_la_causa_del_par_estado_causa_manda_sobre_la_del_estado():
    """Cuatro estados no tienen texto propio porque su causa sale del par. En
    Aprobado, los dos caminos de liquidación dan textos distintos."""
    automatica = v.claimant_cause_text(
        v.ClaimStatus.APROBADO, v.TransitionCause.LIQUIDACION_AUTOMATICA
    )
    del_operador = v.claimant_cause_text(
        v.ClaimStatus.APROBADO, v.TransitionCause.LIQUIDACION_DEL_OPERADOR
    )
    assert automatica and del_operador and automatica != del_operador


def test_las_causas_que_no_llegan_al_reclamante_devuelven_nulo():
    """`Rechazado` no tiene causa propia y `ResolucionDesfavorable` tampoco trae
    texto: el diccionario decide que ese motivo no se le explica al reclamante
    por esta vía, y la función devuelve nulo en lugar de improvisar una frase."""
    assert (
        v.claimant_cause_text(
            v.ClaimStatus.RECHAZADO, v.TransitionCause.RESOLUCION_DESFAVORABLE
        )
        is None
    )


def test_cuando_la_causa_no_trae_texto_cae_a_la_del_estado():
    """`ClasificacionFavorable` viaja sin texto, pero `Aprobable` sí tiene el
    suyo y es el que ve el reclamante."""
    assert v.claimant_cause_text(
        v.ClaimStatus.APROBABLE, v.TransitionCause.CLASIFICACION_FAVORABLE
    ) == v.claimant_cause_text(v.ClaimStatus.APROBABLE)

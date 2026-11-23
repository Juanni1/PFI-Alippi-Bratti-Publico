"""Vocabulario de estados y causas, derivado de `contract/states.yaml`.

Ese archivo es la frontera: de él salen también el enum de Solidity de
`PFI-Contracts` y el tipo de TypeScript de la SPA. Acá se lo carga una vez y
se lo expone como estructuras de solo lectura.

Los enums se declaran a mano y NO se generan desde el YAML. La generación
dinámica haría del archivo la única fuente, pero deja el código sin nombres
que un editor pueda resolver y convierte cualquier error de tipeo en un fallo
de ejecución. La conformidad se sostiene con la prueba de
`tests/test_vocabulary.py`, que compara ambos lados y corre en la compuerta de
CI: la divergencia se ve al integrar y no en producción.

Los valores son los identificadores en español del diccionario porque son
vocabulario del producto y viajan por la API (`ClaimStatusId` y
`TransitionCauseId` del contrato OpenAPI), no identificadores de código.
"""

from __future__ import annotations

import functools
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

RUTA_DICCIONARIO = Path(__file__).resolve().parents[2] / "contract" / "states.yaml"


class ClaimStatus(StrEnum):
    """Los siete estados de adr-001. `RequiereRevision` y `VerificacionFallida`
    comparten etiqueta y no comparten causa: el fallo de verificación señala un
    problema del lado del operador y no de la evidencia."""

    REGISTRADO = "Registrado"
    APROBABLE = "Aprobable"
    REQUIERE_REVISION = "RequiereRevision"
    INCONSISTENCIA_EVIDENCIA = "InconsistenciaEvidencia"
    VERIFICACION_FALLIDA = "VerificacionFallida"
    APROBADO = "Aprobado"
    RECHAZADO = "Rechazado"


class TransitionCause(StrEnum):
    """Vocabulario de causa del evento de RF-26. Los dos valores de liquidación
    distinguen los caminos de adr-007 y desde el 2026-08-21 el contrato los
    asienta por separado."""

    CLASIFICACION_FAVORABLE = "ClasificacionFavorable"
    CONFIANZA_BAJO_UMBRAL = "ConfianzaBajoUmbral"
    MONTO_SOBRE_UMBRAL = "MontoSobreUmbral"
    POLITICA_CONFIRMACION_HUMANA = "PoliticaConfirmacionHumana"
    INCONSISTENCIA_CON_REGLAS = "InconsistenciaConReglas"
    FALLO_DE_VERIFICACION = "FalloDeVerificacion"
    ESCALAMIENTO_OBLIGATORIO = "EscalamientoObligatorio"
    LIQUIDACION_AUTOMATICA = "LiquidacionAutomatica"
    LIQUIDACION_DEL_OPERADOR = "LiquidacionDelOperador"
    RESOLUCION_FAVORABLE = "ResolucionFavorable"
    RESOLUCION_DESFAVORABLE = "ResolucionDesfavorable"


class DamageClass(StrEnum):
    """Las seis clases del clasificador entrenado sobre CarDD. El circuito emite
    logits; softmax y argmax se calculan fuera (RF-16).

    La sexta clase es la fisura y no la corrosión. CarDD no tiene una clase de
    corrosión, y el documento nombra la fisura en todas partes: es la más
    difícil del conjunto y la que sostiene el argumento de utilidad operativa,
    con su F1 pasando de 0,557 a 0,884 con el ajuste fino. El error no salía a
    la luz porque el motor simulado elige la clase por el digesto de la
    fotografía y nunca comparó su salida contra el modelo real.

    El orden de declaración **no** es el de los logits. La correspondencia entre
    el índice que emite la cabeza y la clase la fija el conjunto con el que se
    entrenó, y vive en el adaptador de inferencia junto al artefacto que la
    determina."""

    ABOLLADURA = "abolladura"
    RAYON = "rayon"
    ROTURA_VIDRIO = "rotura_vidrio"
    FARO_ROTO = "faro_roto"
    NEUMATICO_DESINFLADO = "neumatico_desinflado"
    FISURA = "fisura"


@functools.lru_cache(maxsize=1)
def load_dictionary() -> dict[str, Any]:
    """Diccionario crudo, tal como está en el archivo de frontera."""
    with RUTA_DICCIONARIO.open(encoding="utf-8") as archivo:
        return yaml.safe_load(archivo)


@functools.lru_cache(maxsize=1)
def terminal_states() -> frozenset[ClaimStatus]:
    """Los estados sin transiciones de salida (RF-40)."""
    return frozenset(
        ClaimStatus(entrada["id"])
        for entrada in load_dictionary()["states"]
        if entrada["terminal"]
    )


@functools.lru_cache(maxsize=1)
def allowed_transitions() -> frozenset[tuple[ClaimStatus, ClaimStatus, TransitionCause]]:
    """Las combinaciones origen-destino-causa que la matriz de adr-001 admite.

    La entrada al sistema (`from: null`) no es una transición de la matriz y se
    excluye: el registro del reclamo tiene su propio evento.
    """
    permitidas: set[tuple[ClaimStatus, ClaimStatus, TransitionCause]] = set()
    for transicion in load_dictionary()["transitions"]:
        if transicion["from"] is None:
            continue
        origen = ClaimStatus(transicion["from"])
        destino = ClaimStatus(transicion["to"])
        for causa in transicion["causes"]:
            permitidas.add((origen, destino, TransitionCause(causa)))
    return frozenset(permitidas)


@functools.lru_cache(maxsize=1)
def cause_precedence() -> tuple[TransitionCause, ...]:
    """Orden en que se resuelve el empate cuando concurre más de una condición
    de derivación. RF-26 emite una sola causa y ningún requerimiento dice cuál;
    `ClaimRegistry.sol` fijó esta precedencia y el diccionario la replica."""
    return tuple(
        TransitionCause(nombre) for nombre in load_dictionary()["cause_precedence"]
    )


@functools.lru_cache(maxsize=1)
def _claimant_texts() -> dict[str, dict[str, str | None]]:
    diccionario = load_dictionary()
    por_estado = {
        entrada["id"]: {"label": entrada["label"], "cause": entrada["cause"]}
        for entrada in diccionario["states"]
    }
    por_causa = {
        entrada["id"]: {"text": entrada["text"]} for entrada in diccionario["causes"]
    }
    return {"states": por_estado, "causes": por_causa}


def claimant_label(status: ClaimStatus) -> str:
    """Etiqueta redactada que ve el reclamante. Se copia del diccionario y no se
    reescribe por pantalla: es lo que hace que RNF-35 se aplique del lado del
    servidor y no del cliente."""
    return _claimant_texts()["states"][status.value]["label"]


def claimant_cause_text(
    status: ClaimStatus, cause: TransitionCause | None = None
) -> str | None:
    """Texto de causa que ve el reclamante, o `None` si esa causa no llega a sus
    pantallas.

    Cuatro estados no tienen un texto propio porque su causa no sale del estado
    sino del par estado-causa; para ellos manda el vocabulario de causa.
    """
    if cause is not None:
        texto = _claimant_texts()["causes"][cause.value]["text"]
        if texto is not None:
            return texto.strip()
    propio = _claimant_texts()["states"][status.value]["cause"]
    return propio.strip() if propio else None


# ---------------------------------------------------------------------------
# Vocabulario que no vive en el archivo de frontera porque no cruza a Solidity:
# describe el trabajo interno del backend. Los valores que sí viajan por la API
# se copian del contrato OpenAPI y están anotados con el esquema del que salen.
# ---------------------------------------------------------------------------


class JobStatus(StrEnum):
    """Estado de un job de la cola. Los tres primeros son los contadores que
    `QueueState.counters` expone; `COMPLETADO` no se cuenta porque la pantalla
    del operador mira lo que falta, no lo que ya salió."""

    PENDIENTE = "pendiente"
    EN_CURSO = "en_curso"
    FALLIDO = "fallido"
    COMPLETADO = "completado"


class JobStage(StrEnum):
    """Etapa dentro de un job en curso (`QueueState.running[].stage`). Recorre
    el camino de features-afuera: se recupera la evidencia, se extraen las
    características fuera del circuito, se genera la prueba de la cabeza, se la
    verifica localmente y recién entonces se envía."""

    RECUPERACION = "recuperacion"
    EXTRACCION = "extraccion"
    GENERACION = "generacion"
    VERIFICACION_LOCAL = "verificacion_local"
    ENVIO = "envio"


class JobFailureReason(StrEnum):
    """Causa del último fallo (`QueueState.failed[].reason`). RF-13 no distingue
    los fallos que el reintento corrige de los que no: la evidencia no
    recuperada es transitoria y el hash del artefacto que no coincide da el
    mismo resultado en cada corrida."""

    EVIDENCIA_NO_RECUPERADA = "evidencia_no_recuperada"
    ARTEFACTO_NO_DECLARADO = "artefacto_no_declarado"
    MEMORIA_AGOTADA = "memoria_agotada"
    VERIFICACION_LOCAL_FALLIDA = "verificacion_local_fallida"


class PolicyMode(StrEnum):
    """Los dos polos del mismo eje (`Configuration.policyMode`). No se combinan:
    el de confirmación obligatoria vacía la entrada del otro."""

    AUTOMATICO_CON_ESCALAMIENTO = "automatico_con_escalamiento"
    CONFIRMACION_HUMANA_OBLIGATORIA = "confirmacion_humana_obligatoria"


class PinStatus(StrEnum):
    """Estado de fijación de una evidencia. Se registra dos veces, una por
    custodio: el nodo propio y el servicio remoto."""

    PENDIENTE = "pendiente"
    FIJADO = "fijado"
    FALLIDO = "fallido"


class OracleOperationStatus(StrEnum):
    """Estados de una intención en oracle_operations (ADR-018)."""

    PREPARADA = "preparada"
    FIRMADA = "firmada"
    ENVIADA = "enviada"
    CONFIRMADA = "confirmada"
    REVERTIDA = "revertida"
    INCIERTA = "incierta"
    FALLIDA_PRE_ENVIO = "fallida_pre_envio"


class OracleOperationType(StrEnum):
    """Efectos admitidos por el coordinador de la wallet (ADR-018)."""

    REGISTER_CLAIM = "register_claim"
    SUBMIT_INFERENCE = "submit_inference"
    RESOLVE = "resolve"
    SETTLE = "settle"

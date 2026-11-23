"""El generador de pruebas, simulado y real.

Las pruebas del generador real se saltan si `RUTA_ARTEFACTOS` no tiene los tres
artefactos distribuidos del circuito. Las dos claves no se piden porque no se
distribuyen: el generador las deriva al construirse, y que las derive es parte
de lo que estas pruebas ejercitan.
"""

from __future__ import annotations

import os
from array import array

import pytest

from app.adaptadores.proving import (
    ARTEFACTOS_DISTRIBUIDOS,
    CURVA,
    ESQUEMA,
    LOGROWS,
    EzklProofGenerator,
    SimulatedProofGenerator,
    build_proof_generator,
)
from app.dominio.ports import InferenceResult
from app.dominio.vocabulary import DamageClass

RUTA = os.environ.get("RUTA_ARTEFACTOS", "artefactos")

sin_circuito = pytest.mark.skipif(
    not all(os.path.exists(os.path.join(RUTA, n)) for n in ARTEFACTOS_DISTRIBUIDOS),
    reason=f"No hay artefactos del circuito en {RUTA!r}",
)

DIMENSION_DEL_VECTOR = 576


def _inferencia() -> InferenceResult:
    """Una inferencia con la forma que el circuito espera. Los valores no
    importan: el circuito prueba que la cabeza se aplicó, no qué dio."""
    caracteristicas = array("f", [(i % 32) / 32.0 for i in range(DIMENSION_DEL_VECTOR)])
    logits = array("f", [0.1, 0.2, 3.0, 0.05, 0.4, 0.02])
    return InferenceResult(
        feature_vector=caracteristicas.tobytes(),
        logits=logits.tobytes(),
        predicted_class=DamageClass.ROTURA_VIDRIO,
        confidence=0.93,
        extractor_artifact_hash="0" * 64,
        model_version="mnv3_ft-r224",
    )


# ---------------------------------------------------------------------------
# Elección por entorno
# ---------------------------------------------------------------------------


def test_sin_variable_de_entorno_corre_el_simulado(monkeypatch):
    monkeypatch.delenv("GENERADOR_PRUEBAS", raising=False)
    assert isinstance(build_proof_generator(), SimulatedProofGenerator)


def test_elegir_ezkl_sin_artefactos_falla_en_vez_de_caer_al_simulado(
    monkeypatch, tmp_path
):
    """Caer en silencio dejaría a alguien creyendo que hay criptografía donde
    no la hay, que es exactamente lo que la marca de simulación evita."""
    monkeypatch.setenv("GENERADOR_PRUEBAS", "ezkl")
    monkeypatch.setenv("RUTA_ARTEFACTOS", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        build_proof_generator()


# ---------------------------------------------------------------------------
# Simulado
# ---------------------------------------------------------------------------


def test_el_simulado_se_declara_simulado():
    """Sin esta marca una traza producida así sería indistinguible de una
    acreditada para quien lee la base."""
    resultado = SimulatedProofGenerator().prove(_inferencia())
    assert resultado.circuit_parameters["simulated"] is True


def test_el_simulado_no_hace_criptografia():
    """El artefacto tiene la forma correcta y nada más: es un digesto con un
    prefijo, y conviene que la prueba lo diga en voz alta."""
    resultado = SimulatedProofGenerator().prove(_inferencia())
    assert resultado.proof.startswith(b"prueba-simulada:")


# ---------------------------------------------------------------------------
# Real
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def generador() -> EzklProofGenerator:
    return EzklProofGenerator(RUTA)


@sin_circuito
def test_la_prueba_real_verifica_del_lado_propio(generador):
    """Verificar antes de mandar evita gastar una transacción en una prueba que
    ya se sabe inválida, que es lo que el puerto pide."""
    resultado = generador.prove(_inferencia())
    assert resultado.verified_locally is True


@sin_circuito
def test_la_prueba_real_no_lleva_la_marca_de_simulacion(generador):
    """Es la marca la que separa una traza acreditada de una que no lo está, y
    tiene que faltar exactamente cuando la criptografía ocurrió."""
    resultado = generador.prove(_inferencia())
    assert "simulated" not in resultado.circuit_parameters
    assert resultado.circuit_parameters["logrows"] == LOGROWS
    assert resultado.circuit_parameters["curve"] == CURVA
    assert resultado.circuit_parameters["scheme"] == ESQUEMA


@sin_circuito
def test_la_prueba_real_tiene_el_tamano_medido(generador):
    """100,6 KB es la cifra que sostiene RNF-02 y la que decide que la prueba
    entra en una única transacción. Un desvío grande acá significaría que el
    circuito no es el que el documento describe."""
    resultado = generador.prove(_inferencia())
    kilobytes = len(resultado.proof) / 1024
    assert 90 < kilobytes < 115, f"la prueba pesa {kilobytes:.1f} KB"


@sin_circuito
def test_la_prueba_real_declara_la_frontera_escalonada(generador):
    """El circuito cubre de las características a los logits y nada más. Que el
    artefacto lo diga es lo que impide leer la prueba como si acreditara la
    fotografía."""
    resultado = generador.prove(_inferencia())
    assert resultado.declared_visibility["proven_segment"] == "features->logits"
    assert resultado.declared_visibility["replicated_segment"] == "pixel->features"
    # El vector viaja como entrada pública: sube el gas y abre la superficie de
    # privacidad de RNF-25, y es lo que vuelve comparable el tramo replicable.
    assert resultado.declared_visibility["input"] == "public"


@sin_circuito
def test_el_hash_de_la_prueba_es_el_de_sus_bytes(generador):
    import hashlib

    resultado = generador.prove(_inferencia())
    assert resultado.proof_hash == "0x" + hashlib.sha256(resultado.proof).hexdigest()


@sin_circuito
def test_las_claves_se_derivan_del_circuito_y_no_se_distribuyen(tmp_path):
    """La clave de generación pesa 133 MB y no entra en un repositorio, pero
    tampoco hace falta: el setup sobre el mismo circuito compilado y los mismos
    parámetros estructurados es determinista. Que la de verificación se pueda
    re-derivar es además más fuerte que distribuirla, porque quien audita el
    verificador on-chain no tiene que confiar en la que se publicó."""
    import shutil

    for nombre in ARTEFACTOS_DISTRIBUIDOS:
        shutil.copy(os.path.join(RUTA, nombre), tmp_path)
    assert not list(tmp_path.glob("*.key"))

    primero = EzklProofGenerator(str(tmp_path))
    derivadas = sorted(p.name for p in tmp_path.glob("*.key"))
    assert derivadas == ["pk_r576.key", "vk_r576.key"]

    # La comprobación del hash de la clave de verificación corre dentro del
    # constructor: si el setup no fuera determinista, esta segunda construcción
    # sobre las claves ya escritas o una tercera sobre un directorio limpio
    # levantarían ValueError.
    assert primero.prove(_inferencia()).verified_locally is True

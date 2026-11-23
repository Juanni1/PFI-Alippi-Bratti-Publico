"""El motor de inferencia real, contra los artefactos publicados.

Las pruebas se saltan si `RUTA_ARTEFACTOS` no apunta a un directorio con los dos
ONNX. Los artefactos no se versionan en este repositorio —se publican con su
hash, por RNF-29 y RNF-32— así que exigirlos volvería roja la suite de
cualquiera que clone sin descargarlos. Lo que no se hace es sustituirlos por un
doble: un doble comprobaría que el código corre, y lo que hay que comprobar es
que reproduce el modelo que el documento cita.
"""

from __future__ import annotations

import io
import os

import pytest

from app.adaptadores.inference import (
    ARTEFACTO_DEL_EXTRACTOR,
    ARTEFACTO_DE_LA_CABEZA,
    CLASES_DEL_MODELO,
    HASH_DECLARADO_DEL_EXTRACTOR,
    DIMENSION_DEL_VECTOR,
    OnnxInferenceEngine,
    StubInferenceEngine,
    build_inference_engine,
)
from app.dominio.vocabulary import DamageClass

# sha256 del extractor con ajuste fino exportado el 2026-08-23, que es lo que
# adr-011 exige publicar junto al artefacto y contra lo que un tercero
# comprueba que está replicando el modelo correcto.
HASH_DEL_EXTRACTOR = "96c616e82bbf9617c9b6cdb935f9fdc9c4505727a9742c8413e2e90c83c50a6a"

RUTA = os.environ.get("RUTA_ARTEFACTOS", "artefactos")

_faltan = not all(
    os.path.exists(os.path.join(RUTA, nombre))
    for nombre in (ARTEFACTO_DEL_EXTRACTOR, ARTEFACTO_DE_LA_CABEZA)
)
sin_artefactos = pytest.mark.skipif(
    _faltan, reason=f"No hay artefactos ONNX en {RUTA!r}"
)


@pytest.fixture(scope="module")
def motor() -> OnnxInferenceEngine:
    return OnnxInferenceEngine(RUTA)


def _fotografia(color: tuple[int, int, int] = (120, 90, 60), lado: int = 300) -> bytes:
    """Un JPEG cualquiera: estas pruebas miran la forma de la salida y la
    identidad del artefacto, no el acierto del clasificador."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (lado, lado), color).save(buffer, format="JPEG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Elección por entorno
# ---------------------------------------------------------------------------


def test_sin_variable_de_entorno_corre_el_stub(monkeypatch):
    """El stub es el que mantiene la demo en pie mientras los artefactos no
    estén, y por eso es el default."""
    monkeypatch.delenv("MOTOR_INFERENCIA", raising=False)
    assert isinstance(build_inference_engine(), StubInferenceEngine)


def test_el_stub_no_se_puede_confundir_con_el_motor_real():
    """El prefijo del hash es lo que impide que una traza producida con el stub
    sostenga la replicación de RF-53."""
    resultado = StubInferenceEngine().run([_fotografia()])
    assert resultado.extractor_artifact_hash.startswith("stub:")
    assert resultado.model_version == "stub-0"


# ---------------------------------------------------------------------------
# Motor real
# ---------------------------------------------------------------------------


@sin_artefactos
def test_el_motor_real_declara_el_hash_del_extractor_publicado(motor):
    """RF-53 y RNF-32: sin saber con qué artefacto se produjo el vector, el
    tercero que reejecuta no tendría contra qué contrastar su resultado."""
    import onnxruntime

    resultado = motor.run([_fotografia()])
    assert resultado.extractor_artifact_hash == HASH_DEL_EXTRACTOR
    assert not resultado.extractor_artifact_hash.startswith("stub:")
    assert resultado.model_version == "mnv3_ft-r224"
    sesion_extractor = motor._sesion(ARTEFACTO_DEL_EXTRACTOR)
    opciones = sesion_extractor.get_session_options()
    assert opciones.intra_op_num_threads == 1
    assert opciones.inter_op_num_threads == 1
    assert opciones.execution_mode == onnxruntime.ExecutionMode.ORT_SEQUENTIAL


@sin_artefactos
def test_la_salida_tiene_la_forma_que_el_circuito_espera(motor):
    """576 características es la dimensión de MobileNetV3-small y la entrada
    pública del circuito; seis logits, las clases de CarDD."""
    resultado = motor.run([_fotografia()])
    assert len(resultado.feature_vector) == DIMENSION_DEL_VECTOR * 4
    assert len(resultado.logits) == len(CLASES_DEL_MODELO) * 4
    assert 0.0 < resultado.confidence <= 1.0
    assert resultado.predicted_class in set(DamageClass)


@sin_artefactos
def test_la_misma_fotografia_da_siempre_el_mismo_vector(motor):
    """La replicación de RF-53 exige igualdad exacta: si el propio sistema no
    fuera determinista sobre la misma máquina, no tendría sentido pedirle
    determinismo a un tercero."""
    imagen = _fotografia()
    primero = motor.run([imagen])
    segundo = motor.run([imagen])
    assert primero.feature_vector == segundo.feature_vector
    assert primero.logits == segundo.logits


@sin_artefactos
def test_dos_fotografias_distintas_dan_vectores_distintos(motor):
    resultado_a = motor.run([_fotografia((200, 30, 30))])
    resultado_b = motor.run([_fotografia((30, 30, 200))])
    assert resultado_a.feature_vector != resultado_b.feature_vector


@sin_artefactos
def test_sin_evidencia_falla_en_vez_de_inventar_un_vector(motor):
    with pytest.raises(ValueError):
        motor.run([])


def test_el_codigo_y_la_prueba_declaran_el_mismo_hash():
    """Si se reexporta el extractor hay que tocar los dos lugares, y este test
    es lo que impide que uno quede atrás del otro."""
    assert HASH_DECLARADO_DEL_EXTRACTOR == HASH_DEL_EXTRACTOR


def test_un_directorio_sin_artefactos_no_construye_el_motor(tmp_path):
    """Arrancar con el motor ONNX y sin artefactos tiene que fallar al
    construirlo, no al primer reclamo con el healthcheck en verde."""
    with pytest.raises(FileNotFoundError):
        OnnxInferenceEngine(str(tmp_path))


@sin_artefactos
def test_un_extractor_que_no_es_el_declarado_no_construye_el_motor(tmp_path):
    """El caso peor: un extractor distinto no rompe nada, clasifica. Y el
    sistema publicaría su hash como si fuera el declarado, con lo que la
    replicación de RF-53 quedaría comparando contra un artefacto que no
    produjo ese vector."""
    import shutil

    shutil.copy(os.path.join(RUTA, ARTEFACTO_DE_LA_CABEZA), tmp_path)
    (tmp_path / ARTEFACTO_DEL_EXTRACTOR).write_bytes(b"no soy el extractor")
    with pytest.raises(ValueError, match="sha256"):
        OnnxInferenceEngine(str(tmp_path))


def test_el_orden_de_clases_no_es_el_del_vocabulario():
    """Custodia el bug que el motor simulado tapaba. Las carpetas de CarDD se
    recorren en orden alfabético y ese es el orden que la cabeza aprendió;
    usar `list(DamageClass)` daba una permutación silenciosa, con el modelo
    clasificando bien y el sistema mostrando otra clase."""
    assert CLASES_DEL_MODELO[0] is DamageClass.FISURA           # crack
    assert CLASES_DEL_MODELO[1] is DamageClass.ABOLLADURA       # dent
    assert CLASES_DEL_MODELO[4] is DamageClass.RAYON            # scratch
    assert tuple(DamageClass)[:2] != CLASES_DEL_MODELO[:2]
    assert set(CLASES_DEL_MODELO) == set(DamageClass)


def test_la_sexta_clase_es_la_fisura_y_no_la_corrosion():
    """CarDD no tiene clase de corrosión, y el documento nombra la fisura en
    todas partes: es la que sostiene el argumento de utilidad operativa, con su
    F1 pasando de 0,557 a 0,884 con el ajuste fino."""
    assert DamageClass.FISURA.value == "fisura"
    assert "corrosion" not in {clase.value for clase in DamageClass}


def test_las_urls_publicadas_corresponden_a_los_cid_de_pinata():
    """PFI-95: las constantes exportadas apuntan a los CID inmutables del ONNX
    y de su manifiesto de preprocesamiento."""
    from app.adaptadores.inference import (
        CID_PUBLICADO_DEL_EXTRACTOR,
        CID_PUBLICADO_DEL_PREPROCESAMIENTO,
        URL_PUBLICADA_DEL_EXTRACTOR,
        URL_PUBLICADA_DEL_PREPROCESAMIENTO,
    )

    assert URL_PUBLICADA_DEL_EXTRACTOR.endswith(f"/{CID_PUBLICADO_DEL_EXTRACTOR}")
    assert URL_PUBLICADA_DEL_PREPROCESAMIENTO.endswith(
        f"/{CID_PUBLICADO_DEL_PREPROCESAMIENTO}"
    )

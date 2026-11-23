"""Motor de inferencia: el tramo off-chain de features-afuera.

Dos implementaciones. `OnnxInferenceEngine` corre los dos artefactos reales
—el extractor MobileNetV3-small ajustado y la cabeza— con ONNX Runtime.
`StubInferenceEngine` produce una salida determinista sin modelo, para que el
resto del sistema se pueda ejercitar mientras los artefactos no estén.

La distinción importa y no se disimula: `model_version` dice cuál corrió, y el
`extractor_artifact_hash` del stub no es el hash de ningún ONNX publicado, de
modo que una traza producida con el stub no se puede confundir con una real ni
sostener la replicación de RF-53.
"""

from __future__ import annotations

import hashlib
import math
import os
from array import array

from app.dominio.ports import InferenceResult
from app.dominio.vocabulary import DamageClass

DIMENSION_DEL_VECTOR = 576
VERSION_DEL_STUB = "stub-0"


def _softmax(valores: list[float]) -> list[float]:
    """Softmax fuera del circuito (RF-16): el circuito emite los logits crudos
    y el porcentaje se calcula acá."""
    mayor = max(valores)
    exponenciales = [math.exp(valor - mayor) for valor in valores]
    total = sum(exponenciales)
    return [exponencial / total for exponencial in exponenciales]


# Proporciones medidas de la curva de RNF-15 sobre las 785 muestras de prueba
# (adr-014): por debajo del umbral de derivación cae el 52,4 % de los casos,
# entre los dos umbrales queda el 33,5 % y por encima del de liquidación
# automática el 14,1 %.
FRACCION_POR_DEBAJO_DE_DERIVACION = 0.524
FRACCION_HASTA_LIQUIDACION = 0.859
UMBRAL_DE_DERIVACION = 0.90
UMBRAL_DE_LIQUIDACION = 0.95


def _confianza_calibrada(digesto: bytes) -> float:
    """Confianza cuya distribución imita a la del clasificador real.

    Sin esto el stub producía confianzas concentradas entre 0,4 y 0,7, y
    superaba el umbral de derivación en el 1,2 % de los casos: cualquier
    recorrido terminaba siempre en revisión humana y los otros dos caminos del
    diseño no llegaban a ocurrir nunca.

    La distribución no se inventa. Sale de la curva que adr-014 midió sobre el
    clasificador adoptado, de modo que el stub reparte los casos entre los tres
    tramos en las proporciones que el modelo real produjo. Sigue siendo un
    stub, y su `model_version` y el rótulo `simulated` de la prueba lo
    declaran; lo que deja de ser arbitrario es cuántos casos manda a cada lado.
    """
    # Uniforme reproducible en [0, 1) a partir de dos bytes del digesto.
    uniforme = int.from_bytes(digesto[1:3], "big") / 65536.0
    # Segundo uniforme, para repartir dentro del tramo elegido.
    dentro = int.from_bytes(digesto[3:5], "big") / 65536.0

    if uniforme < FRACCION_POR_DEBAJO_DE_DERIVACION:
        return 0.30 + dentro * (UMBRAL_DE_DERIVACION - 0.30)
    if uniforme < FRACCION_HASTA_LIQUIDACION:
        return UMBRAL_DE_DERIVACION + dentro * (UMBRAL_DE_LIQUIDACION - UMBRAL_DE_DERIVACION)
    return UMBRAL_DE_LIQUIDACION + dentro * (0.999 - UMBRAL_DE_LIQUIDACION)


class StubInferenceEngine:
    """Inferencia determinista sin modelo.

    Las mismas fotografías dan siempre la misma clase y la misma confianza, que
    es lo que permite escribir pruebas sobre el worker y lo que hace que una
    demo se comporte igual dos veces seguidas.
    """

    model_version = VERSION_DEL_STUB

    def artifacts_identity(self) -> str:
        return VERSION_DEL_STUB

    def run(self, images: list[bytes]) -> InferenceResult:
        digesto = hashlib.sha256(b"".join(images)).digest()

        # Vector reproducible a partir del digesto, en el mismo formato en que
        # el extractor real entrega el suyo.
        semilla = array("f")
        for indice in range(DIMENSION_DEL_VECTOR):
            byte = digesto[indice % len(digesto)]
            semilla.append((byte / 255.0) - 0.5)

        clases = list(DamageClass)
        ganadora = digesto[0] % len(clases)
        confianza = _confianza_calibrada(digesto)

        # Logits que producen exactamente esa confianza al pasar por softmax.
        # Con la clase ganadora en `a` y las otras cinco en cero,
        # c = e^a / (e^a + 5), de donde a = ln(5c / (1 - c)).
        ventaja = math.log(5.0 * confianza / (1.0 - confianza))
        crudos = [ventaja if indice == ganadora else 0.0 for indice in range(len(clases))]

        return InferenceResult(
            feature_vector=semilla.tobytes(),
            logits=array("f", crudos).tobytes(),
            predicted_class=clases[ganadora],
            confidence=confianza,
            # No es el hash de ningún ONNX publicado: lo declara el prefijo.
            extractor_artifact_hash=f"stub:{digesto.hex()[:32]}",
            model_version=VERSION_DEL_STUB,
        )


# Preprocesamiento canónico del extractor, especificado en
# `50-conceptos/preprocesamiento-canonico-del-extractor.md`. Es lo que vuelve
# ejecutable la replicación de RF-53: el auditor que reejecuta parte del archivo
# direccionado por CID, no de un tensor ya preparado, así que el redimensionado
# y la normalización son tanto parte del artefacto como el grafo ONNX.
RESOLUCION_DEL_EXTRACTOR = 224
MEDIA_IMAGENET = (0.485, 0.456, 0.406)
DESVIO_IMAGENET = (0.229, 0.224, 0.225)

ARTEFACTO_DEL_EXTRACTOR = "backbone_mnv3_ft.onnx"
ARTEFACTO_DE_LA_CABEZA = "head_mnv3_ft.onnx"
VERSION_DEL_MOTOR_ONNX = "mnv3_ft-r224"

# sha256 del extractor con ajuste fino, exportado el 2026-08-23 desde el
# checkpoint que adr-011 adopta. Es lo que un tercero comprueba antes de
# replicar, y acá sirve además para que el servicio se niegue a arrancar con
# otro artefacto en lugar de clasificar con él y declararlo como si fuera este.
HASH_DECLARADO_DEL_EXTRACTOR = (
    "96c616e82bbf9617c9b6cdb935f9fdc9c4505727a9742c8413e2e90c83c50a6a"
)

# Artefactos publicados en IPFS/Pinata para la replicación externa
# (RF-53, RNF-27, RNF-32 y RNF-34).
CID_PUBLICADO_DEL_EXTRACTOR = (
    "bafybeiczwkityamcb73l5lgvleom3zapa7grfny3abov3kp5ygu5ebffgu"
)
CID_PUBLICADO_DEL_PREPROCESAMIENTO = (
    "bafkreie2sfkzvshkdqybyujyrk4zzcdzrczqvxuhlvnzcn7t2phl5uiyom"
)
PASARELA_PUBLICACION_POR_DEFECTO = "https://gateway.pinata.cloud/ipfs"
URL_PUBLICADA_DEL_EXTRACTOR = (
    f"{PASARELA_PUBLICACION_POR_DEFECTO}/{CID_PUBLICADO_DEL_EXTRACTOR}"
)
URL_PUBLICADA_DEL_PREPROCESAMIENTO = (
    f"{PASARELA_PUBLICACION_POR_DEFECTO}/{CID_PUBLICADO_DEL_PREPROCESAMIENTO}"
)

# Qué clase significa cada índice de logit. Lo fija el conjunto con el que se
# entrenó y no el orden en que el vocabulario declara sus miembros: las carpetas
# de CarDD se recorren en orden alfabético —crack, dent, glass_shatter,
# lamp_broken, scratch, tire_flat— y ese es el orden que la cabeza aprendió.
# Confiar en `list(DamageClass)` daba una permutación silenciosa: el motor
# clasificaba bien y el sistema mostraba otra clase.
CLASES_DEL_MODELO = (
    DamageClass.FISURA,                # crack
    DamageClass.ABOLLADURA,            # dent
    DamageClass.ROTURA_VIDRIO,         # glass_shatter
    DamageClass.FARO_ROTO,             # lamp_broken
    DamageClass.RAYON,                 # scratch
    DamageClass.NEUMATICO_DESINFLADO,  # tire_flat
)


class OnnxInferenceEngine:
    """Extractor y cabeza reales, con ONNX Runtime.

    `onnxruntime` y `numpy` se importan dentro de los métodos y no arriba: son
    dependencias pesadas que solo hacen falta cuando esta implementación se usa
    de verdad, y dejarlas afuera del arranque mantiene liviana la imagen que se
    despliega. Los artefactos no se versionan en este repositorio; llegan por
    `RUTA_ARTEFACTOS` con el hash que `adr-011` exige publicar.

    El preprocesamiento se reimplementa con Pillow y numpy en lugar de arrastrar
    torchvision, y esa reimplementación no se dio por buena: se comprobó contra
    la original sobre cuarenta parches del conjunto de prueba y los vectores
    salieron idénticos bit a bit. La igualdad exacta no es un lujo acá, porque
    el tramo píxel → características se acredita por replicación y no por
    prueba: si el vector del sistema y el del auditor difirieran en el último
    bit, la comparación exigiría una tolerancia, y una tolerancia es una puerta.
    """

    model_version = VERSION_DEL_MOTOR_ONNX

    def __init__(self, artifacts_dir: str | None = None) -> None:
        self.artifacts_dir = artifacts_dir or os.environ.get("RUTA_ARTEFACTOS", "")
        if not self.artifacts_dir:
            raise ValueError("RUTA_ARTEFACTOS no configurada")
        self._sesiones: dict[str, object] = {}
        self._comprobar_los_artefactos()

    def _comprobar_los_artefactos(self) -> None:
        """Los artefactos están y son los declarados, o el servicio no arranca.

        Es la lección del despliegue del 22/08 aplicada a otro archivo: con
        `states.yaml` la aplicación levantaba, el healthcheck daba verde y el
        fallo aparecía recién al primer reclamo. Acá sería peor que un fallo,
        porque un extractor distinto no rompe nada: clasifica, y el sistema
        publica su hash como si fuera el declarado. Toda la replicación de
        RF-53 cuelga de que esos dos valores sean el mismo.
        """
        for nombre in (ARTEFACTO_DEL_EXTRACTOR, ARTEFACTO_DE_LA_CABEZA):
            if not os.path.exists(os.path.join(self.artifacts_dir, nombre)):
                raise FileNotFoundError(
                    f"Falta {nombre} en {self.artifacts_dir!r}. Sin los dos "
                    "artefactos el motor ONNX no puede correr; el simulado se "
                    "elige con MOTOR_INFERENCIA=stub y se declara como tal."
                )

        obtenido = self._hash_del_artefacto(ARTEFACTO_DEL_EXTRACTOR)
        if obtenido != HASH_DECLARADO_DEL_EXTRACTOR:
            raise ValueError(
                f"El extractor de {self.artifacts_dir!r} tiene sha256 {obtenido} "
                f"y el declarado es {HASH_DECLARADO_DEL_EXTRACTOR}. Publicar un "
                "vector bajo un hash que no lo produjo rompe la replicación."
            )

    def _sesion(self, nombre: str):
        """Sesión de ONNX Runtime, memorizada.

        Construirla relee y revalida el grafo, que con concurrencia 1 es un
        costo fijo pagado en cada reclamo sin necesidad.
        """
        import onnxruntime  # importación diferida, ver el docstring de la clase

        if nombre not in self._sesiones:
            opciones = onnxruntime.SessionOptions()
            opciones.intra_op_num_threads = 1
            opciones.inter_op_num_threads = 1
            opciones.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
            self._sesiones[nombre] = onnxruntime.InferenceSession(
                os.path.join(self.artifacts_dir, nombre),
                sess_options=opciones,
                providers=["CPUExecutionProvider"],
            )
        return self._sesiones[nombre]

    def _hash_del_artefacto(self, nombre: str) -> str:
        digesto = hashlib.sha256()
        with open(os.path.join(self.artifacts_dir, nombre), "rb") as archivo:
            for bloque in iter(lambda: archivo.read(65536), b""):
                digesto.update(bloque)
        return digesto.hexdigest()

    def artifacts_identity(self) -> str:
        import onnxruntime

        return ":".join((
            VERSION_DEL_MOTOR_ONNX, onnxruntime.__version__, "preprocesamiento-r224-v1",
            self._hash_del_artefacto(ARTEFACTO_DEL_EXTRACTOR),
            self._hash_del_artefacto(ARTEFACTO_DE_LA_CABEZA),
        ))

    def _preprocesar(self, imagen: bytes):
        """Bytes de la fotografía al tensor que el extractor espera.

        Cada paso replica el de la corrida que produjo el artefacto: apertura en
        RGB, redimensionado bilineal a 224 × 224, escala a [0, 1] dividiendo por
        255 y normalización con la media y el desvío de ImageNet.
        """
        import io

        import numpy
        from PIL import Image

        media = numpy.array(MEDIA_IMAGENET, dtype=numpy.float32).reshape(3, 1, 1)
        desvio = numpy.array(DESVIO_IMAGENET, dtype=numpy.float32).reshape(3, 1, 1)

        with Image.open(io.BytesIO(imagen)) as archivo:
            rgb = archivo.convert("RGB").resize(
                (RESOLUCION_DEL_EXTRACTOR, RESOLUCION_DEL_EXTRACTOR), Image.BILINEAR
            )
        pixeles = numpy.asarray(rgb, dtype=numpy.float32) / 255.0
        pixeles = numpy.transpose(pixeles, (2, 0, 1))
        return numpy.expand_dims((pixeles - media) / desvio, axis=0)

    def run(self, images: list[bytes]) -> InferenceResult:
        import numpy

        if not images:
            raise ValueError("No hay evidencia sobre la que inferir")

        # Una fotografía por reclamo. La agregación de las N que RF-02 admite
        # está decidida y sin ejecutar —una inferencia y una prueba por
        # fotografía, con el contrato agregando después por peor caso de
        # severidad— y su plan vive en `00-admin`. Promediar los vectores acá
        # sería inventar una regla de negocio que ningún requerimiento describe,
        # y quedaría escondida en un adaptador.
        extractor = self._sesion(ARTEFACTO_DEL_EXTRACTOR)
        cabeza = self._sesion(ARTEFACTO_DE_LA_CABEZA)

        entrada = self._preprocesar(images[0])
        caracteristicas = extractor.run(
            None, {extractor.get_inputs()[0].name: entrada}
        )[0].astype(numpy.float32)
        crudos = cabeza.run(None, {cabeza.get_inputs()[0].name: caracteristicas})[0]

        logits = numpy.asarray(crudos, dtype=numpy.float32).reshape(-1)
        if len(logits) != len(CLASES_DEL_MODELO):
            raise ValueError(
                f"La cabeza emitió {len(logits)} logits y el modelo declara "
                f"{len(CLASES_DEL_MODELO)} clases"
            )
        probabilidades = _softmax([float(valor) for valor in logits])
        ganadora = max(range(len(probabilidades)), key=probabilidades.__getitem__)

        return InferenceResult(
            feature_vector=caracteristicas.reshape(-1).tobytes(),
            logits=logits.tobytes(),
            predicted_class=CLASES_DEL_MODELO[ganadora],
            confidence=probabilidades[ganadora],
            extractor_artifact_hash=self._hash_del_artefacto(ARTEFACTO_DEL_EXTRACTOR),
            model_version=VERSION_DEL_MOTOR_ONNX,
        )


def build_inference_engine():
    """Elige la implementación por entorno. Sin `MOTOR_INFERENCIA=onnx` corre el
    stub, que es lo que mantiene la demo en pie mientras los artefactos no
    estén publicados."""
    if os.environ.get("MOTOR_INFERENCIA", "stub").lower() == "onnx":
        return OnnxInferenceEngine()
    return StubInferenceEngine()

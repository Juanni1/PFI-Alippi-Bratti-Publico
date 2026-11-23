"""Generador de pruebas de la cabeza.

Dos implementaciones. `EzklProofGenerator` corre EZKL sobre Halo2/KZG en BN254
con los artefactos del circuito adoptado: logrows 15, prueba de 100,6 KB y un
verificador de 13.389 B que entra en el límite de EIP-170.
`SimulatedProofGenerator` produce un artefacto con la forma correcta y sin
criptografía.

La distinción no se disimula: el simulado lleva `simulated: True` en sus
parámetros de circuito, y sin esa marca una traza producida así sería
indistinguible de una acreditada para quien lee la base.

Lo que la prueba acredita, y conviene tener a mano al leer este módulo, es el
tramo de las características a los logits y nada más. El tramo del píxel a las
características se acredita por replicación y queda fuera del circuito, que es
la frontera escalonada que el proyecto declara.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from array import array

from app.dominio.ports import InferenceResult, ProofResult

# Parámetros medidos de la configuración adoptada, para que el artefacto
# simulado declare contra qué circuito se lo compara cuando exista el real.
LOGROWS = 15
CURVA = "bn254"
ESQUEMA = "halo2-kzg"


class SimulatedProofGenerator:
    """Prueba simulada, determinista sobre la inferencia que acredita."""

    def artifacts_identity(self) -> str:
        return f"simulated:{LOGROWS}:{CURVA}:{ESQUEMA}:v1"

    def prove(self, inference: InferenceResult) -> ProofResult:
        material = inference.feature_vector + inference.logits
        digesto = hashlib.sha256(material).hexdigest()

        return ProofResult(
            proof=b"prueba-simulada:" + digesto.encode("ascii"),
            public_inputs=inference.feature_vector + inference.logits,
            proof_hash="0x" + digesto,
            circuit_parameters={
                "logrows": LOGROWS,
                "curve": CURVA,
                "scheme": ESQUEMA,
                # Marca explícita: sin esto una traza simulada sería
                # indistinguible de una real para quien lee la base.
                "simulated": True,
            },
            declared_visibility={
                # La frontera de confianza escalonada, declarada en el propio
                # artefacto: el vector de características es entrada PÚBLICA y
                # viaja en la calldata, y solo el tramo de las características a
                # los logits queda acreditado por prueba.
                "input": "public",
                "output": "public",
                "model": "fixed",
                "proven_segment": "features->logits",
                "replicated_segment": "pixel->features",
            },
            verified_locally=True,
        )


# Artefactos del circuito. Los produce `fase1/prove_head.py` en el repositorio
# de aprendizaje automático y son fijos: el setup se corre una vez por circuito,
# no una vez por reclamo. Lo que ocurre por reclamo es el testigo y la prueba.
CIRCUITO_COMPILADO = "model_r576.compiled"
PARAMETROS_ESTRUCTURADOS = "kzg_15.srs"
AJUSTES_DEL_CIRCUITO = "settings_r576.json"

# Los tres de arriba se distribuyen; los dos de abajo se derivan. La clave de
# generación pesa 133 MB y no entra en un repositorio, pero tampoco hace falta
# que entre: el setup de EZKL sobre el mismo circuito compilado y los mismos
# parámetros estructurados es **determinista**, comprobado corriéndolo dos
# veces y comparando los sha256 de las dos claves, y tarda 1,5 segundos.
#
# Derivarlas es además una propiedad más fuerte que distribuirlas. La clave de
# verificación es la que el verificador on-chain incorpora, y que se pueda
# re-derivar de artefactos públicos significa que nadie tiene que confiar en
# que la que se publicó es la que corresponde al circuito: la recalcula.
CLAVE_DE_GENERACION = "pk_r576.key"
CLAVE_DE_VERIFICACION = "vk_r576.key"

ARTEFACTOS_DISTRIBUIDOS = (
    CIRCUITO_COMPILADO,
    PARAMETROS_ESTRUCTURADOS,
    AJUSTES_DEL_CIRCUITO,
)
ARTEFACTOS_DERIVADOS = (CLAVE_DE_GENERACION, CLAVE_DE_VERIFICACION)
ARTEFACTOS_DEL_CIRCUITO = ARTEFACTOS_DISTRIBUIDOS + ARTEFACTOS_DERIVADOS

# sha256 de la clave de verificación que produce el setup determinista. Si la
# derivada no coincide, el circuito compilado o los parámetros no son los que
# el documento describe, y las pruebas que se generaran no serían las medidas.
HASH_DECLARADO_DE_LA_CLAVE_DE_VERIFICACION = (
    "f1321136e9c47a291dc440dadf803347"
)


class EzklProofGenerator:
    """Prueba real de la cabeza, con EZKL sobre Halo2/KZG en BN254.

    `ezkl` se importa dentro de los métodos por el mismo motivo que
    `onnxruntime` en el motor de inferencia: es pesado y solo hace falta cuando
    esta implementación corre de verdad.

    La entrada del circuito es el vector de características, y viaja como
    entrada **pública**. Eso sube el gas de la verificación y abre la superficie
    de privacidad que RNF-25 recoge; a cambio es lo que permite que un tercero
    compare el vector contra el que produce al reejecutar el extractor, y sin
    eso el tramo replicable no tendría contra qué contrastarse.
    """

    def __init__(self, artifacts_dir: str | None = None) -> None:
        self.artifacts_dir = artifacts_dir or os.environ.get("RUTA_ARTEFACTOS", "")
        if not self.artifacts_dir:
            raise ValueError("RUTA_ARTEFACTOS no configurada")
        self._comprobar_los_artefactos()

    def _ruta(self, nombre: str) -> str:
        return os.path.join(self.artifacts_dir, nombre)

    def _comprobar_los_artefactos(self) -> None:
        """Los tres artefactos distribuidos están y los dos derivados existen o
        se derivan, o el servicio no arranca.

        Que falten no se descubre bien al primer reclamo: el job fallaría, se
        reintentaría tres veces y el reclamo terminaría escalado por una causa
        que no es la suya.
        """
        faltantes = [
            nombre
            for nombre in ARTEFACTOS_DISTRIBUIDOS
            if not os.path.exists(self._ruta(nombre))
        ]
        if faltantes:
            raise FileNotFoundError(
                f"Faltan artefactos del circuito en {self.artifacts_dir!r}: "
                f"{', '.join(faltantes)}. El generador simulado se elige con "
                "GENERADOR_PRUEBAS=simulado y se declara como tal."
            )

        if not all(os.path.exists(self._ruta(n)) for n in ARTEFACTOS_DERIVADOS):
            self._derivar_las_claves()

        obtenido = self._hash_del_artefacto(CLAVE_DE_VERIFICACION)
        if obtenido != HASH_DECLARADO_DE_LA_CLAVE_DE_VERIFICACION:
            raise ValueError(
                f"La clave de verificación derivada tiene sha256 {obtenido} y la "
                f"declarada es {HASH_DECLARADO_DE_LA_CLAVE_DE_VERIFICACION}. El "
                "circuito compilado o los parámetros estructurados no son los "
                "que el documento describe."
            )

    def _derivar_las_claves(self) -> None:
        """Setup de EZKL sobre el circuito compilado. Determinista y de un par
        de segundos, y por eso se paga al arrancar en lugar de distribuir los
        133 MB de la clave de generación."""
        import ezkl

        os.environ.setdefault("HOME", os.path.expanduser("~"))
        ezkl.setup(
            self._ruta(CIRCUITO_COMPILADO),
            self._ruta(CLAVE_DE_VERIFICACION),
            self._ruta(CLAVE_DE_GENERACION),
            self._ruta(PARAMETROS_ESTRUCTURADOS),
        )
        for nombre in ARTEFACTOS_DERIVADOS:
            if not os.path.exists(self._ruta(nombre)):
                raise RuntimeError(f"El setup de EZKL no produjo {nombre}")

    def _hash_del_artefacto(self, nombre: str) -> str:
        """Los primeros 32 caracteres del sha256, que es la forma en que se
        anotó el valor declarado."""
        digesto = hashlib.sha256()
        with open(self._ruta(nombre), "rb") as archivo:
            for bloque in iter(lambda: archivo.read(1 << 20), b""):
                digesto.update(bloque)
        return digesto.hexdigest()[:32]

    def artifacts_identity(self) -> str:
        import ezkl

        return ":".join((
            str(ezkl.__version__),
            *(self._hash_del_artefacto(name) for name in ARTEFACTOS_DEL_CIRCUITO),
        ))

    def prove(self, inference: InferenceResult) -> ProofResult:
        import ezkl

        # EZKL (Rust) lee HOME sin tolerar su ausencia. En un contenedor puede
        # no estar definida, y el fallo aparecería recién al probar.
        os.environ.setdefault("HOME", os.path.expanduser("~"))

        caracteristicas = list(array("f", inference.feature_vector))

        with tempfile.TemporaryDirectory(prefix="prueba-") as trabajo:
            entrada = os.path.join(trabajo, "entrada.json")
            testigo = os.path.join(trabajo, "witness.json")
            prueba = os.path.join(trabajo, "proof.json")

            # Formato de EZKL: una fila por muestra.
            with open(entrada, "w") as archivo:
                json.dump({"input_data": [caracteristicas]}, archivo)

            ezkl.gen_witness(
                entrada,
                self._ruta(CIRCUITO_COMPILADO),
                testigo,
                vk_path=None,
                srs_path=self._ruta(PARAMETROS_ESTRUCTURADOS),
            )
            if not os.path.exists(testigo):
                raise RuntimeError("ezkl.gen_witness no generó el testigo")

            ezkl.prove(
                testigo,
                self._ruta(CIRCUITO_COMPILADO),
                self._ruta(CLAVE_DE_GENERACION),
                prueba,
                self._ruta(PARAMETROS_ESTRUCTURADOS),
            )
            if not os.path.exists(prueba):
                raise RuntimeError("ezkl.prove no generó la prueba")

            # Verificar del lado propio antes de mandar: gastar una transacción
            # en una prueba que ya se sabe inválida es el desperdicio que el
            # puerto pide evitar. Y una prueba que no verifica acá no se
            # devuelve marcada como buena, se devuelve marcada.
            verifica = bool(
                ezkl.verify(
                    prueba,
                    self._ruta(AJUSTES_DEL_CIRCUITO),
                    self._ruta(CLAVE_DE_VERIFICACION),
                    self._ruta(PARAMETROS_ESTRUCTURADOS),
                )
            )

            with open(prueba, "rb") as archivo:
                bytes_de_la_prueba = archivo.read()

        return ProofResult(
            proof=bytes_de_la_prueba,
            public_inputs=inference.feature_vector + inference.logits,
            proof_hash="0x" + hashlib.sha256(bytes_de_la_prueba).hexdigest(),
            circuit_parameters={
                "logrows": LOGROWS,
                "curve": CURVA,
                "scheme": ESQUEMA,
                # Sin marca de simulación: esta prueba es criptográfica y la
                # ausencia de la marca es lo que lo dice.
                "proof_bytes": len(bytes_de_la_prueba),
            },
            declared_visibility={
                "input": "public",
                "output": "public",
                "model": "fixed",
                "proven_segment": "features->logits",
                "replicated_segment": "pixel->features",
            },
            verified_locally=verifica,
        )


def build_proof_generator():
    """Elige la implementación por entorno.

    Sin `GENERADOR_PRUEBAS=ezkl` corre el simulado, que es lo que mantiene el
    sistema en pie donde los artefactos del circuito no estén. Elegir el real
    y que falten los artefactos es un error explícito y no una caída silenciosa
    al simulador: caer en silencio dejaría a alguien creyendo que hay
    criptografía donde no la hay.
    """
    if os.environ.get("GENERADOR_PRUEBAS", "simulado").lower() == "ezkl":
        return EzklProofGenerator()
    return SimulatedProofGenerator()

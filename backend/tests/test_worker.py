"""Worker: las cinco etapas, los reintentos y las dos liquidaciones.

Lo que más se cuida acá es que el sistema no invente números. Sin umbral
medido no clasifica automáticamente y sin umbral de liquidación medido no
liquida solo: en los dos casos deriva a una persona, que es la conducta
defendible cuando falta el dato.
"""

from decimal import Decimal

import pytest

from app.adaptadores.chain import SimulatedChainClient
from app.adaptadores.database import session_scope
from app.adaptadores.inference import StubInferenceEngine
from app.adaptadores.models import Claim, Inference, Job, Proof, Transition
from app.adaptadores.proving import SimulatedProofGenerator
from app.dominio import worker
from app.dominio.claims import UploadedFile, current_configuration, register_claim
from app.dominio.vocabulary import (
    ClaimStatus,
    JobFailureReason,
    JobStatus,
    PolicyMode,
    TransitionCause,
)
from tests.conftest import DAMAGE_DESCRIPTION, PIXEL_PNG, PublicadorQueFalla


def _registrar(sesion, publicador, monto="185000.00"):
    return register_claim(
        sesion,
        publicador,
        damage_description=DAMAGE_DESCRIPTION,
        files=[UploadedFile("frente.png", "image/png", PIXEL_PNG)],
        declared_amount=Decimal(monto),
    )


def _correr(factory, publicador, *, umbral=0.70, modo=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO,
            verifica=True):
    return worker.run_next(
        factory,
        publisher=publicador,
        engine=StubInferenceEngine(),
        prover=SimulatedProofGenerator(),
        chain=SimulatedChainClient(
            derivation_threshold=umbral, policy_mode=modo, verifier_accepts=verifica
        ),
    )


# ---------------------------------------------------------------------------
# Camino completo
# ---------------------------------------------------------------------------


def test_el_worker_recorre_las_cinco_etapas_y_deja_todo_asentado(sesiones, publicador):
    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)

    with session_scope(sesiones) as sesion:
        procesado = _correr(sesiones, publicador)
        assert procesado is not None

    with session_scope(sesiones) as sesion:
        inferencia = sesion.query(Inference).one()
        prueba = sesion.query(Proof).one()
        job = sesion.query(Job).one()

        # El vector y el hash del artefacto viajan juntos: es lo que vuelve
        # ejecutable la replicación del extractor.
        assert len(worker.decode_vector(inferencia.feature_vector)) == 576
        assert inferencia.extractor_artifact_hash
        assert prueba.verified_locally is True
        assert prueba.inference_id == inferencia.id
        assert job.status is JobStatus.COMPLETADO
        assert job.finished_at is not None


def test_la_prueba_simulada_se_declara_como_tal(sesiones, publicador):
    """Sin esta marca, una traza simulada sería indistinguible de una real para
    quien lee la base."""
    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
    with session_scope(sesiones) as sesion:
        _correr(sesiones, publicador)
    with session_scope(sesiones) as sesion:
        prueba = sesion.query(Proof).one()
        assert prueba.circuit_parameters["simulated"] is True
        assert prueba.declared_visibility["proven_segment"] == "features->logits"


def test_la_transicion_queda_sin_hash_de_transaccion(sesiones, publicador):
    """Con la cadena simulada no hay respaldo on-chain, y el nulo es la marca de
    eso. Rellenarlo con un valor plausible sería peor que dejarlo vacío."""
    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
    with session_scope(sesiones) as sesion:
        _correr(sesiones, publicador)
    with session_scope(sesiones) as sesion:
        clasificacion = (
            sesion.query(Transition).filter(Transition.cause.isnot(None)).one()
        )
        assert clasificacion.transaction_hash is None
        assert clasificacion.block_number is None
        assert clasificacion.proof_hash is not None


# ---------------------------------------------------------------------------
# Umbrales sin medir
# ---------------------------------------------------------------------------


def test_sin_umbral_medido_no_clasifica_solo_y_deriva(sesiones, publicador):
    """Es la conducta defendible cuando falta el dato: comparar contra un
    número inventado resolvería reclamos sobre un umbral que nadie midió."""
    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
    with session_scope(sesiones) as sesion:
        procesado = _correr(sesiones, publicador, umbral=None)
        assert procesado.status is ClaimStatus.REQUIERE_REVISION
        assert procesado.cause is TransitionCause.CONFIANZA_BAJO_UMBRAL


def test_con_umbral_medido_y_confianza_alta_queda_aprobable(sesiones, publicador):
    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
    with session_scope(sesiones) as sesion:
        procesado = _correr(sesiones, publicador, umbral=0.10)
        assert procesado.status is ClaimStatus.APROBABLE
        assert procesado.cause is TransitionCause.CLASIFICACION_FAVORABLE


def test_el_modo_de_confirmacion_humana_vacia_el_camino_automatico(sesiones, publicador):
    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
    with session_scope(sesiones) as sesion:
        procesado = _correr(
            sesiones,
            publicador,
            umbral=0.10,
            modo=PolicyMode.CONFIRMACION_HUMANA_OBLIGATORIA,
        )
        assert procesado.status is ClaimStatus.REQUIERE_REVISION
        assert procesado.cause is TransitionCause.POLITICA_CONFIRMACION_HUMANA


# ---------------------------------------------------------------------------
# RNF-17 — prueba que no verifica
# ---------------------------------------------------------------------------


def test_una_prueba_que_no_verifica_asienta_el_episodio_y_no_revienta(
    sesiones, publicador
):
    """RNF-17: el reclamo queda en VerificacionFallida y la operación NO falla.
    Revertir borraría el episodio, y el episodio es lo que sostiene el
    argumento de auditabilidad."""
    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
    with session_scope(sesiones) as sesion:
        procesado = _correr(sesiones, publicador, verifica=False)
        assert procesado is not None
        assert procesado.status is ClaimStatus.VERIFICACION_FALLIDA
        assert procesado.cause is TransitionCause.FALLO_DE_VERIFICACION

    with session_scope(sesiones) as sesion:
        assert sesion.query(Job).one().status is JobStatus.COMPLETADO
        assert sesion.query(Proof).count() == 1


# ---------------------------------------------------------------------------
# Fallos de etapa y reintentos (RF-13, RNF-18)
# ---------------------------------------------------------------------------


def test_la_evidencia_no_recuperable_devuelve_el_job_a_la_cola(sesiones, publicador):
    """RNF-18: el trabajo vuelve a la cola conservando su lugar."""
    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
        job = sesion.query(Job).one()
        job.max_attempts = 3

    with session_scope(sesiones) as sesion:
        resultado = _correr(sesiones, PublicadorQueFalla())
        assert resultado is None

    with session_scope(sesiones) as sesion:
        job = sesion.query(Job).one()
        assert job.status is JobStatus.PENDIENTE
        assert job.attempts == 1
        assert job.last_failure_cause is JobFailureReason.EVIDENCIA_NO_RECUPERADA
        assert job.started_at is None


def test_agotados_los_intentos_el_job_queda_fallido(sesiones, publicador):
    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
        sesion.query(Job).one().max_attempts = 1

    with session_scope(sesiones) as sesion:
        _correr(sesiones, PublicadorQueFalla())

    with session_scope(sesiones) as sesion:
        job = sesion.query(Job).one()
        assert job.status is JobStatus.FALLIDO
        assert job.finished_at is not None
        # El reclamo no avanzó: sigue donde estaba.
        assert sesion.query(Claim).one().status is ClaimStatus.REGISTRADO


def test_la_cola_vacia_no_hace_nada(sesiones, publicador):
    with session_scope(sesiones) as sesion:
        assert _correr(sesiones, publicador) is None


# ---------------------------------------------------------------------------
# Liquidación automática (adr-007)
# ---------------------------------------------------------------------------


def test_sin_umbral_de_liquidacion_medido_no_liquida_solo(sesiones, publicador):
    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
    with session_scope(sesiones) as sesion:
        procesado = _correr(sesiones, publicador, umbral=0.10)
        # Aprobable y ahí se queda: falta que una persona registre la
        # liquidación, porque el umbral que la habilitaría no está medido.
        assert procesado.status is ClaimStatus.APROBABLE


def test_con_umbral_de_liquidacion_medido_cierra_solo_por_el_camino_automatico(
    sesiones, publicador
):
    """adr-007: la causa distingue este cierre del que registra el operador, y
    de esa distinción dependen los textos de MK-03, MK-04 y MK-08."""
    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
        configuracion = current_configuration(sesion)
        configuracion.derivation_threshold = 0.10
        configuracion.auto_settlement_threshold = 0.10

    with session_scope(sesiones) as sesion:
        procesado = _correr(sesiones, publicador, umbral=0.10)
        assert procesado.status is ClaimStatus.APROBADO
        assert procesado.cause is TransitionCause.LIQUIDACION_AUTOMATICA

    with session_scope(sesiones) as sesion:
        causas = [t.cause for t in sesion.query(Transition).all()]
        assert TransitionCause.LIQUIDACION_AUTOMATICA in causas
        assert TransitionCause.LIQUIDACION_DEL_OPERADOR not in causas


def test_la_confirmacion_humana_obligatoria_desactiva_la_liquidacion_automatica(
    sesiones, publicador
):
    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
        configuracion = current_configuration(sesion)
        configuracion.derivation_threshold = 0.10
        configuracion.auto_settlement_threshold = 0.10
        configuracion.policy_mode = PolicyMode.CONFIRMACION_HUMANA_OBLIGATORIA

    with session_scope(sesiones) as sesion:
        procesado = _correr(
            sesiones, publicador, umbral=0.10, modo=PolicyMode.CONFIRMACION_HUMANA_OBLIGATORIA
        )
        assert procesado.status is ClaimStatus.REQUIERE_REVISION


# ---------------------------------------------------------------------------
# Disparo desde la API
# ---------------------------------------------------------------------------


def test_registrar_dispara_el_procesamiento_y_la_explicacion_queda_disponible(
    cliente, archivos, sesiones
):
    """De punta a punta por HTTP: se carga la evidencia y, sin ninguna otra
    llamada, el reclamo queda clasificado y con su explicación."""
    respuesta = cliente.post(
        "/reclamos",
        files=archivos,
        data={
            "declaredAmount": "185000",
            "damageDescription": DAMAGE_DESCRIPTION,
        },
    )
    assert respuesta.status_code == 201
    numero = respuesta.json()["number"]
    token = respuesta.json()["accessToken"]

    with session_scope(sesiones) as sesion:
        assert sesion.query(Inference).count() == 1
        assert sesion.query(Job).one().status is JobStatus.COMPLETADO

    explicacion = cliente.get(
        f"/reclamos/{numero}/explicacion", headers={"Authorization": f"Bearer {token}"}
    )
    assert explicacion.status_code == 200
    cuerpo = explicacion.json()
    assert cuerpo["evidence"]["publishedFeatureVector"]["dimension"] == 576
    assert cuerpo["evidence"]["publishedFeatureVector"]["published"] is True
    assert cuerpo["evidence"]["publishedFeatureVector"]["notice"] == (
        "Las fotos quedan accesibles por su identificador en IPFS. "
        "El análisis utiliza un resumen numérico calculado a partir de ellas."
    )
    assert cuerpo["proof"]["provenSegment"]
    assert cuerpo["model"]["predictedClass"]


def test_la_traza_publica_publica_el_vector_completo(cliente, archivos):
    """RF-53: el tercero reejecuta el extractor y compara contra este vector;
    sin él no tiene contra qué comparar."""
    numero = cliente.post(
        "/reclamos",
        files=archivos,
        data={
            "declaredAmount": "185000",
            "damageDescription": DAMAGE_DESCRIPTION,
        },
    ).json()["number"]

    traza = cliente.get(f"/publico/reclamos/{numero}/traza")
    assert traza.status_code == 200
    cuerpo = traza.json()
    assert len(cuerpo["featureVector"]) == 576
    assert cuerpo["artifacts"]["proof"]["proofHash"]
    # Esta corrida usa el motor simulado, y el prefijo lo dice: un auditor que
    # lea `stub:` sabe que no hay artefacto que replicar, en lugar de salir a
    # buscar uno que no existe.
    replicacion = cuerpo["artifacts"]["replication"]
    assert replicacion["extractorOnnxHash"].startswith("stub:")
    assert replicacion["extractorOnnxUrl"] is None
    assert replicacion["preprocessingSpecUrl"] is None
    # Y la prueba de esta corrida lleva su marca de simulación, que es lo que
    # impide leer la traza como acreditada.
    assert cuerpo["artifacts"]["proof"]["circuitParameters"]["simulated"] is True
    # Y sin las celdas que la lista cerrada de RF-44 no expone.
    assert "declaredAmount" not in cuerpo
    assert "damageDescription" not in cuerpo
    assert DAMAGE_DESCRIPTION not in traza.text
    assert "confidence" not in cuerpo


def test_la_traza_publica_expone_las_urls_publicadas_para_el_extractor_real(
    cliente, archivos, sesiones
):
    """PFI-95 / RF-53: cuando el reclamo corrió con el extractor ONNX publicado,
    la traza expone las URL de descarga del modelo y de su manifiesto."""
    from app.adaptadores.inference import (
        HASH_DECLARADO_DEL_EXTRACTOR,
        URL_PUBLICADA_DEL_EXTRACTOR,
        URL_PUBLICADA_DEL_PREPROCESAMIENTO,
        VERSION_DEL_MOTOR_ONNX,
    )
    from app.adaptadores.models import Model

    numero = cliente.post(
        "/reclamos",
        files=archivos,
        data={
            "declaredAmount": "185000",
            "damageDescription": DAMAGE_DESCRIPTION,
        },
    ).json()["number"]

    with session_scope(sesiones) as sesion:
        reclamo = sesion.query(Claim).filter_by(tracking_number=numero).one()
        inferencia = sesion.query(Inference).filter_by(claim_id=reclamo.id).one()
        modelo = sesion.get(Model, inferencia.model_id)
        modelo.extractor_sha256 = HASH_DECLARADO_DEL_EXTRACTOR
        modelo.version = VERSION_DEL_MOTOR_ONNX

    traza = cliente.get(f"/publico/reclamos/{numero}/traza")
    assert traza.status_code == 200
    replicacion = traza.json()["artifacts"]["replication"]
    assert replicacion["extractorOnnxHash"] == HASH_DECLARADO_DEL_EXTRACTOR
    assert replicacion["extractorOnnxUrl"] == URL_PUBLICADA_DEL_EXTRACTOR
    assert replicacion["preprocessingSpecUrl"] == URL_PUBLICADA_DEL_PREPROCESAMIENTO
    assert replicacion["runtimeVersion"] == VERSION_DEL_MOTOR_ONNX


# ---------------------------------------------------------------------------
# Calibración del motor simulado
# ---------------------------------------------------------------------------


def test_los_logits_del_stub_son_coherentes_con_la_confianza():
    """La confianza tiene que ser el máximo del softmax de los logits que el
    mismo resultado transporta. Si no lo fuera, la explicación de MK-04 y la
    traza pública contarían cosas distintas sobre el mismo reclamo."""
    import math
    from array import array

    from app.adaptadores.inference import StubInferenceEngine

    resultado = StubInferenceEngine().run([b"una foto cualquiera"])
    logits = array("f", resultado.logits).tolist()
    mayor = max(logits)
    exponenciales = [math.exp(valor - mayor) for valor in logits]
    softmax = [e / sum(exponenciales) for e in exponenciales]
    assert abs(max(softmax) - resultado.confidence) < 1e-5


def test_el_stub_reparte_los_casos_en_las_proporciones_medidas():
    """La distribución de confianza del stub imita a la del clasificador real,
    medida en adr-014. Sin esa calibración el stub superaba el umbral de
    derivación en el 1,2 % de los casos y ningún recorrido llegaba nunca a los
    otros dos tramos del diseño."""
    from app.adaptadores.inference import StubInferenceEngine

    motor = StubInferenceEngine()
    confianzas = [motor.run([f"muestra-{i}".encode()]).confidence for i in range(2000)]
    total = len(confianzas)

    deriva = sum(c < 0.90 for c in confianzas) / total
    aprobable = sum(0.90 <= c < 0.95 for c in confianzas) / total
    automatica = sum(c >= 0.95 for c in confianzas) / total

    # Tolerancia de tres puntos sobre las proporciones de la curva medida.
    assert abs(deriva - 0.524) < 0.03
    assert abs(aprobable - 0.335) < 0.03
    assert abs(automatica - 0.141) < 0.03


# ---------------------------------------------------------------------------
# RF-10 — recuperar por identificador y verificar el digesto
# ---------------------------------------------------------------------------


class PublicadorQueDevuelveOtraCosa:
    """Devuelve contenido distinto del publicado, con el mismo identificador.

    En un almacén direccionado por contenido esto no debería poder ocurrir, y
    ese es justamente el punto: si ocurre, el identificador dejó de garantizar
    lo que dice garantizar y el sistema tiene que notarlo.
    """

    def __init__(self) -> None:
        self.contenido: dict[str, bytes] = {}

    def publish(self, content: bytes, filename: str = "evidencia") -> str:
        import hashlib

        cid = "bafkrei" + hashlib.sha256(content).hexdigest()[:52]
        self.contenido[cid] = content
        return cid

    def retrieve(self, cid: str) -> bytes:
        return b"contenido adulterado que no es el que se publico"


def test_el_worker_verifica_el_digesto_de_lo_recuperado(sesiones, publicador):
    """RF-10: recuperar por el identificador y comprobar que coincide."""
    from app.adaptadores.models import Evidence

    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
    with session_scope(sesiones) as sesion:
        _correr(sesiones, publicador)

    with session_scope(sesiones) as sesion:
        assert sesion.query(Evidence).one().digest_verified is True


def test_un_contenido_que_no_coincide_detiene_el_procesamiento(sesiones):
    """La inferencia no corre sobre contenido que no es el que se recibió. El
    reclamo no avanza y el job vuelve a la cola con su causa."""
    from app.adaptadores.models import Claim, Evidence, Inference, Job

    publicador = PublicadorQueDevuelveOtraCosa()
    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
        sesion.query(Job).one().max_attempts = 2

    with session_scope(sesiones) as sesion:
        assert _correr(sesiones, publicador) is None

    with session_scope(sesiones) as sesion:
        assert sesion.query(Evidence).one().digest_verified is False
        # No se produjo ninguna inferencia sobre contenido no verificado.
        assert sesion.query(Inference).count() == 0
        assert sesion.query(Claim).one().status is ClaimStatus.REGISTRADO
        job = sesion.query(Job).one()
        assert job.status is JobStatus.PENDIENTE
        assert job.last_failure_cause is JobFailureReason.EVIDENCIA_NO_RECUPERADA


def test_la_traza_publica_informa_el_resultado_de_la_verificacion(cliente, archivos):
    """RF-10, mitad de CU-08: el auditor externo lee qué encontró el sistema al
    recuperar la evidencia y comparar."""
    numero = cliente.post(
        "/reclamos",
        files=archivos,
        data={
            "declaredAmount": "185000",
            "damageDescription": DAMAGE_DESCRIPTION,
        },
    ).json()["number"]

    traza = cliente.get(f"/publico/reclamos/{numero}/traza").json()
    assert traza["evidence"]["digestMatches"] is True


def test_sin_recuperacion_todavia_el_resultado_viaja_nulo(sesiones, publicador, cliente):
    """Nulo no es lo mismo que falso: significa que nadie la recuperó todavía,
    y confundirlos le haría creer al auditor que hubo una comprobación."""
    from app.adaptadores.models import Evidence

    with session_scope(sesiones) as sesion:
        _registrar(sesion, publicador)
        assert sesion.query(Evidence).one().digest_verified is None

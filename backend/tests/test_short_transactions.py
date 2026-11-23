"""Avances visibles y filas libres durante cómputo externo."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest

from sqlalchemy import text

from app.adaptadores.chain import SimulatedChainClient
from app.adaptadores.database import session_scope
from app.adaptadores.inference import StubInferenceEngine
from app.adaptadores.models import Inference, Job
from app.adaptadores.proving import SimulatedProofGenerator
from app.dominio import worker
from app.dominio.vocabulary import DamageClass, JobStatus, PolicyMode


@pytest.mark.parametrize("blocked_stage", ["prover", "submission"])
@pytest.mark.parametrize("verifier_accepts", [True, False])
def test_acquisition_and_inference_are_durable_while_proving(
    blocked_stage, verifier_accepts, cliente, sesiones, publicador, reclamo_registrado
):
    entered, release = Event(), Event()

    class BlockingProver(SimulatedProofGenerator):
        def prove(self, inference):
            if blocked_stage == "prover":
                entered.set()
                assert release.wait(10)
            return super().prove(inference)

    class BlockingChain(SimulatedChainClient):
        def submit_inference(self, *args, **kwargs):
            if blocked_stage == "submission":
                entered.set()
                assert release.wait(10)
            return super().submit_inference(*args, **kwargs)

    def execute():
        return worker.run_next(
            sesiones, publisher=publicador, engine=StubInferenceEngine(),
            prover=BlockingProver(), chain=BlockingChain(
                derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO,
                verifier_accepts=verifier_accepts,
            ),
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute)
        try:
            if not entered.wait(10):
                future.result(timeout=1)
                raise AssertionError("la etapa de prueba no comenzó")
            with session_scope(sesiones) as session:
                job = session.query(Job).one()
                assert job.status is JobStatus.EN_CURSO
                assert job.attempts == 1
                assert session.query(Inference).one().claim_id == reclamo_registrado['id']
                session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                session.execute(text('SELECT id FROM jobs FOR UPDATE'))
            path = f'/reclamos/{reclamo_registrado["number"]}'
            for token in [reclamo_registrado["token"], "revisor-rev-04", "operador-op-02"]:
                headers = {"Authorization": f"Bearer {token}"}
                response = cliente.get(path, headers=headers)
                assert response.status_code == 200
                assert response.json()["resolution"] is None
                if token == reclamo_registrado["token"]:
                    assert response.json()["explanationAvailable"] is False
                else:
                    assert response.json()["inference"] is None
                if token != "operador-op-02":
                    explanation = cliente.get(path + "/explicacion", headers=headers)
                    assert explanation.status_code == 409
                    assert "model" not in explanation.json()
            assert cliente.get(f'/publico{path}/traza').status_code == 409
        finally:
            release.set()
        assert future.result(timeout=10) is not None
    headers = {"Authorization": f'Bearer {reclamo_registrado["token"]}'}
    assert cliente.get(path, headers=headers).json()["explanationAvailable"] is True
    explanation = cliente.get(path + "/explicacion", headers=headers)
    assert explanation.status_code == 200
    assert explanation.json()["model"]["accredited"] is verifier_accepts
    assert explanation.json()["proof"]["verificationFailed"] is not verifier_accepts
    trace = cliente.get(f'/publico{path}/traza')
    assert trace.status_code == 200
    with session_scope(sesiones) as session:
        job = session.query(Job).one()
        from app.adaptadores.models import Proof
        assert trace.json()["artifacts"]["proof"]["proofHash"] == session.get(Proof, job.proof_id).proof_hash


def test_generation_retry_keeps_inference_and_configuration(sesiones, publicador, reclamo_registrado):

    class FailingProver(SimulatedProofGenerator):
        def prove(self, inference):
            raise MemoryError("fallo antes de producir prueba")

    chain = SimulatedChainClient(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
    assert worker.run_next(sesiones, publisher=publicador, engine=StubInferenceEngine(),
                           prover=FailingProver(), chain=chain) is None
    with session_scope(sesiones) as session:
        job = session.query(Job).one()
        inference_id, configuration_id = job.inference_id, job.configuration_id
        assert session.get(Inference, inference_id).claim_id == reclamo_registrado["id"]
        assert job.status == JobStatus.PENDIENTE
        assert job.current_stage == "generacion"
        original_enqueue = job.enqueued_at
    result = worker.run_next(sesiones, publisher=publicador, engine=StubInferenceEngine(),
                             prover=SimulatedProofGenerator(), chain=chain)
    assert result is not None
    with session_scope(sesiones) as session:
        job = session.query(Job).one()
        assert job.inference_id == inference_id
        assert job.configuration_id == configuration_id
        assert job.enqueued_at == original_enqueue
        assert job.attempts == 2
        assert session.query(Inference).count() == 1


def test_failed_auto_settlement_preserves_classification_and_blocks_replay(
    sesiones, publicador, reclamo_registrado
):
    from app.adaptadores.models import Claim, Transition
    from app.dominio.claims import current_configuration
    from app.dominio.vocabulary import ClaimStatus, TransitionCause

    with session_scope(sesiones) as session:
        configuration = current_configuration(session)
        configuration.auto_settlement_threshold = configuration.derivation_threshold = 0.1

    class UncertainChain(SimulatedChainClient):
        def settle(self, number, *, automatic):
            raise TimeoutError("el resultado del envío aún no se conoce")

    chain = UncertainChain(derivation_threshold=0.1, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
    with pytest.raises(TimeoutError):
        worker.run_next(sesiones, publisher=publicador, engine=StubInferenceEngine(),
                        prover=SimulatedProofGenerator(), chain=chain)
    with session_scope(sesiones) as session:
        claim, job = session.query(Claim).one(), session.query(Job).one()
        assert claim.status == ClaimStatus.APROBABLE
        assert claim.pending_chain_action == "settle"
        assert job.status == JobStatus.EN_CURSO
        assert job.current_stage == "liquidacion"
        assert session.query(Transition).filter(
            Transition.cause == TransitionCause.CLASIFICACION_FAVORABLE
        ).one().to_status == ClaimStatus.APROBABLE
    assert worker.run_next(sesiones, publisher=publicador, engine=StubInferenceEngine(),
                           prover=SimulatedProofGenerator(), chain=chain) is None
    with session_scope(sesiones) as session:
        assert session.query(Job).one().attempts == 1


@pytest.mark.parametrize("operation", ["resolve", "settle"])
def test_human_rpc_keeps_rows_free_and_rejects_overlapping_action(
    operation, cliente, sesiones, reclamo_registrado, monkeypatch
):
    from app.adaptadores.models import Claim, Review
    from app.dominio.vocabulary import ClaimStatus
    from app.exposicion import api

    entered, release = Event(), Event()

    class BlockingChain(SimulatedChainClient):
        def resolve(self, number, *, favorable, decision_hash):
            entered.set()
            assert release.wait(10)
            return super().resolve(number, favorable=favorable, decision_hash=decision_hash)

        def settle(self, number, *, automatic):
            entered.set()
            assert release.wait(10)
            return super().settle(number, automatic=automatic)

    chain = BlockingChain(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
    monkeypatch.setattr(api, "_cliente_de_cadena", lambda session: chain)
    initial_status = ClaimStatus.REQUIERE_REVISION if operation == "resolve" else ClaimStatus.APROBABLE
    with session_scope(sesiones) as session:
        session.get(Claim, reclamo_registrado["id"]).status = initial_status
    route = "resolucion" if operation == "resolve" else "liquidacion"
    token = "revisor-rev-04" if operation == "resolve" else "operador-op-02"
    kwargs = {"headers": {"Authorization": f"Bearer {token}"}}
    if operation == "resolve":
        kwargs["json"] = {"favorable": True, "justification": "Se verificó la evidencia."}
    url = f'/reclamos/{reclamo_registrado["number"]}/{route}'
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cliente.post, url, **kwargs)
        try:
            if not entered.wait(10):
                future.result(timeout=1)
                raise AssertionError("el RPC no comenzó")
            with session_scope(sesiones) as session:
                claim = session.get(Claim, reclamo_registrado["id"])
                assert claim.status == initial_status
                assert claim.pending_chain_action == operation
                session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                session.execute(text("SELECT id FROM claims FOR UPDATE"))
                if operation == "resolve":
                    review = session.query(Review).one()
                    assert review.transition_id is None
                    assert review.reviewer_identity == "rev-04"
            assert cliente.post(url, **kwargs).status_code == 409
            path = f'/reclamos/{reclamo_registrado["number"]}'
            for reader in [reclamo_registrado["token"], "revisor-rev-04", "operador-op-02"]:
                response = cliente.get(path, headers={"Authorization": f"Bearer {reader}"})
                assert response.status_code == 200
                assert response.json()["resolution"] is None
                if reader != reclamo_registrado["token"]:
                    assert response.json()["statusId"] == initial_status.value
                    assert response.json()["inference"] is None
        finally:
            release.set()
        response = future.result(timeout=10)
        assert response.status_code == 200
        assert response.json()["statusId"] == "Aprobado"
    with session_scope(sesiones) as session:
        claim = session.get(Claim, reclamo_registrado["id"])
        assert claim.pending_chain_action is None
        assert claim.status == ClaimStatus.APROBADO
        if operation == "resolve":
            assert session.query(Review).one().transition_id is not None
    for reader in [reclamo_registrado["token"], "revisor-rev-04", "operador-op-02"]:
        payload = cliente.get(path, headers={"Authorization": f"Bearer {reader}"}).json()
        if operation == "resolve":
            assert payload["resolution"]["justification"] == "Se verificó la evidencia."
            if reader != reclamo_registrado["token"]:
                assert payload["resolution"]["reviewer"] == "rev-04"
        else:
            assert payload["resolution"] is None


def test_resolution_crash_before_rpc_keeps_prepared_intent_and_recovers(
    cliente, sesiones, reclamo_registrado, monkeypatch
):
    """A decision and its chain intent survive the same crash boundary."""
    from app.adaptadores.oracle_coordinator import OracleCoordinator
    from app.dominio.vocabulary import ClaimStatus, PolicyMode
    from app.adaptadores.models import Claim
    from app.adaptadores.chain import SimulatedChainClient

    with session_scope(sesiones) as session:
        session.get(Claim, reclamo_registrado["id"]).status = ClaimStatus.REQUIERE_REVISION

    original_resolve = OracleCoordinator.resolve

    def interrupted_before_rpc(self, *args, **kwargs):
        raise RuntimeError("injected crash before oracle RPC")

    monkeypatch.setattr(OracleCoordinator, "resolve", interrupted_before_rpc)
    url = f'/reclamos/{reclamo_registrado["number"]}/resolucion'
    with pytest.raises(RuntimeError, match="injected crash before oracle RPC"):
        cliente.post(url, headers={"Authorization": "Bearer revisor-rev-04"},
                     json={"favorable": False, "justification": "Daño no cubierto."})

    queue = cliente.get("/cola", headers={"Authorization": "Bearer operador-op-02"})
    assert queue.status_code == 200
    assert queue.json()["oracleOperations"][0]["status"] == "preparada"
    assert queue.json()["oracleBlocked"] is True

    monkeypatch.setattr(OracleCoordinator, "resolve", original_resolve)
    chain = SimulatedChainClient(
        derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO
    )
    recovered = OracleCoordinator(sesiones, chain).reconcile_pending(max_operations=50)
    assert len(recovered) == 1
    assert recovered[0].status == "confirmada"
    response = cliente.get(
        f'/reclamos/{reclamo_registrado["number"]}',
        headers={"Authorization": "Bearer revisor-rev-04"},
    )
    assert response.status_code == 200
    assert response.json()["statusId"] == ClaimStatus.RECHAZADO.value
    assert response.json()["resolution"] is not None


def test_manual_settlement_crash_preserves_one_recoverable_intent(
    cliente, sesiones, reclamo_registrado, monkeypatch
):
    """A settlement reservation is never orphaned between commit and RPC."""
    from app.adaptadores.oracle_coordinator import OracleCoordinator
    from app.adaptadores.chain import SimulatedChainClient
    from app.adaptadores.models import Claim
    from app.dominio.vocabulary import ClaimStatus, PolicyMode

    with session_scope(sesiones) as session:
        session.get(Claim, reclamo_registrado["id"]).status = ClaimStatus.APROBABLE
    original_settle = OracleCoordinator.settle

    def interrupted_before_rpc(self, *args, **kwargs):
        raise RuntimeError("injected settlement crash before RPC")

    monkeypatch.setattr(OracleCoordinator, "settle", interrupted_before_rpc)
    url = f'/reclamos/{reclamo_registrado["number"]}/liquidacion'
    with pytest.raises(RuntimeError, match="injected settlement crash before RPC"):
        cliente.post(url, headers={"Authorization": "Bearer operador-op-02"})

    queue = cliente.get("/cola", headers={"Authorization": "Bearer operador-op-02"})
    assert queue.status_code == 200
    assert queue.json()["oracleBlocked"] is True
    assert [(op["operation"], op["status"]) for op in queue.json()["oracleOperations"]] == [
        ("settle", "preparada")
    ]
    monkeypatch.setattr(OracleCoordinator, "settle", original_settle)
    chain = SimulatedChainClient(
        derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO
    )
    recovered = OracleCoordinator(sesiones, chain).reconcile_pending(max_operations=50)
    assert len(recovered) == 1
    assert recovered[0].status == "confirmada"
    response = cliente.get(
        f'/reclamos/{reclamo_registrado["number"]}',
        headers={"Authorization": "Bearer operador-op-02"},
    )
    assert response.status_code == 200
    assert response.json()["statusId"] == ClaimStatus.APROBADO.value


def test_failed_resolution_keeps_private_decision_after_http_rollback(
    cliente, sesiones, reclamo_registrado, monkeypatch
):
    from app.adaptadores.models import Claim, Review
    from app.dominio.vocabulary import ClaimStatus
    from app.exposicion import api

    class UncertainChain(SimulatedChainClient):
        def resolve(self, number, *, favorable, decision_hash):
            raise TimeoutError("resultado incierto")

    monkeypatch.setattr(api, "_cliente_de_cadena", lambda session: UncertainChain(
        derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO
    ))
    with session_scope(sesiones) as session:
        session.get(Claim, reclamo_registrado["id"]).status = ClaimStatus.REQUIERE_REVISION
    url = f'/reclamos/{reclamo_registrado["number"]}/resolucion'
    kwargs = {"headers": {"Authorization": "Bearer revisor-rev-04"},
              "json": {"favorable": False, "justification": "Daño no cubierto."}}
    response = cliente.post(url, **kwargs)
    assert response.status_code == 409
    assert response.json()["title"] == "Operación del oráculo incierta."
    with session_scope(sesiones) as session:
        claim, review = session.query(Claim).one(), session.query(Review).one()
        assert claim.pending_chain_action == "resolve"
        assert claim.status == ClaimStatus.REQUIERE_REVISION
        assert review.justification == "Daño no cubierto."
        assert review.transition_id is None
    assert cliente.post(url, **kwargs).status_code == 409
    for reader in [reclamo_registrado["token"], "revisor-rev-04", "operador-op-02"]:
        payload = cliente.get(
            f'/reclamos/{reclamo_registrado["number"]}',
            headers={"Authorization": f"Bearer {reader}"},
        ).json()
        assert payload["resolution"] is None


@pytest.mark.parametrize("legacy", [False, True])
def test_confirmed_projection_ignores_unlinked_history(
    legacy, cliente, sesiones, publicador, reclamo_registrado
):
    from array import array
    from app.adaptadores.models import Model, Proof

    with session_scope(sesiones) as session:
        model = Model(version="historical", extractor_path="simulated", head_path="simulated",
                      extractor_sha256="0" * 64, head_sha256="0" * 64, onnx_opset=17,
                      engine_version="simulated", current=False)
        session.add(model)
        session.flush()
        historical = Inference(
            claim_id=reclamo_registrado["id"], model_id=model.id,
            feature_vector=array("f", [7.0] * 576).tobytes(),
            extractor_artifact_hash=model.extractor_sha256,
            logits=array("f", [7.0] * 6).tobytes(),
            predicted_class=DamageClass.FISURA,
            confidence=0.01,
        )
        session.add(historical)
        session.flush()
        session.add(Proof(inference_id=historical.id, proof=b"historical",
                          public_inputs=b"historical", proof_hash="0x" + "1" * 64,
                          circuit_parameters={"simulated": True}, declared_visibility={},
                          verified_locally=True))
        historical_model_id = model.id
    assert worker.run_next(
        sesiones, publisher=publicador, engine=StubInferenceEngine(),
        prover=SimulatedProofGenerator(),
        chain=SimulatedChainClient(derivation_threshold=1.0,
                                  policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO),
    ) is not None
    with session_scope(sesiones) as session:
        job = session.query(Job).one()
        inference = session.get(Inference, job.inference_id)
        expected_class, expected_confidence = inference.predicted_class.value, inference.confidence
        expected_vector = array("f", inference.feature_vector).tolist()
        expected_hash = session.get(Proof, job.proof_id).proof_hash
        # Otra fila posterior tampoco reemplaza la clasificación confirmada.
        session.add(Inference(
            claim_id=reclamo_registrado["id"], model_id=historical_model_id,
            feature_vector=array("f", [8.0] * 576).tobytes(),
            extractor_artifact_hash="0" * 64, logits=array("f", [8.0] * 6).tobytes(),
            predicted_class=inference.predicted_class, confidence=0.02,
        ))
        if legacy:
            job.inference_id = job.proof_id = None
            job.checkpoint_context = None
    path = f'/reclamos/{reclamo_registrado["number"]}'
    explanation = cliente.get(path + "/explicacion", headers={
        "Authorization": f'Bearer {reclamo_registrado["token"]}',
    }).json()
    assert explanation["model"]["predictedClass"] == expected_class
    assert explanation["model"]["confidence"] == expected_confidence
    trace = cliente.get(f'/publico{path}/traza').json()
    assert trace["featureVector"] == expected_vector
    assert trace["artifacts"]["proof"]["proofHash"] == expected_hash
    row = cliente.get("/revision/bandeja", headers={
        "Authorization": "Bearer revisor-rev-04",
    }).json()["rows"][0]
    assert row["predictedClass"] == expected_class
    assert row["confidence"] == expected_confidence
    assert row["accredited"] is True

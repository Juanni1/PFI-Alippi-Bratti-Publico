"""El cliente de cadena, simulado y real.

Las pruebas del cliente real se saltan si no hay nodo escuchando y un artefacto
de despliegue: necesitan `npx hardhat node` y `deploy-local.js` corriendo en
PFI-Contracts. Lo que no se saltan nunca son las tablas de traducción de enums,
porque su desalineación con el contrato no se ve al ejecutar (un estado se
traduce a otro estado, no a un error) y es el tipo de fallo que llega hasta la
pantalla del reclamante.
"""

from __future__ import annotations

import json
import os

import pytest

from app.adaptadores.chain import (
    CAUSAS_DEL_CONTRATO,
    GAS_DE_ENVIO_DE_INFERENCIA,
    DESPLIEGUE_POR_DEFECTO,
    ESTADOS_DEL_CONTRATO,
    PUNTOS_BASICOS,
    SimulatedChainClient,
    Web3ChainClient,
    build_chain_client,
    identificador_on_chain,
)
from app.dominio.ports import ProofResult
from app.dominio.vocabulary import ClaimStatus, PolicyMode, TransitionCause

DESPLIEGUE = os.environ.get("CADENA_DESPLIEGUE", DESPLIEGUE_POR_DEFECTO)
# Cuenta #0 de Hardhat: clave de prueba pública y conocida, sin fondos reales.
CLAVE_DE_PRUEBA = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _hay_cadena() -> bool:
    if not os.path.exists(DESPLIEGUE):
        return False
    try:
        from web3 import Web3

        return Web3(Web3.HTTPProvider(os.environ.get("CADENA_RPC", "http://127.0.0.1:8545"))).is_connected()
    except Exception:  # noqa: BLE001
        return False


sin_cadena = pytest.mark.skipif(
    not _hay_cadena(), reason="no hay nodo local con el despliegue del contrato"
)


# ---------------------------------------------------------------------------
# Traducción de enums: se comprueba sin nodo
# ---------------------------------------------------------------------------


def test_los_estados_siguen_el_orden_del_contrato():
    """El orden es parte del ABI: el evento lleva el índice, no el nombre."""
    assert ESTADOS_DEL_CONTRATO[0] is None
    assert ESTADOS_DEL_CONTRATO[1] is ClaimStatus.REGISTRADO
    assert ESTADOS_DEL_CONTRATO[5] is ClaimStatus.VERIFICACION_FALLIDA
    assert ESTADOS_DEL_CONTRATO[7] is ClaimStatus.RECHAZADO
    assert len(ESTADOS_DEL_CONTRATO) == 8


def test_las_causas_siguen_el_orden_del_contrato():
    assert CAUSAS_DEL_CONTRATO[0] is None
    assert CAUSAS_DEL_CONTRATO[1] is TransitionCause.CLASIFICACION_FAVORABLE
    assert CAUSAS_DEL_CONTRATO[8] is TransitionCause.LIQUIDACION_AUTOMATICA
    assert CAUSAS_DEL_CONTRATO[9] is TransitionCause.LIQUIDACION_DEL_OPERADOR
    assert len(CAUSAS_DEL_CONTRATO) == 12


def test_las_tablas_cubren_el_vocabulario_entero():
    """Un estado del dominio que no esté en la tabla es uno que el contrato no
    sabría devolver."""
    assert set(ESTADOS_DEL_CONTRATO) - {None} == set(ClaimStatus)
    assert set(CAUSAS_DEL_CONTRATO) - {None} == set(TransitionCause)


def test_el_identificador_es_keccak_y_no_sha256():
    """Que sea keccak es lo que deja a un tercero recomputarlo dentro de un
    contrato. El valor está fijado para que un cambio de función se note."""
    esperado = "e883a8d2cd170c7bb0aefe4366e3117294d0c908a247ad1621d360ad92bea79f"
    assert identificador_on_chain("SN-4119").hex() == esperado
    assert identificador_on_chain("SN-4119") != identificador_on_chain("SN-4120")


# ---------------------------------------------------------------------------
# Selección por entorno
# ---------------------------------------------------------------------------


def test_sin_variable_corre_el_simulado(monkeypatch):
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    monkeypatch.delenv("CLIENTE_CADENA", raising=False)
    cliente = build_chain_client(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
    assert isinstance(cliente, SimulatedChainClient)


@pytest.mark.parametrize("value", [None, "simulado", "typo-secret-sentinel"])
def test_produccion_exige_web3_antes_de_cargar_configuracion(monkeypatch, value):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", " Production ")
    if value is None:
        monkeypatch.delenv("CLIENTE_CADENA", raising=False)
    else:
        monkeypatch.setenv("CLIENTE_CADENA", value)
    with pytest.raises(ValueError, match="CLIENTE_CADENA.*web3") as error:
        build_chain_client(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
    assert "typo-secret-sentinel" not in str(error.value)


def test_local_valor_desconocido_conserva_simulador(monkeypatch):
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    monkeypatch.setenv("CLIENTE_CADENA", "desconocido")
    assert isinstance(build_chain_client(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO), SimulatedChainClient)


def test_produccion_web3_conserva_error_de_despliegue(monkeypatch, tmp_path):
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    monkeypatch.setenv("CLIENTE_CADENA", "web3")
    monkeypatch.setenv("CADENA_DESPLIEGUE", str(tmp_path / "inexistente"))
    with pytest.raises(FileNotFoundError, match="deploy-local"):
        build_chain_client(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)


def test_web3_sin_clave_levanta_en_lugar_de_caer_al_simulado(monkeypatch, tmp_path):
    """Caer al simulado en silencio dejaría una traza sin respaldo con aspecto
    de traza acreditada."""
    artefacto = tmp_path / "despliegue.json"
    artefacto.write_text(json.dumps({
        "red": {"nombre": "local", "chainId": 12345},
        "oraculo": "0x" + "11" * 20,
        "verificador": "0x" + "22" * 20,
        "registro": "0x" + "33" * 20,
        "abi": [],
        "umbralDeDerivacionBps": 9000,
        "bytecodeDelVerificador": {"bytes": 2, "limiteEip170": 24576},
    }))
    monkeypatch.setenv("CLIENTE_CADENA", "web3")
    monkeypatch.setenv("CADENA_DESPLIEGUE", str(artefacto))
    monkeypatch.delenv("CADENA_CLAVE_ORACULO", raising=False)
    with pytest.raises(ValueError, match="CADENA_CLAVE_ORACULO"):
        build_chain_client(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)


def test_web3_sin_despliegue_levanta(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIENTE_CADENA", "web3")
    monkeypatch.setenv("CADENA_DESPLIEGUE", str(tmp_path / "no-existe.json"))
    with pytest.raises(FileNotFoundError, match="deploy-local"):
        build_chain_client(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)


@pytest.fixture
def artifact_client(monkeypatch, tmp_path):
    """RPC aislada: cuenta, lecturas y ausencia de efectos observables."""
    import web3

    address = "0x" + "11" * 20
    verifier = "0x" + "22" * 20
    registry = "0x" + "33" * 20
    account = type("Account", (), {"address": address})()
    observations = []
    state = {
        "chain_id": 12345,
        "oracle": address,
        "verifier": verifier,
        "registry_code": bytes.fromhex("6001"),
        "verifier_code": bytes.fromhex("6002"),
    }

    class Call:
        def __init__(self, name):
            self.name = name

        def call(self):
            observations.append(self.name)
            return state[self.name]

    class Functions:
        def oracle(self):
            return Call("oracle")

        def verifier(self):
            return Call("verifier")

    class Eth:
        @property
        def chain_id(self):
            observations.append("chain_id")
            if state.get("rpc_error"):
                raise ValueError("http://user:password@localhost:8545 clave-ficticia")
            return state["chain_id"]
        account = type("Accounts", (), {"from_key": lambda self, key: account})()

        def contract(self, *, address, abi):
            observations.append("contract")
            return type("Contract", (), {"functions": Functions()})()

        def get_code(self, address):
            observations.append("get_code")
            return state["registry_code"] if address.lower() == registry.lower() else state["verifier_code"]

        def send_raw_transaction(self, value):
            raise AssertionError("No se debe transmitir")

    class FakeWeb3:
        HTTPProvider = staticmethod(lambda url, request_kwargs: object())

        def __init__(self, provider):
            self.eth = Eth()

        def is_connected(self):
            observations.append("connected")
            return True

        to_checksum_address = staticmethod(web3.Web3.to_checksum_address)
        keccak = staticmethod(web3.Web3.keccak)

    monkeypatch.setattr(web3, "Web3", FakeWeb3)
    artifact = {
        "red": {"nombre": "local", "chainId": state["chain_id"]},
        "oraculo": address,
        "verificador": verifier,
        "registro": registry,
        "abi": [],
        "umbralDeDerivacionBps": 9000,
        "bytecodeDelVerificador": {"bytes": 2, "limiteEip170": 24576},
    }
    path = tmp_path / "despliegue.json"
    monkeypatch.setenv("CLIENTE_CADENA", "web3")
    monkeypatch.setenv("CADENA_DESPLIEGUE", str(path))
    monkeypatch.setenv("CADENA_CLAVE_ORACULO", "clave-ficticia")
    monkeypatch.setenv("CADENA_RPC", "http://user:password@localhost:8545")

    def build():
        path.write_text(json.dumps(artifact))
        return build_chain_client(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)

    return artifact, state, observations, build


def test_artifact_binding_accepts_legacy_and_direct_client(artifact_client):
    artifact, state, observations, build = artifact_client
    assert isinstance(build(), Web3ChainClient)
    assert isinstance(Web3ChainClient(rpc_url="http://localhost", contract_address=artifact["registro"],
                                      oracle_private_key="clave-ficticia", abi=[]), Web3ChainClient)
    assert observations.count("oracle") == 2


@pytest.mark.parametrize("change,expected", [
    (lambda a, s: a["red"].update(chainId=s["chain_id"] + 1), "red"),
    (lambda a, s: a.update(oraculo="0x" + "44" * 20), "oráculo"),
    (lambda a, s: s.update(oracle="0x" + "44" * 20), "oráculo"),
    (lambda a, s: a.update(verificador="0x" + "44" * 20), "verificador"),
    (lambda a, s: s.update(registry_code=b""), "código"),
    (lambda a, s: s.update(verifier_code=b""), "código"),
])
def test_artifact_binding_rejects_mismatch(artifact_client, change, expected):
    artifact, state, observations, build = artifact_client
    change(artifact, state)
    with pytest.raises(ValueError, match=expected) as error:
        build()
    assert "password" not in str(error.value)
    assert "clave-ficticia" not in str(error.value)
    assert "send" not in observations


@pytest.mark.parametrize("change", [
    lambda a: a.update(estado="pendiente"),
    lambda a: a.update(estado="confirmado"),
    lambda a: a.pop("red"),
    lambda a: a["red"].update(chainId="12345"),
    lambda a: a.update(registro="not-an-address"),
    lambda a: a.update(codigo={"verificadorHash": "not-a-hash"}),
    lambda a: a.update(codigo={"verificadorHash": "0x" + "55" * 32,
                               "verificadorEsperadoHash": "0x" + "66" * 32,
                               "registroHash": "0x" + "77" * 32,
                               "registroEsperadoHash": "0x" + "77" * 32,
                               "registroBytes": 2}),
])
def test_artifact_binding_rejects_incomplete_before_rpc(artifact_client, change):
    artifact, state, observations, build = artifact_client
    change(artifact)
    with pytest.raises(ValueError, match="artefacto"):
        build()
    assert observations == []


@pytest.mark.parametrize("target", ["registro", "verificador"])
def test_artifact_binding_checks_confirmed_code_hash(artifact_client, target):
    from web3 import Web3

    artifact, state, observations, build = artifact_client
    artifact["codigo"] = {
        "registroHash": "0x" + Web3.keccak(state["registry_code"]).hex(),
        "registroEsperadoHash": "0x" + Web3.keccak(state["registry_code"]).hex(),
        "verificadorHash": "0x" + Web3.keccak(state["verifier_code"]).hex(),
        "verificadorEsperadoHash": "0x" + Web3.keccak(state["verifier_code"]).hex(),
        "registroBytes": 2,
    }
    assert isinstance(build(), Web3ChainClient)
    state[("registry" if target == "registro" else "verifier") + "_code"] = bytes.fromhex("6003")
    with pytest.raises(ValueError, match="código"):
        build()
    assert "send" not in observations


def test_artifact_rpc_exception_does_not_expose_credentials(artifact_client):
    artifact, state, observations, build = artifact_client
    # La RPC puede contener credenciales en el texto de sus errores.
    state["rpc_error"] = True
    with pytest.raises(ConnectionError, match="RPC del artefacto") as error:
        build()
    assert "password" not in str(error.value)
    assert "clave-ficticia" not in str(error.value)


def test_artifact_provider_construction_does_not_expose_credentials(
    artifact_client, monkeypatch, capsys
):
    import web3

    _, _, observations, build = artifact_client

    def failing_provider(*args, **kwargs):
        raise ValueError("http://user:password@localhost:8545 clave-ficticia")

    monkeypatch.setattr(web3.Web3, "HTTPProvider", failing_provider)
    with pytest.raises(ConnectionError, match="RPC configurada") as error:
        build()
    assert "password" not in str(error.value)
    assert "clave-ficticia" not in str(error.value)
    assert observations == []
    assert capsys.readouterr() == ("", "")


def test_direct_client_preserves_provider_construction_error(artifact_client, monkeypatch):
    import web3

    artifact, _, observations, _ = artifact_client

    def failing_provider(*args, **kwargs):
        raise ValueError("fallo del proveedor directo")

    monkeypatch.setattr(web3.Web3, "HTTPProvider", failing_provider)
    with pytest.raises(ValueError, match="^fallo del proveedor directo$"):
        Web3ChainClient(rpc_url="http://localhost", contract_address=artifact["registro"],
                        oracle_private_key="clave-ficticia", abi=[])
    assert observations == []


def test_el_registro_simulado_no_inventa_una_causa():
    """La entrada al sistema no es una transición de la matriz: con una causa
    aparecería en la línea de tiempo del reclamante como si lo fuera."""
    cliente = SimulatedChainClient(derivation_threshold=0.9, policy_mode=PolicyMode.AUTOMATICO_CON_ESCALAMIENTO)
    recibo = cliente.register_claim("SN-4119", "bafy...")
    assert recibo.cause is None
    assert recibo.transaction_hash is None


# ---------------------------------------------------------------------------
# Contra el nodo
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def prueba_real():
    """Una prueba criptográfica de verdad, generada una sola vez para el módulo.

    No se puede sustituir por un artefacto de juguete: lo que estas pruebas
    ejercitan es que el verificador de EZKL desplegado la acepte, y ese
    verificador no acepta nada que no venga de su circuito.
    """
    from array import array

    from app.adaptadores.proving import ARTEFACTOS_DISTRIBUIDOS, EzklProofGenerator
    from app.dominio.ports import InferenceResult
    from app.dominio.vocabulary import DamageClass

    ruta = os.environ.get("RUTA_ARTEFACTOS", "artefactos")
    if not all(os.path.exists(os.path.join(ruta, n)) for n in ARTEFACTOS_DISTRIBUIDOS):
        pytest.skip("no están los artefactos del circuito")

    caracteristicas = array("f", [0.05] * 576)
    inferencia = InferenceResult(
        feature_vector=caracteristicas.tobytes(),
        logits=array("f", [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]).tobytes(),
        predicted_class=DamageClass.ABOLLADURA,
        confidence=0.97,
        extractor_artifact_hash="0" * 64,
        model_version="prueba",
    )
    return EzklProofGenerator(artifacts_dir=ruta).prove(inferencia)


@pytest.fixture
def cliente_real():
    with open(DESPLIEGUE) as archivo:
        datos = json.load(archivo)
    return Web3ChainClient(
        rpc_url=os.environ.get("CADENA_RPC", "http://127.0.0.1:8545"),
        contract_address=datos["registro"],
        oracle_private_key=os.environ.get("CADENA_CLAVE_ORACULO", CLAVE_DE_PRUEBA),
        abi=datos["abi"],
    )


@sin_cadena
def test_una_clave_que_no_es_la_del_oraculo_falla_al_construir():
    """El modificador onlyOracle revierte sin distinguir causa. Comprobarlo al
    construir convierte un fallo por reclamo en uno al arrancar."""
    with open(DESPLIEGUE) as archivo:
        datos = json.load(archivo)
    # Cuenta #1 de Hardhat, que no es el oráculo del despliegue.
    otra = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
    with pytest.raises(ValueError, match="oráculo"):
        Web3ChainClient(
            rpc_url=os.environ.get("CADENA_RPC", "http://127.0.0.1:8545"),
            contract_address=datos["registro"],
            oracle_private_key=otra,
            abi=datos["abi"],
        )


@sin_cadena
def test_el_registro_devuelve_hash_y_bloque_reales(cliente_real):
    import uuid

    numero = f"SN-T{uuid.uuid4().hex[:8]}"
    recibo = cliente_real.register_claim(numero, "bafytest")
    assert recibo.status is ClaimStatus.REGISTRADO
    # Con prefijo, como el hash de la prueba: los dos viajan a la misma traza y
    # que uno lo lleve y el otro no obliga a normalizar del lado del que lee.
    assert recibo.transaction_hash.startswith("0x")
    assert len(recibo.transaction_hash) == 66
    assert recibo.block_number is not None and recibo.block_number > 0
    assert recibo.extra["gas_used"] > 0


@sin_cadena
def test_registrar_dos_veces_no_traba_el_reclamo(cliente_real):
    """Un job que registró en la cadena y falló antes de confirmar en la base
    reintenta: sin idempotencia el contrato revertiría y el reclamo quedaría
    trabado."""
    import uuid

    numero = f"SN-T{uuid.uuid4().hex[:8]}"
    cliente_real.register_claim(numero, "bafytest")
    segundo = cliente_real.register_claim(numero, "bafytest")
    assert segundo.extra["ya_estaba_registrado"] is True


@sin_cadena
def test_una_prueba_valida_verifica_on_chain_y_clasifica(cliente_real, prueba_real):
    """El camino entero: el contrato verifica la prueba criptográfica y recién
    después clasifica (RF-27)."""
    import uuid

    numero = f"SN-T{uuid.uuid4().hex[:8]}"
    cliente_real.register_claim(numero, "bafytest")
    recibo = cliente_real.submit_inference(
        numero,
        prueba_real,
        confidence=0.97,
        amount_above_threshold=False,
        rules_consistent=True,
    )
    assert recibo.status is ClaimStatus.APROBABLE
    assert recibo.cause is TransitionCause.CLASIFICACION_FAVORABLE
    assert recibo.transaction_hash is not None
    assert recibo.extra["gas_used"] > 1_000_000  # la verificación domina el gas


@sin_cadena
def test_la_confianza_bajo_el_umbral_deriva_a_revision(cliente_real, prueba_real):
    import uuid

    numero = f"SN-T{uuid.uuid4().hex[:8]}"
    cliente_real.register_claim(numero, "bafytest")
    recibo = cliente_real.submit_inference(
        numero,
        prueba_real,
        confidence=0.80,
        amount_above_threshold=False,
        rules_consistent=True,
    )
    assert recibo.status is ClaimStatus.REQUIERE_REVISION
    assert recibo.cause is TransitionCause.CONFIANZA_BAJO_UMBRAL


@sin_cadena
def test_una_prueba_adulterada_asienta_verificacion_fallida_y_no_revierte(
    cliente_real, prueba_real
):
    """RNF-17. El verificador de EZKL revierte ante prueba inválida; el
    contrato atrapa el revert y asienta el episodio en lugar de borrarlo."""
    import uuid

    artefacto = json.loads(prueba_real.proof)
    crudo = bytearray(bytes.fromhex(artefacto["hex_proof"].removeprefix("0x")))
    crudo[0] ^= 0xFF
    artefacto["hex_proof"] = "0x" + crudo.hex()
    adulterada = ProofResult(
        proof=json.dumps(artefacto).encode(),
        public_inputs=prueba_real.public_inputs,
        proof_hash=prueba_real.proof_hash,
        circuit_parameters=prueba_real.circuit_parameters,
        declared_visibility=prueba_real.declared_visibility,
        verified_locally=True,
    )

    numero = f"SN-T{uuid.uuid4().hex[:8]}"
    cliente_real.register_claim(numero, "bafytest")
    recibo = cliente_real.submit_inference(
        numero,
        adulterada,
        confidence=0.97,
        amount_above_threshold=False,
        rules_consistent=True,
    )
    assert recibo.status is ClaimStatus.VERIFICACION_FALLIDA
    assert recibo.cause is TransitionCause.FALLO_DE_VERIFICACION
    assert recibo.transaction_hash is not None


@sin_cadena
def test_la_liquidacion_automatica_se_distingue_de_la_del_operador(
    cliente_real, prueba_real
):
    """adr-007: la causa del evento es lo único que un auditor externo tiene
    para reconstruir por cuál de los dos caminos se liquidó."""
    import uuid

    numero = f"SN-T{uuid.uuid4().hex[:8]}"
    cliente_real.register_claim(numero, "bafytest")
    cliente_real.submit_inference(
        numero,
        prueba_real,
        confidence=0.97,
        amount_above_threshold=False,
        rules_consistent=True,
    )
    recibo = cliente_real.settle(numero, automatic=True)
    assert recibo.status is ClaimStatus.APROBADO
    assert recibo.cause is TransitionCause.LIQUIDACION_AUTOMATICA


@sin_cadena
def test_la_confianza_viaja_en_puntos_basicos(cliente_real, prueba_real):
    """El contrato guarda el umbral en puntos básicos para no meter coma
    flotante en la EVM. 0,90 exacto tiene que quedar del lado que clasifica
    como favorable, y no un punto por debajo."""
    import uuid

    assert PUNTOS_BASICOS == 10_000
    numero = f"SN-T{uuid.uuid4().hex[:8]}"
    cliente_real.register_claim(numero, "bafytest")
    recibo = cliente_real.submit_inference(
        numero,
        prueba_real,
        confidence=0.90,
        amount_above_threshold=False,
        rules_consistent=True,
    )
    assert recibo.status is ClaimStatus.APROBABLE


@sin_cadena
def test_la_estimacion_de_gas_por_si_sola_da_un_veredicto_falso(cliente_real, prueba_real):
    """El fallo que este cliente tuvo y que ninguna otra prueba detectaría.

    El contrato envuelve la verificación en try/catch porque el verificador
    revierte ante prueba inválida. Con eso la transacción nunca revierte, y
    `eth_estimateGas` converge al camino más barato que no falla: el del fallo
    de verificación. Aplicado como límite, la subllamada recibe 63/64 del gas
    restante, se queda sin gas a mitad de verificar y una prueba **válida**
    termina asentada como VerificacionFallida.

    No es un fallo que se note: no hay excepción, no hay reversión, y el reclamo
    sigue su curso hacia revisión humana con un episodio que nunca ocurrió.
    """
    import uuid

    from app.adaptadores.chain import identificador_on_chain as ident

    numero = f"SN-T{uuid.uuid4().hex[:8]}"
    cliente_real.register_claim(numero, "bafytest")

    bytes_de_la_prueba, instancias = cliente_real._instancias(prueba_real)
    funcion = cliente_real.contrato.functions.submitInference(
        ident(numero), bytes_de_la_prueba, instancias, 9700, False, True
    )
    estimado = funcion.estimate_gas({"from": cliente_real.cuenta.address})

    # El techo del cliente tiene que quedar por encima de la estimación, que es
    # exactamente lo que la vuelve inservible acá.
    assert GAS_DE_ENVIO_DE_INFERENCIA > estimado

    recibo = cliente_real.submit_inference(
        numero,
        prueba_real,
        confidence=0.97,
        amount_above_threshold=False,
        rules_consistent=True,
    )
    assert recibo.status is ClaimStatus.APROBABLE


@sin_cadena
def test_un_numero_reusado_de_otra_corrida_falla_donde_se_entiende(
    cliente_real, prueba_real
):
    """La cadena persiste y la base de un entorno de prueba no.

    Vaciar la base reinicia la numeración, y el número reciclado apunta a un
    reclamo de otra corrida que el contrato ya clasificó. Sin esta comprobación
    el registro pasaría en silencio y el fallo aparecería recién al enviar la
    inferencia, como un InvalidTransition sin relación aparente con su causa.
    """
    import uuid

    numero = f"SN-T{uuid.uuid4().hex[:8]}"
    cliente_real.register_claim(numero, "bafytest")
    cliente_real.submit_inference(
        numero,
        prueba_real,
        confidence=0.80,
        amount_above_threshold=False,
        rules_consistent=True,
    )
    with pytest.raises(RuntimeError, match="otra corrida"):
        cliente_real.register_claim(numero, "bafytest")


@sin_cadena
def test_reenviar_la_inferencia_falla_nombrando_la_divergencia(cliente_real, prueba_real):
    """Cuando la cadena avanzó y la base no lo asentó, el reintento reenvía.

    El contrato lo rechaza con InvalidTransition, que por la RPC llega como un
    revert crudo sin nombrar el reclamo. Acá se corta antes, con el estado que
    el contrato tiene y el que la operación exigía.
    """
    import uuid

    numero = f"SN-T{uuid.uuid4().hex[:8]}"
    cliente_real.register_claim(numero, "bafytest")
    cliente_real.submit_inference(
        numero,
        prueba_real,
        confidence=0.97,
        amount_above_threshold=False,
        rules_consistent=True,
    )
    with pytest.raises(RuntimeError, match="solo se admite desde Registrado"):
        cliente_real.submit_inference(
            numero,
            prueba_real,
            confidence=0.97,
            amount_above_threshold=False,
            rules_consistent=True,
        )

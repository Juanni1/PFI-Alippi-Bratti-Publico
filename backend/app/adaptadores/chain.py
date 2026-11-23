"""Cliente de la cadena: el oráculo del contrato.

Dos implementaciones. `Web3ChainClient` habla con `ClaimRegistry.sol`
desplegado, y el estado del reclamo lo decide el contrato: la base asienta lo
que la cadena devolvió. `SimulatedChainClient` replica en memoria esa misma
máquina, e invierte la dirección de la autoridad, porque con ella el estado lo
define la base. Esa inversión es la razón de que el recibo simulado devuelva
`transaction_hash` nulo: el nulo es la marca de que la transición no tiene
respaldo on-chain, y no se rellena con un valor plausible.

El destino se elige por configuración. El cliente vincula el despliegue
configurado con la identidad de la RPC antes de operar.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

from app.dominio.ports import ChainReceipt, ProofResult
from app.dominio.state_machine import classify, resolution_cause, settlement_cause
from app.dominio.vocabulary import ClaimStatus, PolicyMode, TransitionCause


class SimulatedChainClient:
    """Réplica local de la máquina del contrato.

    Se construye con la configuración vigente porque el contrato guarda su
    propia copia operativa de los umbrales: la clasificación tiene que ocurrir
    contra los mismos valores de los dos lados o la réplica deja de serlo.
    """

    def __init__(
        self,
        *,
        derivation_threshold: float | None,
        policy_mode: PolicyMode,
        verifier_accepts: bool = True,
    ) -> None:
        self.derivation_threshold = derivation_threshold
        self.policy_mode = policy_mode
        # Permite ejercitar el camino de RNF-17 sin adulterar una prueba real.
        self.verifier_accepts = verifier_accepts

    def register_claim(self, claim_id: str, evidence_id: str) -> ChainReceipt:
        # Sin causa: la entrada al sistema no es una transicion de la matriz y
        # el contrato la emite con su propio evento. Poner una causa cualquiera
        # aca la haria aparecer en la linea de tiempo del reclamante.
        return ChainReceipt(
            status=ClaimStatus.REGISTRADO,
            cause=None,
            extra={"evidence_id": evidence_id},
        )

    def submit_inference(
        self,
        claim_id: str,
        proof: ProofResult,
        *,
        confidence: float,
        amount_above_threshold: bool,
        rules_consistent: bool,
    ) -> ChainReceipt:
        """Verifica y, solo si verifica, clasifica.

        Ante prueba inválida no falla: asienta `VerificacionFallida` y sigue
        (RNF-17). Revertir borraría el episodio, y el episodio es justamente lo
        que sostiene el argumento de auditabilidad.
        """
        if not self.verifier_accepts:
            return ChainReceipt(
                status=ClaimStatus.VERIFICACION_FALLIDA,
                cause=TransitionCause.FALLO_DE_VERIFICACION,
                proof_hash=proof.proof_hash,
            )

        # Las reglas se evalúan únicamente después de una verificación
        # exitosa (RF-27).
        if self.derivation_threshold is None:
            # Sin umbral medido no hay clasificación automática posible: el
            # reclamo va a revisión humana en lugar de compararse contra un
            # número inventado.
            return ChainReceipt(
                status=ClaimStatus.REQUIERE_REVISION,
                cause=TransitionCause.CONFIANZA_BAJO_UMBRAL,
                proof_hash=proof.proof_hash,
            )

        clasificacion = classify(
            confidence=confidence,
            derivation_threshold=self.derivation_threshold,
            amount_above_limit=amount_above_threshold,
            rules_consistent=rules_consistent,
            policy_mode=self.policy_mode,
        )
        return ChainReceipt(
            status=clasificacion.status,
            cause=clasificacion.cause,
            proof_hash=proof.proof_hash,
        )

    def settle(self, claim_id: str, *, automatic: bool) -> ChainReceipt:
        return ChainReceipt(
            status=ClaimStatus.APROBADO,
            cause=settlement_cause(automatic=automatic),
        )

    def resolve(
        self, claim_id: str, *, favorable: bool, decision_hash: str
    ) -> ChainReceipt:
        return ChainReceipt(
            status=ClaimStatus.APROBADO if favorable else ClaimStatus.RECHAZADO,
            cause=resolution_cause(favorable=favorable),
        )


# ---------------------------------------------------------------------------
# Cliente real
# ---------------------------------------------------------------------------

# El contrato enumera sus estados y causas por posición, y el orden es parte de
# su ABI: una fila de más en el medio de `Status` cambiaría el significado de
# todos los eventos ya emitidos. Estas tuplas replican ese orden y no lo
# reordenan por comodidad.
ESTADOS_DEL_CONTRATO = (
    None,  # Status.None — el reclamo no existe en el mapping
    ClaimStatus.REGISTRADO,
    ClaimStatus.APROBABLE,
    ClaimStatus.REQUIERE_REVISION,
    ClaimStatus.INCONSISTENCIA_EVIDENCIA,
    ClaimStatus.VERIFICACION_FALLIDA,
    ClaimStatus.APROBADO,
    ClaimStatus.RECHAZADO,
)

CAUSAS_DEL_CONTRATO = (
    None,  # Cause.None
    TransitionCause.CLASIFICACION_FAVORABLE,
    TransitionCause.CONFIANZA_BAJO_UMBRAL,
    TransitionCause.MONTO_SOBRE_UMBRAL,
    TransitionCause.POLITICA_CONFIRMACION_HUMANA,
    TransitionCause.INCONSISTENCIA_CON_REGLAS,
    TransitionCause.FALLO_DE_VERIFICACION,
    TransitionCause.ESCALAMIENTO_OBLIGATORIO,
    TransitionCause.LIQUIDACION_AUTOMATICA,
    TransitionCause.LIQUIDACION_DEL_OPERADOR,
    TransitionCause.RESOLUCION_FAVORABLE,
    TransitionCause.RESOLUCION_DESFAVORABLE,
)

# Puntos básicos: la unidad en que el contrato guarda su copia del umbral, para
# no meter coma flotante en la EVM.
PUNTOS_BASICOS = 10_000

# Límite de gas fijo para el envío de la inferencia, y NO el que `eth_estimateGas`
# devuelve. La estimación es inservible para esta función en particular: el
# contrato envuelve la verificación en un try/catch porque el verificador de
# EZKL revierte ante prueba inválida (RF-23, RNF-17), de modo que la
# transacción nunca revierte y el estimador converge al camino más barato que
# «no falla», que es justamente el del fallo de verificación. Con ese límite la
# subllamada recibe 63/64 del gas restante, se queda sin gas a mitad de la
# verificación y el catch asienta VerificacionFallida sobre una prueba válida.
# Medido: la verificación exitosa consume 1.088.754 y la estimación devuelve ese
# mismo número, que aplicado como límite produce el fallo. El techo deja margen
# y no cuesta nada, porque lo que no se usa no se cobra.
GAS_DE_ENVIO_DE_INFERENCIA = int(os.environ.get("CADENA_GAS_INFERENCIA", 3_000_000))


def identificador_on_chain(valor: str) -> bytes:
    """Cadena de texto a `bytes32`.

    El número de seguimiento y el CID de la evidencia son de largo variable y
    `bytes32` no lo es, así que va su digesto. Que sea keccak y no sha256 es
    porque es la función que la EVM calcula nativamente, y así un tercero puede
    recomputar el identificador dentro de un contrato.
    """
    from eth_utils import keccak  # noqa: PLC0415

    return keccak(text=valor)


class Web3ChainClient:
    """Oráculo real contra `ClaimRegistry.sol`.

    A diferencia del simulado no recibe umbral ni modo de política: el contrato
    guarda su propia copia operativa y clasifica con ella. Pasárselos sería
    dejar dos fuentes de verdad para la misma decisión, y la que manda es la
    que quedó escrita en la cadena.
    """

    def __init__(
        self,
        *,
        rpc_url: str,
        contract_address: str,
        oracle_private_key: str,
        abi: list,
        timeout: int = 120,
        expected_artifact: dict | None = None,
    ) -> None:
        from web3 import Web3  # noqa: PLC0415

        try:
            self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))
        except Exception:
            if expected_artifact is not None:
                raise ConnectionError("No se pudo conectar con la RPC configurada.") from None
            raise
        try:
            connected = self.w3.is_connected()
        except Exception:
            raise ConnectionError("No se pudo conectar con la RPC configurada.") from None
        if not connected:
            raise ConnectionError("No hay nodo escuchando en la RPC configurada.")

        try:
            self.cuenta = self.w3.eth.account.from_key(oracle_private_key)
            self.contrato = self.w3.eth.contract(
                address=self.w3.to_checksum_address(contract_address), abi=abi
            )
        except Exception:
            if expected_artifact is not None:
                raise ValueError("La clave o el artefacto configurados no son válidos.") from None
            raise
        self.timeout = timeout

        # En el constructor directo se conserva el control histórico del oráculo.
        # El camino configurado agrega la identidad del despliegue antes de operar.
        if expected_artifact is not None:
            def rpc_read(callback):
                try:
                    return callback()
                except Exception:
                    # El proveedor puede incluir URL y credenciales en su excepción.
                    raise ConnectionError("No se pudo verificar la RPC del artefacto.") from None

            if rpc_read(lambda: self.w3.eth.chain_id) != expected_artifact["red"]["chainId"]:
                raise ValueError("La red RPC no coincide con el artefacto.")
            if self.cuenta.address.lower() != expected_artifact["oraculo"].lower():
                raise ValueError("La clave no coincide con el oráculo del artefacto.")
            oracle = rpc_read(lambda: self.contrato.functions.oracle().call())
            if oracle.lower() != expected_artifact["oraculo"].lower():
                raise ValueError("El oráculo on-chain no coincide con el artefacto.")
            verifier = rpc_read(lambda: self.contrato.functions.verifier().call())
            if verifier.lower() != expected_artifact["verificador"].lower():
                raise ValueError("El verificador on-chain no coincide con el artefacto.")
            for address, label in (
                (contract_address, "registro"),
                (expected_artifact["verificador"], "verificador"),
            ):
                code = rpc_read(lambda: self.w3.eth.get_code(self.w3.to_checksum_address(address)))
                if not code:
                    raise ValueError("Falta código en el destino del artefacto.")
                metadata = expected_artifact.get("codigo")
                if metadata is not None:
                    digest = "0x" + Web3.keccak(code).hex()
                    if digest.lower() != metadata[label + "Hash"].lower():
                        raise ValueError("El código on-chain no coincide con el artefacto.")
                    if label == "registro" and len(code) != metadata["registroBytes"]:
                        raise ValueError("El tamaño del código on-chain no coincide con el artefacto.")
        else:
            oracle = self.contrato.functions.oracle().call()
            if oracle.lower() != self.cuenta.address.lower():
                raise ValueError(
                    f"La clave configurada es de {self.cuenta.address} y el contrato "
                    f"reconoce como oráculo a {oracle}. Con esta clave toda "
                    "transición revertiría."
                )

    # -- infraestructura ---------------------------------------------------

    def _enviar(self, funcion, gas: int | None = None):
        """Firma, envía y espera. Devuelve el recibo de la transacción.

        Se espera la confirmación en lugar de seguir de largo porque el estado
        que la base va a asentar es el que el contrato calculó, y hasta que la
        transacción no entra en un bloque ese estado no existe.

        Con `gas` en nulo se deja estimar, que es lo correcto para las funciones
        que revierten cuando algo sale mal. La que verifica una prueba no es una
        de esas.
        """
        parametros = {
            "from": self.cuenta.address,
            "nonce": self.w3.eth.get_transaction_count(self.cuenta.address),
        }
        if gas is not None:
            parametros["gas"] = gas
        transaccion = funcion.build_transaction(parametros)
        firmada = self.cuenta.sign_transaction(transaccion)
        hash_ = self.w3.eth.send_raw_transaction(firmada.raw_transaction)
        recibo = self.w3.eth.wait_for_transaction_receipt(hash_, timeout=self.timeout)
        if recibo["status"] != 1:
            raise RuntimeError(
                f"La transacción 0x{hash_.hex()} revirtió en el bloque "
                f"{recibo['blockNumber']}."
            )
        return recibo

    def _transicion_del_recibo(self, recibo) -> tuple[ClaimStatus, TransitionCause]:
        """Lee el evento `StateTransition` y devuelve estado y causa.

        El destino se toma del evento y no se deduce de lo que el backend
        esperaba: la clasificación la hizo el contrato, y si las dos réplicas
        difirieran, la que vale es la suya.
        """
        from web3.logs import DISCARD  # noqa: PLC0415

        # DISCARD y no el modo estricto: el recibo trae tambien los registros
        # del verificador, que este ABI no conoce y no tiene por que conocer.
        eventos = self.contrato.events.StateTransition().process_receipt(
            recibo, errors=DISCARD
        )
        if not eventos:
            raise RuntimeError(
                f"La transacción 0x{recibo['transactionHash'].hex()} no emitió "
                "StateTransition."
            )
        argumentos = eventos[-1]["args"]
        return (
            ESTADOS_DEL_CONTRATO[argumentos["to"]],
            CAUSAS_DEL_CONTRATO[argumentos["cause"]],
        )

    @staticmethod
    def _instancias(proof: ProofResult) -> tuple[bytes, list[int]]:
        """Extrae del artefacto de EZKL lo que el verificador espera.

        El campo `proof` del puerto guarda el JSON entero que produce EZKL, no
        los bytes de la prueba: adentro vienen la prueba en hexadecimal y las
        instancias públicas como elementos de campo en **little-endian**, que es
        el orden en que EZKL los serializa y el inverso del que la EVM usa para
        sus enteros. Invertirlo mal no da un error: da un verificador que
        rechaza una prueba válida.
        """
        artefacto = json.loads(proof.proof)
        bytes_de_la_prueba = bytes.fromhex(artefacto["hex_proof"].removeprefix("0x"))
        instancias = [
            int.from_bytes(bytes.fromhex(elemento), "little")
            for grupo in artefacto["instances"]
            for elemento in grupo
        ]
        return bytes_de_la_prueba, instancias

    # -- puerto ------------------------------------------------------------

    def register_claim(self, claim_id: str, evidence_id: str) -> ChainReceipt:
        # Idempotente a propósito, y solo hasta donde corresponde. Un job que
        # registró en la cadena y falló antes de confirmar en la base deja el
        # reclamo asentado de un lado y no del otro; al reintentar,
        # `registerClaim` revertiría con ClaimAlreadyExists y el reclamo quedaría
        # trabado. Reintentar sobre un reclamo que quedó en Registered es
        # inofensivo y se deja pasar.
        #
        # Encontrarlo en cualquier otro estado es harina de otro costal: la
        # cadena persiste y la base de un entorno de prueba no, así que un
        # número de seguimiento reutilizado después de vaciar la base apunta al
        # reclamo de otra corrida, ya clasificado. Dejarlo pasar produciría un
        # InvalidTransition más adelante, lejos de su causa; acá se declara.
        identificador = identificador_on_chain(claim_id)
        estado, _, _ = self.contrato.functions.claims(identificador).call()
        if estado == ESTADOS_DEL_CONTRATO.index(ClaimStatus.REGISTRADO):
            return ChainReceipt(
                status=ClaimStatus.REGISTRADO,
                cause=None,
                extra={"evidence_id": evidence_id, "ya_estaba_registrado": True},
            )
        if estado != 0:
            raise RuntimeError(
                f"El contrato ya conoce a {claim_id} y lo tiene en "
                f"{ESTADOS_DEL_CONTRATO[estado].value}, no en Registrado. El "
                "número de seguimiento pertenece a un reclamo de otra corrida: "
                "la cadena conserva estado que la base perdió. Redesplegar el "
                "contrato antes de reusar la numeración."
            )

        recibo = self._enviar(
            self.contrato.functions.registerClaim(
                identificador, identificador_on_chain(evidence_id)
            )
        )
        # La entrada no es una transición de la matriz y el contrato la emite
        # con su propio evento, sin causa. Por eso acá no se lee StateTransition.
        return ChainReceipt(
            status=ClaimStatus.REGISTRADO,
            cause=None,
            transaction_hash="0x" + recibo["transactionHash"].hex(),
            block_number=recibo["blockNumber"],
            extra={"evidence_id": evidence_id, "gas_used": recibo["gasUsed"]},
        )

    def submit_inference(
        self,
        claim_id: str,
        proof: ProofResult,
        *,
        confidence: float,
        amount_above_threshold: bool,
        rules_consistent: bool,
    ) -> ChainReceipt:
        """Manda la prueba al contrato, que verifica y clasifica.

        Ante prueba inválida esto **no** levanta excepción: el verificador
        revierte, el contrato atrapa el revert y asienta `VerificacionFallida`
        (RNF-17). La transacción entra igual, y el episodio queda en la cadena.
        """
        # Segunda barrera, por si la primera falla. El contrato solo admite el
        # envío desde Registrado; encontrarlo en otro estado significa que esta
        # inferencia ya se envió y que la base perdió el resultado. Sin esta
        # comprobación el fallo llega como un revert crudo de la RPC, que no
        # nombra ni el reclamo ni lo que pasó.
        identificador = identificador_on_chain(claim_id)
        estado, _, _ = self.contrato.functions.claims(identificador).call()
        if estado != ESTADOS_DEL_CONTRATO.index(ClaimStatus.REGISTRADO):
            actual = ESTADOS_DEL_CONTRATO[estado]
            raise RuntimeError(
                f"El contrato tiene a {claim_id} en "
                f"{actual.value if actual else 'ningún estado'} y el envío de "
                "la inferencia solo se admite desde Registrado. La transición "
                "ya ocurrió on-chain y la base no la asentó."
            )

        bytes_de_la_prueba, instancias = self._instancias(proof)
        recibo = self._enviar(
            self.contrato.functions.submitInference(
                identificador,
                bytes_de_la_prueba,
                instancias,
                round(confidence * PUNTOS_BASICOS),
                amount_above_threshold,
                rules_consistent,
            ),
            gas=GAS_DE_ENVIO_DE_INFERENCIA,
        )
        estado, causa = self._transicion_del_recibo(recibo)
        return ChainReceipt(
            status=estado,
            cause=causa,
            transaction_hash="0x" + recibo["transactionHash"].hex(),
            block_number=recibo["blockNumber"],
            proof_hash=proof.proof_hash,
            extra={"gas_used": recibo["gasUsed"]},
        )

    def settle(self, claim_id: str, *, automatic: bool) -> ChainReceipt:
        recibo = self._enviar(
            self.contrato.functions.settle(
                identificador_on_chain(claim_id), automatic
            )
        )
        estado, causa = self._transicion_del_recibo(recibo)
        return ChainReceipt(
            status=estado,
            cause=causa,
            transaction_hash="0x" + recibo["transactionHash"].hex(),
            block_number=recibo["blockNumber"],
            extra={"gas_used": recibo["gasUsed"]},
        )

    def resolve(
        self, claim_id: str, *, favorable: bool, decision_hash: str
    ) -> ChainReceipt:
        """Asienta la decisión humana. A la cadena va el digesto y no el texto.

        La identidad de quien revisó, la marca temporal y la justificación
        quedan fuera (RF-39, RNF-24); el contrato exige que el digesto no sea
        cero, porque una resolución sin nada que la respalde no es auditable.
        """
        recibo = self._enviar(
            self.contrato.functions.resolve(
                identificador_on_chain(claim_id),
                favorable,
                bytes.fromhex(decision_hash.removeprefix("0x")),
            )
        )
        estado, causa = self._transicion_del_recibo(recibo)
        return ChainReceipt(
            status=estado,
            cause=causa,
            transaction_hash="0x" + recibo["transactionHash"].hex(),
            block_number=recibo["blockNumber"],
            extra={"gas_used": recibo["gasUsed"], "decision_hash": decision_hash},
        )


# ---------------------------------------------------------------------------
# Selección
# ---------------------------------------------------------------------------

# Artefacto que deja `scripts/deploy-local.js` en `contracts/`. No se versiona:
# el nodo de Hardhat no persiste estado entre arranques.
DESPLIEGUE_POR_DEFECTO = str(
    Path(__file__).resolve().parents[3] / "contracts" / "despliegue-local.json"
)


def _validate_artifact(data: object) -> dict:
    """Rechaza registros pendientes o incompletos sin tocar la RPC."""
    def address(value):
        return isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is not None

    def digest(value):
        return isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", value) is not None
    invalid = ValueError("El artefacto de despliegue está incompleto o mal formado.")
    if not isinstance(data, dict) or "estado" in data:
        raise invalid
    network = data.get("red")
    if not isinstance(network, dict) or not isinstance(network.get("nombre"), str) or not network["nombre"] or type(network.get("chainId")) is not int or network["chainId"] <= 0:
        raise invalid
    if not all(address(data.get(key)) for key in ("registro", "oraculo", "verificador")):
        raise invalid
    if not isinstance(data.get("abi"), list):
        raise invalid
    threshold = data.get("umbralDeDerivacionBps")
    bytecode = data.get("bytecodeDelVerificador")
    if type(threshold) is not int or not 1 <= threshold <= PUNTOS_BASICOS or not isinstance(bytecode, dict) or type(bytecode.get("bytes")) is not int or bytecode["bytes"] <= 0 or type(bytecode.get("limiteEip170")) is not int or bytecode["limiteEip170"] <= 0:
        raise invalid
    if "codigo" in data:
        code = data["codigo"]
        if not isinstance(code, dict) or not all(digest(code.get(key)) for key in (
            "verificadorHash", "verificadorEsperadoHash", "registroHash", "registroEsperadoHash"
        )) or type(code.get("registroBytes")) is not int or code["registroBytes"] <= 0:
            raise invalid
        if (code["verificadorHash"].lower() != code["verificadorEsperadoHash"].lower()
                or code["registroHash"].lower() != code["registroEsperadoHash"].lower()):
            raise invalid
    return data


def build_chain_client(*, derivation_threshold, policy_mode):
    """Elige la implementación por entorno.

    `CLIENTE_CADENA=web3` no cae al simulador si algo falta: levanta. Caer en
    silencio dejaría creer que hay una cadena detrás de una traza que no la
    tiene, y esa confusión es exactamente la que el proyecto no puede permitirse.
    """
    elegido = os.environ.get("CLIENTE_CADENA", "simulado").lower()
    if os.environ.get("RAILWAY_ENVIRONMENT_NAME", "").strip().lower() == "production" and elegido != "web3":
        raise ValueError("En producción, CLIENTE_CADENA debe ser web3.")
    if elegido != "web3":
        return SimulatedChainClient(
            derivation_threshold=derivation_threshold, policy_mode=policy_mode
        )

    despliegue = os.environ.get("CADENA_DESPLIEGUE", DESPLIEGUE_POR_DEFECTO)
    if not os.path.exists(despliegue):
        raise FileNotFoundError(
            f"No está el artefacto de despliegue en {despliegue}. Correr "
            "`npx hardhat run scripts/deploy-local.js --network localhost` en "
            "PFI-Contracts, con el nodo levantado."
        )
    try:
        with open(despliegue) as archivo:
            datos = _validate_artifact(json.load(archivo))
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError("El artefacto de despliegue está incompleto o mal formado.") from None

    clave = os.environ.get("CADENA_CLAVE_ORACULO")
    if not clave:
        raise ValueError(
            "Falta CADENA_CLAVE_ORACULO. La clave no viaja en el artefacto de "
            "despliegue ni en el repositorio (RNF-22): se pasa por entorno."
        )

    return Web3ChainClient(
        rpc_url=os.environ.get("CADENA_RPC", "http://127.0.0.1:8545"),
        contract_address=datos["registro"],
        oracle_private_key=clave,
        abi=datos["abi"],
        expected_artifact=datos,
    )

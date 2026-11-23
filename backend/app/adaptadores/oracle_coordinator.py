"""Coordinador durable de operaciones y wallet del oráculo (ADR-018).

Garantiza:
1. Intención inmutable persistida en `oracle_operations` antes de cualquier RPC.
2. Exclusión durable de la wallet en `oracle_wallets` combinada con un advisory lock
   de sesión de PostgreSQL por red y dirección de wallet.
3. No mantener transacciones abiertas de base de datos durante llamadas RPC externas.
4. Persistencia obligatoria de nonce, bytes firmados (`raw_signed_transaction`) y hash
   calculado antes del broadcast.
5. Aplicación atómica del recibo minado al dominio (transición, estado del reclamo,
   enlace de revisión) y liberación de la wallet.
6. En caso de timeout o incertidumbre de RPC, conservación de la reserva activa
   (`oracle_wallets.active_operation_id`) y estado `incierta`, bloqueando efectos
   incompatibles y prohibiendo reenvíos a ciegas con nuevo nonce.
7. Reconciliación acotada capaz de recuperar efectos confirmados tras caídas o
   respuestas perdidas.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from web3.exceptions import ContractLogicError

from app.adaptadores.chain import (
    CAUSAS_DEL_CONTRATO,
    ESTADOS_DEL_CONTRATO,
    GAS_DE_ENVIO_DE_INFERENCIA,
    SimulatedChainClient,
    Web3ChainClient,
    identificador_on_chain,
)
from app.adaptadores.database import session_scope
from app.adaptadores.models import (
    Claim,
    Configuration,
    Job,
    OracleOperation,
    OracleWallet,
    Review,
    Transition,
)
from app.dominio.ports import ChainClient, ChainReceipt, ProofResult
from app.dominio.state_machine import (
    resolution_cause,
    settlement_cause,
    validate_transition,
)
from app.dominio.vocabulary import (
    ClaimStatus,
    OracleOperationStatus,
    OracleOperationType,
    TransitionCause,
)

logger = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(timezone.utc)


def wallet_lock_key(chain_id: int, sender_address: str) -> int:
    """Genera una clave determinista de 64 bits con signo para pg_advisory_lock."""
    digest = hashlib.sha256(f"{chain_id}:{sender_address.lower()}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class OracleCoordinatorError(Exception):
    """Error base de coordinación del oráculo."""


class WalletBlockedError(OracleCoordinatorError):
    """La wallet se encuentra reservada por una operación activa o incierta."""

    def __init__(
        self,
        active_operation_id: UUID | str | None,
        last_error_code: str | None = None,
        message: str | None = None,
    ) -> None:
        self.active_operation_id = str(active_operation_id) if active_operation_id else None
        self.last_error_code = last_error_code
        msg = message or f"Wallet bloqueada por operación {self.active_operation_id}"
        super().__init__(msg)


class OracleOperationUncertainError(TimeoutError, OracleCoordinatorError):
    """El resultado de la operación en la cadena es incierto."""

    def __init__(
        self,
        operation_id: UUID | str,
        last_error_code: str = "RPC_TIMEOUT",
        message: str | None = None,
    ) -> None:
        self.operation_id = str(operation_id)
        self.last_error_code = last_error_code
        msg = message or f"Operación {self.operation_id} quedó en estado incierto ({last_error_code})"
        super().__init__(msg)


class OracleOperationRevertedError(RuntimeError, OracleCoordinatorError):
    """La transacción minó en la cadena pero fue revertida."""
    def __init__(
        self,
        operation_id: UUID | str,
        transaction_hash: str | None = None,
        block_number: int | None = None,
        message: str | None = None,
    ) -> None:
        self.operation_id = str(operation_id)
        self.transaction_hash = transaction_hash
        self.block_number = block_number
        msg = message or f"La transacción {transaction_hash} revirtió en el bloque {block_number}"
        super().__init__(msg)


class OracleOperationRejectedError(RuntimeError, OracleCoordinatorError):
    """El contrato rechazó la construcción antes de firmar o enviar."""

    def __init__(self, operation_id: UUID | str) -> None:
        self.operation_id = str(operation_id)
        super().__init__(f"Operación {self.operation_id} rechazada antes de firmar y enviar.")


class OracleCoordinator:
    """Coordina envíos a la cadena con exclusión de wallet y persistencia previa."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        chain_client: ChainClient | Web3ChainClient | SimulatedChainClient,
        *,
        chain_id: int | None = None,
        sender_address: str | None = None,
        contract_address: str | None = None,
        oracle_private_key: str | None = None,
        before_broadcast_hook: Callable[[OracleOperation], None] | None = None,
        after_broadcast_hook: Callable[[OracleOperation], None] | None = None,
        before_confirm_hook: Callable[[OracleOperation, dict[str, Any]], None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.chain_client = chain_client
        self.engine: Engine = session_factory.kw["bind"]

        self.before_broadcast_hook = before_broadcast_hook
        self.after_broadcast_hook = after_broadcast_hook
        self.before_confirm_hook = before_confirm_hook

        if isinstance(chain_client, Web3ChainClient):
            self.is_web3 = True
            self.w3 = chain_client.w3
            self.cuenta = chain_client.cuenta
            self.contrato = chain_client.contrato
            self.chain_id = chain_id or self.w3.eth.chain_id
            self.sender_address = sender_address or self.cuenta.address
            self.contract_address = contract_address or self.contrato.address
            self.oracle_private_key = oracle_private_key or self.cuenta.key.hex()
            self.timeout = getattr(chain_client, "timeout", 120)
        else:
            self.is_web3 = False
            self.chain_id = chain_id or 31337
            self.sender_address = sender_address or "0x1111111111111111111111111111111111111111"
            self.contract_address = contract_address or "0x2222222222222222222222222222222222222222"
            self.oracle_private_key = oracle_private_key or "0x" + "a" * 64
            self.timeout = 120

    # -----------------------------------------------------------------------
    # Bloqueos consultivos de sesión (PostgreSQL)
    # -----------------------------------------------------------------------

    def _acquire_session_lock(self, lock_key: int):
        """Obtiene el lock consultivo de sesión sobre una conexión dedicada en autocommit."""
        conn = self.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
            return conn
        except Exception:
            conn.close()
            raise

    def _release_session_lock(self, conn, lock_key: int) -> None:
        """Libera el lock consultivo y cierra la conexión dedicada."""
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
        except Exception as err:
            logger.warning("Failed to release advisory lock %s: %s", lock_key, type(err).__name__)
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Deduplicación y claves de operación
    # -----------------------------------------------------------------------

    def build_operation_key(
        self,
        *,
        operation_type: str,
        claim_number: str,
        review_id: int | None = None,
    ) -> str:
        """Clave canónica inmutable: red:contrato:reclamo:efecto."""
        base = f"{self.chain_id}:{self.contract_address.lower()}:{claim_number}:{operation_type}"
        if operation_type == OracleOperationType.RESOLVE.value and review_id is not None:
            return f"{base}:{review_id}"
        return base

    # -----------------------------------------------------------------------
    # Preparación e intención previa
    # -----------------------------------------------------------------------

    def prepare_operation_in_session(
        self,
        session: Session,
        *,
        claim_id: int,
        operation_type: str,
        payload: dict[str, Any],
        configuration_id: int,
        job_id: int | None = None,
    ) -> OracleOperation:
        """Commit a human decision, frozen intent and wallet reservation together."""
        number = payload["number"]
        key = self.build_operation_key(
            operation_type=operation_type,
            claim_number=number,
            review_id=payload.get("review_id"),
        )
        existing = session.scalars(
            select(OracleOperation).where(OracleOperation.operation_key == key).with_for_update()
        ).first()
        if existing is not None:
            if existing.payload != payload or existing.claim_id != claim_id:
                raise WalletBlockedError(existing.id, "INTENT_CONFLICT")
            if existing.status == OracleOperationStatus.FALLIDA_PRE_ENVIO.value:
                raise OracleOperationRejectedError(existing.id)
            op = existing
        else:
            op = OracleOperation(
                id=uuid4(), operation_key=key,
                claim_id=claim_id, job_id=job_id,
                configuration_id=configuration_id, operation=operation_type,
                payload=dict(payload), chain_id=self.chain_id,
                sender_address=self.sender_address,
                contract_address=self.contract_address,
                status=OracleOperationStatus.PREPARADA.value,
            )
            session.add(op)
            session.flush()
        wallet = session.get(
            OracleWallet,
            {"chain_id": self.chain_id, "sender_address": self.sender_address},
            with_for_update=True,
        )
        if wallet is None:
            session.add(OracleWallet(
                chain_id=self.chain_id, sender_address=self.sender_address,
                active_operation_id=op.id,
            ))
        elif wallet.active_operation_id not in (None, op.id):
            raise WalletBlockedError(wallet.active_operation_id)
        else:
            wallet.active_operation_id = op.id
            wallet.updated_at = now()
        claim = session.get(Claim, claim_id)
        claim.pending_chain_action = operation_type
        return op

    def _get_or_create_operation(
        self,
        *,
        claim_id: int,
        operation_type: str,
        payload: dict[str, Any],
        configuration_id: int,
        job_id: int | None,
        operation_key: str,
    ) -> OracleOperation:
        with session_scope(self.session_factory) as session:
            op = session.scalars(
                select(OracleOperation).where(OracleOperation.operation_key == operation_key)
            ).first()
            if op is not None:
                return op

            op = OracleOperation(
                id=uuid4(),
                operation_key=operation_key,
                claim_id=claim_id,
                job_id=job_id,
                configuration_id=configuration_id,
                operation=operation_type,
                payload=payload,
                chain_id=self.chain_id,
                sender_address=self.sender_address,
                contract_address=self.contract_address,
                status=OracleOperationStatus.PREPARADA.value,
            )
            session.add(op)
            session.flush()
            # Mapeo de compatibilidad con pending_chain_action
            claim = session.get(Claim, claim_id)
            if claim is not None:
                claim.pending_chain_action = operation_type
            return op

    def _reserve_wallet(self, operation_id: UUID) -> None:
        """Asienta la exclusión de la wallet en oracle_wallets."""
        with session_scope(self.session_factory) as session:
            wallet = session.get(
                OracleWallet,
                {"chain_id": self.chain_id, "sender_address": self.sender_address},
                with_for_update=True,
            )
            if wallet is None:
                wallet = OracleWallet(
                    chain_id=self.chain_id,
                    sender_address=self.sender_address,
                    active_operation_id=operation_id,
                )
                session.add(wallet)
            else:
                if (
                    wallet.active_operation_id is not None
                    and wallet.active_operation_id != operation_id
                ):
                    # Intentar reconciliar la operación activa antes de rechazar
                    active_op = session.get(OracleOperation, wallet.active_operation_id)
                    last_code = active_op.last_error_code if active_op else None
                    raise WalletBlockedError(wallet.active_operation_id, last_code)
                wallet.active_operation_id = operation_id
                wallet.updated_at = now()

    def _clear_wallet(self, operation_id: UUID) -> None:
        """Libera la wallet en oracle_wallets si pertenecía a esta operación."""
        with session_scope(self.session_factory) as session:
            wallet = session.get(
                OracleWallet,
                {"chain_id": self.chain_id, "sender_address": self.sender_address},
                with_for_update=True,
            )
            if wallet is not None and wallet.active_operation_id == operation_id:
                wallet.active_operation_id = None
                wallet.updated_at = now()

    # -----------------------------------------------------------------------
    # Construcción de calldata según operación
    # -----------------------------------------------------------------------

    def _build_contract_function(self, operation_type: str, payload: dict[str, Any]):
        if not self.is_web3:
            return None
        claim_number = payload["number"]
        identificador = identificador_on_chain(claim_number)

        if operation_type == OracleOperationType.REGISTER_CLAIM.value:
            evidence_id = payload["evidence_id"]
            return self.contrato.functions.registerClaim(
                identificador, identificador_on_chain(evidence_id)
            )

        if operation_type == OracleOperationType.SUBMIT_INFERENCE.value:
            bytes_prueba = bytes.fromhex(payload["hex_proof"])
            instancias = payload["instances"]
            confianza = payload["confidence_bps"]
            sobre_monto = payload["amount_above_threshold"]
            reglas = payload["rules_consistent"]
            return self.contrato.functions.submitInference(
                identificador, bytes_prueba, instancias, confianza, sobre_monto, reglas
            )

        if operation_type == OracleOperationType.SETTLE.value:
            automatic = payload.get("automatic", True)
            return self.contrato.functions.settle(identificador, automatic)

        if operation_type == OracleOperationType.RESOLVE.value:
            favorable = payload["favorable"]
            decision_hash = payload["decision_hash"]
            raw_hash = bytes.fromhex(decision_hash.removeprefix("0x"))
            return self.contrato.functions.resolve(identificador, favorable, raw_hash)

        raise ValueError(f"Operación desconocida: {operation_type}")

    # -----------------------------------------------------------------------
    # Aplicación atómica de recibo minado a la base de dominio
    # -----------------------------------------------------------------------

    def _apply_receipt_to_domain(
        self,
        session: Session,
        operation: OracleOperation,
        receipt: ChainReceipt,
        block_number: int | None,
    ) -> None:
        claim = session.get(Claim, operation.claim_id, with_for_update=True)
        if claim is None:
            raise RuntimeError(f"Reclamo {operation.claim_id} no encontrado")

        op_type = operation.operation
        tx_hash = receipt.transaction_hash

        if op_type == OracleOperationType.REGISTER_CLAIM.value:
            entry = session.scalars(
                select(Transition)
                .where(Transition.claim_id == claim.id, Transition.from_status.is_(None))
                .order_by(Transition.id)
            ).first()
            if entry is not None:
                entry.transaction_hash = tx_hash
                entry.block_number = block_number
            claim.onchain_claim_id = identificador_on_chain(claim.tracking_number).hex()

        elif op_type == OracleOperationType.SUBMIT_INFERENCE.value:
            # Preservar VerificacionFallida como evento confirmado (RNF-17)
            session.add(
                Transition(
                    claim_id=claim.id,
                    configuration_id=operation.configuration_id,
                    from_status=claim.status,
                    to_status=receipt.status,
                    cause=receipt.cause,
                    proof_hash=receipt.proof_hash,
                    transaction_hash=tx_hash,
                    block_number=block_number,
                )
            )
            claim.status = receipt.status

        elif op_type == OracleOperationType.SETTLE.value:
            cause = receipt.cause or settlement_cause(automatic=operation.payload.get("automatic", True))
            session.add(
                Transition(
                    claim_id=claim.id,
                    configuration_id=operation.configuration_id,
                    from_status=claim.status,
                    to_status=ClaimStatus.APROBADO,
                    cause=cause,
                    transaction_hash=tx_hash,
                    block_number=block_number,
                )
            )
            claim.status = ClaimStatus.APROBADO

        elif op_type == OracleOperationType.RESOLVE.value:
            favorable = operation.payload.get("favorable", True)
            cause = receipt.cause or resolution_cause(favorable=favorable)
            to_status = receipt.status or (ClaimStatus.APROBADO if favorable else ClaimStatus.RECHAZADO)
            trans = Transition(
                claim_id=claim.id,
                configuration_id=operation.configuration_id,
                from_status=claim.status,
                to_status=to_status,
                cause=cause,
                transaction_hash=tx_hash,
                block_number=block_number,
            )
            session.add(trans)
            session.flush()
            claim.status = to_status

            review_id = operation.payload.get("review_id")
            if review_id:
                review = session.get(Review, review_id)
                if review is not None:
                    review.transition_id = trans.id

        claim.pending_chain_action = None

    # -----------------------------------------------------------------------
    # Enviar operación coordinada (Pipeline durable)
    # -----------------------------------------------------------------------

    def submit_operation(
        self,
        *,
        claim_id: int,
        operation_type: str,
        payload: dict[str, Any],
        configuration_id: int,
        job_id: int | None = None,
        operation_key: str | None = None,
    ) -> ChainReceipt:
        """Punto de entrada principal para los cinco emisores."""
        number = payload.get("number")
        if not number:
            with session_scope(self.session_factory) as s:
                c = s.get(Claim, claim_id)
                number = c.tracking_number if c else str(claim_id)
                payload["number"] = number

        if not operation_key:
            review_id = payload.get("review_id")
            operation_key = self.build_operation_key(
                operation_type=operation_type,
                claim_number=number,
                review_id=review_id,
            )

        # 1. Registrar intención inmutable antes de cualquier I/O
        op_row = self._get_or_create_operation(
            claim_id=claim_id,
            operation_type=operation_type,
            payload=payload,
            configuration_id=configuration_id,
            job_id=job_id,
            operation_key=operation_key,
        )
        op_id = op_row.id
        if op_row.status == OracleOperationStatus.PREPARADA.value and (
            op_row.claim_id != claim_id
            or op_row.configuration_id != configuration_id
            or op_row.operation != operation_type
            or op_row.payload != payload
        ):
            raise WalletBlockedError(op_id, "INTENT_CONFLICT")
        payload = op_row.payload

        # Reconstruct only the persisted domain outcome, never a default approval.
        if op_row.status == OracleOperationStatus.CONFIRMADA.value:
            if operation_type == OracleOperationType.REGISTER_CLAIM.value:
                return ChainReceipt(
                    status=ClaimStatus.REGISTRADO, cause=None,
                    transaction_hash=op_row.transaction_hash if self.is_web3 else None,
                    block_number=op_row.block_number,
                )
            with session_scope(self.session_factory) as session:
                transitions = select(Transition).where(
                    Transition.claim_id == op_row.claim_id,
                    Transition.configuration_id == op_row.configuration_id,
                )
                if self.is_web3:
                    transitions = transitions.where(
                        Transition.transaction_hash == op_row.transaction_hash
                    )
                elif operation_type == OracleOperationType.SUBMIT_INFERENCE.value:
                    transitions = transitions.where(
                        Transition.proof_hash == op_row.payload.get("proof_hash")
                    )
                else:
                    transitions = transitions.where(
                        Transition.cause == (
                            settlement_cause(automatic=op_row.payload["automatic"])
                            if operation_type == OracleOperationType.SETTLE.value
                            else resolution_cause(favorable=op_row.payload["favorable"])
                        )
                    )
                transition = session.scalars(transitions.order_by(Transition.id.desc())).first()
                if transition is None:
                    raise OracleCoordinatorError("Confirmed operation has no domain transition")
                return ChainReceipt(
                    status=transition.to_status,
                    cause=transition.cause,
                    transaction_hash=transition.transaction_hash,
                    block_number=transition.block_number,
                    proof_hash=transition.proof_hash,
                )

        if op_row.status == OracleOperationStatus.FALLIDA_PRE_ENVIO.value:
            raise OracleOperationRejectedError(op_id)

        # A persisted attempt must be reconciled before the same effect can run again.
        if op_row.status != OracleOperationStatus.PREPARADA.value:
            raise WalletBlockedError(op_id, op_row.last_error_code)

        # 2. Lock consultivo de sesión sobre conexión dedicada
        lock_k = wallet_lock_key(self.chain_id, self.sender_address)
        lock_conn = self._acquire_session_lock(lock_k)

        try:
            # La instantánea previa al lock puede haber sido rechazada por otro emisor.
            with session_scope(self.session_factory) as session:
                current = session.get(OracleOperation, op_id)
                if current.status == OracleOperationStatus.FALLIDA_PRE_ENVIO.value:
                    raise OracleOperationRejectedError(op_id)
            # 3. Exclusión durable en oracle_wallets
            self._reserve_wallet(op_id)

            # 4. Construcción, firma y persistencia de bytes antes del broadcast
            if self.is_web3:
                receipt = self._execute_web3_operation(op_id, operation_type, payload)
            else:
                receipt = self._execute_simulated_operation(op_id, operation_type, payload)

            return receipt

        finally:
            self._release_session_lock(lock_conn, lock_k)

    def _reject_before_signing(self, operation_id: UUID) -> OracleOperation:
        """Cierra el rechazo probado antes del envío, bajo el advisory lock de la wallet."""
        with session_scope(self.session_factory) as session:
            op = session.get(OracleOperation, operation_id, with_for_update=True)
            if op.status != OracleOperationStatus.PREPARADA.value or any(
                value is not None for value in (
                    op.nonce, op.raw_signed_transaction, op.transaction_hash, op.broadcast_at,
                )
            ):
                raise OracleCoordinatorError("La operación no acredita ausencia de firma y envío")
            op.status = OracleOperationStatus.FALLIDA_PRE_ENVIO.value
            op.last_error_code = "CONTRACT_REJECTED_BEFORE_SIGNING"
            op.updated_at = now()
            wallet = session.get(OracleWallet, {
                "chain_id": op.chain_id, "sender_address": op.sender_address,
            }, with_for_update=True)
            if wallet is not None and wallet.active_operation_id == op.id:
                wallet.active_operation_id = None
                wallet.updated_at = now()
            claim = session.get(Claim, op.claim_id, with_for_update=True)
            if claim is not None and claim.pending_chain_action == op.operation:
                claim.pending_chain_action = None
            return op

    def _mark_uncertain(self, operation_id: UUID, code: str) -> OracleOperation:
        with session_scope(self.session_factory) as session:
            op = session.get(OracleOperation, operation_id)
            op.status = OracleOperationStatus.INCIERTA.value
            op.last_error_code = code
            op.updated_at = now()
            return op

    def _verify_transaction(self, operation: OracleOperation, transaction: Any) -> None:
        """Compare a node's transaction with the immutable signed intent."""
        if not operation.raw_signed_transaction or operation.nonce is None or not operation.transaction_hash:
            raise ValueError("Missing durable signed transaction")
        if self.w3.eth.chain_id != operation.chain_id:
            raise ValueError("Chain identity differs")
        signed_hash = self.w3.keccak(operation.raw_signed_transaction)
        if bytes(signed_hash) != bytes.fromhex(operation.transaction_hash.removeprefix("0x")):
            raise ValueError("Signed bytes do not match transaction hash")
        expected_input = self._build_contract_function(operation.operation, operation.payload)
        if (
            bytes(transaction["hash"]) != bytes(signed_hash)
            or str(transaction["from"]).lower() != operation.sender_address.lower()
            or str(transaction["to"]).lower() != operation.contract_address.lower()
            or transaction["nonce"] != operation.nonce
            or transaction["chainId"] != operation.chain_id
            or self.w3.to_hex(transaction["input"]).lower() != expected_input._encode_transaction_data().lower()
        ):
            raise ValueError("Transaction identity differs from frozen intent")

    def _verify_mined_receipt(
        self, operation: OracleOperation, receipt: Any
    ) -> tuple[ClaimStatus | None, TransitionCause | None]:
        """Accept only the signed transaction and its expected claim event."""
        transaction = self.w3.eth.get_transaction(operation.transaction_hash)
        self._verify_transaction(operation, transaction)
        if (
            bytes(receipt["transactionHash"]) != bytes(transaction["hash"])
            or str(receipt["from"]).lower() != operation.sender_address.lower()
            or str(receipt["to"]).lower() != operation.contract_address.lower()
            or receipt.get("blockNumber") is None
        ):
            raise ValueError("Receipt identity differs from signed transaction")
        if receipt.get("status") != 1:
            return None, None

        from web3.logs import DISCARD

        claim_id = identificador_on_chain(operation.payload["number"])
        event_name = (
            "ClaimRegistered" if operation.operation == OracleOperationType.REGISTER_CLAIM.value
            else "StateTransition"
        )
        events = getattr(self.contrato.events, event_name)().process_receipt(receipt, errors=DISCARD)
        matches = [event for event in events if bytes(event["args"]["claimId"]) == claim_id]
        if len(matches) != 1:
            raise ValueError("Expected claim event is missing or ambiguous")
        if operation.operation == OracleOperationType.REGISTER_CLAIM.value:
            if bytes(matches[0]["args"]["evidenceId"]) != identificador_on_chain(operation.payload["evidence_id"]):
                raise ValueError("Registered evidence does not match intent")
            return ClaimStatus.REGISTRADO, None

        args = matches[0]["args"]
        state = ESTADOS_DEL_CONTRATO[args["to"]]
        cause = CAUSAS_DEL_CONTRATO[args["cause"]]
        if operation.operation == OracleOperationType.SUBMIT_INFERENCE.value:
            if state not in (
                ClaimStatus.APROBABLE, ClaimStatus.REQUIERE_REVISION,
                ClaimStatus.INCONSISTENCIA_EVIDENCIA, ClaimStatus.VERIFICACION_FALLIDA,
            ) or bytes(args["proofHash"]) != bytes(self.w3.keccak(bytes.fromhex(operation.payload["hex_proof"]))):
                raise ValueError("Inference event does not match proof intent")
        elif operation.operation == OracleOperationType.SETTLE.value:
            if state != ClaimStatus.APROBADO or cause != settlement_cause(automatic=operation.payload["automatic"]):
                raise ValueError("Settlement event does not match intent")
        elif operation.operation == OracleOperationType.RESOLVE.value:
            favorable = operation.payload["favorable"]
            if state != (ClaimStatus.APROBADO if favorable else ClaimStatus.RECHAZADO) or cause != resolution_cause(favorable=favorable):
                raise ValueError("Resolution transition differs from intent")
            decisions = self.contrato.events.ResolutionRecorded().process_receipt(receipt, errors=DISCARD)
            matching = [event for event in decisions if bytes(event["args"]["claimId"]) == claim_id]
            if len(matching) != 1 or matching[0]["args"]["favorable"] != favorable or bytes(matching[0]["args"]["decisionHash"]) != bytes.fromhex(operation.payload["decision_hash"].removeprefix("0x")):
                raise ValueError("Resolution decision event differs from intent")
        return state, cause

    def _execute_web3_operation(
        self,
        op_id: UUID,
        operation_type: str,
        payload: dict[str, Any],
    ) -> ChainReceipt:
        # Releer operación
        with session_scope(self.session_factory) as session:
            op = session.get(OracleOperation, op_id)
            raw_tx = op.raw_signed_transaction
            tx_hash = op.transaction_hash
            nonce = op.nonce
            status = op.status

        # Si aún no está firmada, firmar y persistir antes de broadcast
        if not raw_tx:
            func = self._build_contract_function(operation_type, payload)
            nonce = self.w3.eth.get_transaction_count(self.sender_address)
            gas = GAS_DE_ENVIO_DE_INFERENCIA if operation_type == OracleOperationType.SUBMIT_INFERENCE.value else None
            tx_params = {
                "from": self.sender_address,
                "nonce": nonce,
                "chainId": self.chain_id,
            }
            if gas is not None:
                tx_params["gas"] = gas

            try:
                tx_built = func.build_transaction(tx_params)
            except ContractLogicError:
                # El rechazo de estimación ocurrió antes de crear bytes firmados.
                self._reject_before_signing(op_id)
                raise OracleOperationRejectedError(op_id) from None
            signed = self.cuenta.sign_transaction(tx_built)
            raw_tx = bytes(signed.raw_transaction)
            tx_hash = "0x" + signed.hash.hex()

            with session_scope(self.session_factory) as session:
                op = session.get(OracleOperation, op_id)
                op.nonce = nonce
                op.raw_signed_transaction = raw_tx
                op.transaction_hash = tx_hash
                op.status = OracleOperationStatus.FIRMADA.value
                op.updated_at = now()

        # Hook antes de broadcast (seam para simular caída antes de red)
        if self.before_broadcast_hook:
            with session_scope(self.session_factory) as session:
                self.before_broadcast_hook(session.get(OracleOperation, op_id))

        # Broadcast
        with session_scope(self.session_factory) as session:
            op = session.get(OracleOperation, op_id)
            op.status = OracleOperationStatus.ENVIADA.value
            op.broadcast_at = now()
            op.updated_at = now()

        try:
            self.w3.eth.send_raw_transaction(raw_tx)
        except Exception as err:
            err_msg = str(err).lower()
            if "already known" not in err_msg and "nonce too low" not in err_msg:
                with session_scope(self.session_factory) as session:
                    op = session.get(OracleOperation, op_id)
                    op.status = OracleOperationStatus.INCIERTA.value
                    op.last_error_code = "BROADCAST_FAILED"
                    op.updated_at = now()
                raise OracleOperationUncertainError(op_id, "BROADCAST_FAILED") from None

        # Hook posterior al broadcast
        if self.after_broadcast_hook:
            with session_scope(self.session_factory) as session:
                self.after_broadcast_hook(session.get(OracleOperation, op_id))

        # Esperar recibo
        try:
            raw_receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.timeout)
        except Exception as err:
            with session_scope(self.session_factory) as session:
                op = session.get(OracleOperation, op_id)
                op.status = OracleOperationStatus.INCIERTA.value
                op.last_error_code = "RPC_TIMEOUT"
                op.updated_at = now()
            raise OracleOperationUncertainError(op_id, "RPC_TIMEOUT") from None

        try:
            with session_scope(self.session_factory) as session:
                verified_op = session.get(OracleOperation, op_id)
                status_val, cause_val = self._verify_mined_receipt(verified_op, raw_receipt)
        except Exception as err:
            self._mark_uncertain(op_id, "RECEIPT_UNVERIFIED")
            raise OracleOperationUncertainError(op_id, "RECEIPT_UNVERIFIED") from err

        # Verificar revert
        if raw_receipt.get("status") != 1:
            with session_scope(self.session_factory) as session:
                op = session.get(OracleOperation, op_id)
                op.status = OracleOperationStatus.REVERTIDA.value
                op.block_number = raw_receipt.get("blockNumber")
                op.last_error_code = "TRANSACTION_REVERTED"
                op.updated_at = now()
            self._clear_wallet(op_id)
            raise OracleOperationRevertedError(
                op_id, tx_hash, raw_receipt.get("blockNumber")
            )

        # Hook antes de confirmación atómica
        if self.before_confirm_hook:
            with session_scope(self.session_factory) as session:
                self.before_confirm_hook(session.get(OracleOperation, op_id), raw_receipt)

        chain_receipt = ChainReceipt(
            status=status_val,
            cause=cause_val,
            transaction_hash=tx_hash,
            block_number=raw_receipt["blockNumber"],
            proof_hash=payload.get("proof_hash"),
            extra={"gas_used": raw_receipt.get("gasUsed")},
        )

        # Aplicar atómicamente a la base de dominio y liberar wallet
        with session_scope(self.session_factory) as session:
            op = session.get(OracleOperation, op_id)
            op.status = OracleOperationStatus.CONFIRMADA.value
            op.confirmed_at = now()
            op.block_number = raw_receipt["blockNumber"]
            op.last_error_code = None
            op.updated_at = now()
            self._apply_receipt_to_domain(
                session, op, chain_receipt, raw_receipt["blockNumber"]
            )
            # Liberar wallet
            wallet = session.get(
                OracleWallet,
                {"chain_id": self.chain_id, "sender_address": self.sender_address},
                with_for_update=True,
            )
            if wallet is not None and wallet.active_operation_id == op_id:
                wallet.active_operation_id = None
                wallet.updated_at = now()

        return chain_receipt

    def _execute_simulated_operation(
        self,
        op_id: UUID,
        operation_type: str,
        payload: dict[str, Any],
    ) -> ChainReceipt:
        # Distinct diagnostic identity for a simulated operation; never an on-chain receipt.
        raw_tx = op_id.bytes
        tx_hash = "0x" + hashlib.sha256(raw_tx).hexdigest()

        with session_scope(self.session_factory) as session:
            op = session.get(OracleOperation, op_id)
            if not op.raw_signed_transaction:
                last_nonce = session.scalar(
                    select(func.max(OracleOperation.nonce)).where(
                        OracleOperation.chain_id == self.chain_id,
                        OracleOperation.sender_address == self.sender_address,
                    )
                )
                op.nonce = (last_nonce or 0) + 1
                op.raw_signed_transaction = raw_tx
                op.transaction_hash = tx_hash
                op.status = OracleOperationStatus.FIRMADA.value
                op.updated_at = now()

        if self.before_broadcast_hook:
            with session_scope(self.session_factory) as session:
                self.before_broadcast_hook(session.get(OracleOperation, op_id))

        with session_scope(self.session_factory) as session:
            op = session.get(OracleOperation, op_id)
            op.status = OracleOperationStatus.ENVIADA.value
            op.broadcast_at = now()
            op.updated_at = now()

        # Invocar cliente simulado
        try:
            claim_number = payload["number"]
            if operation_type == OracleOperationType.REGISTER_CLAIM.value:
                sim_receipt = self.chain_client.register_claim(claim_number, payload["evidence_id"])
            elif operation_type == OracleOperationType.SUBMIT_INFERENCE.value:
                saved_proof = payload["proof_result"]
                proof_res = ProofResult(
                    proof=bytes.fromhex(saved_proof["proof_hex"]),
                    public_inputs=bytes.fromhex(saved_proof["public_inputs_hex"]),
                    proof_hash=saved_proof["proof_hash"],
                    circuit_parameters=saved_proof["circuit_parameters"],
                    declared_visibility=saved_proof["declared_visibility"],
                    verified_locally=saved_proof["verified_locally"],
                )
                sim_receipt = self.chain_client.submit_inference(
                    claim_number,
                    proof_res,
                    confidence=payload["confidence"],
                    amount_above_threshold=payload["amount_above_threshold"],
                    rules_consistent=payload["rules_consistent"],
                )
            elif operation_type == OracleOperationType.SETTLE.value:
                sim_receipt = self.chain_client.settle(claim_number, automatic=payload.get("automatic", True))
            elif operation_type == OracleOperationType.RESOLVE.value:
                sim_receipt = self.chain_client.resolve(
                    claim_number, favorable=payload["favorable"], decision_hash=payload["decision_hash"]
                )
            else:
                raise ValueError(operation_type)
        except Exception as err:
            with session_scope(self.session_factory) as session:
                op = session.get(OracleOperation, op_id)
                op.status = OracleOperationStatus.INCIERTA.value
                op.last_error_code = "SIMULATED_TIMEOUT"
                op.updated_at = now()
            raise OracleOperationUncertainError(op_id, "SIMULATED_TIMEOUT") from None

        if self.after_broadcast_hook:
            with session_scope(self.session_factory) as session:
                self.after_broadcast_hook(session.get(OracleOperation, op_id))

        receipt = ChainReceipt(
            status=sim_receipt.status,
            cause=sim_receipt.cause,
            transaction_hash=None,
            block_number=None,
            proof_hash=sim_receipt.proof_hash or payload.get("proof_hash"),
            extra=sim_receipt.extra,
        )

        if self.before_confirm_hook:
            with session_scope(self.session_factory) as session:
                self.before_confirm_hook(session.get(OracleOperation, op_id), {"status": 1, "blockNumber": None})

        with session_scope(self.session_factory) as session:
            op = session.get(OracleOperation, op_id)
            op.status = OracleOperationStatus.CONFIRMADA.value
            op.confirmed_at = now()
            op.block_number = None
            op.last_error_code = None
            op.updated_at = now()
            self._apply_receipt_to_domain(session, op, receipt, None)
            wallet = session.get(
                OracleWallet,
                {"chain_id": self.chain_id, "sender_address": self.sender_address},
                with_for_update=True,
            )
            if wallet is not None and wallet.active_operation_id == op_id:
                wallet.active_operation_id = None
                wallet.updated_at = now()

        return receipt

    # -----------------------------------------------------------------------
    # Reconciliación acotada
    # -----------------------------------------------------------------------

    def reconcile_operation(self, operation_id: UUID | str) -> OracleOperation:
        """Reconcilia una operación individual sin modificar nonce ni destino."""
        if isinstance(operation_id, str):
            operation_id = UUID(operation_id)

        lock_k = wallet_lock_key(self.chain_id, self.sender_address)
        lock_conn = self._acquire_session_lock(lock_k)

        try:
            with session_scope(self.session_factory) as session:
                op = session.get(OracleOperation, operation_id)
                if op is None:
                    raise RuntimeError(f"Operación {operation_id} no encontrada")

                # Si ya terminó, limpiar wallet si apuntaba a ella
                if op.status in (
                    OracleOperationStatus.CONFIRMADA.value,
                    OracleOperationStatus.REVERTIDA.value,
                    OracleOperationStatus.FALLIDA_PRE_ENVIO.value,
                ):
                    wallet = session.get(
                        OracleWallet,
                        {"chain_id": self.chain_id, "sender_address": self.sender_address},
                        with_for_update=True,
                    )
                    if wallet and wallet.active_operation_id == op.id:
                        wallet.active_operation_id = None
                        wallet.updated_at = now()
                    return op

                # Without signed bytes there was no broadcast. Keep the intent;
                # the bounded startup recovery can resume exactly this payload.
                if op.status == OracleOperationStatus.PREPARADA.value:
                    return op

                tx_hash = op.transaction_hash
                if not tx_hash:
                    return op

            # Consultar estado en el nodo
            if not self.is_web3:
                # A simulated timeout has no independently verifiable receipt.
                with session_scope(self.session_factory) as session:
                    op = session.get(OracleOperation, operation_id)
                    op.status = OracleOperationStatus.INCIERTA.value
                    op.last_error_code = "SIMULATED_RESULT_UNVERIFIABLE"
                    op.updated_at = now()
                    return op

            from web3.exceptions import TransactionNotFound

            try:
                raw_receipt = self.w3.eth.get_transaction_receipt(tx_hash)
            except TransactionNotFound:
                raw_receipt = None

            if raw_receipt is not None:
                try:
                    with session_scope(self.session_factory) as session:
                        candidate = session.get(OracleOperation, operation_id)
                        st_val, cs_val = self._verify_mined_receipt(candidate, raw_receipt)
                except Exception:
                    return self._mark_uncertain(operation_id, "RECEIPT_UNVERIFIED")
                is_success = raw_receipt.get("status") == 1
                with session_scope(self.session_factory) as session:
                    op = session.get(OracleOperation, operation_id)
                    block_num = raw_receipt.get("blockNumber")
                    if is_success:
                        receipt = ChainReceipt(
                            status=st_val,
                            cause=cs_val,
                            transaction_hash=tx_hash,
                            block_number=block_num,
                            proof_hash=op.payload.get("proof_hash"),
                        )
                        op.status = OracleOperationStatus.CONFIRMADA.value
                        op.confirmed_at = now()
                        op.block_number = block_num
                        op.last_error_code = None
                        op.updated_at = now()
                        self._apply_receipt_to_domain(session, op, receipt, block_num)
                    else:
                        op.status = OracleOperationStatus.REVERTIDA.value
                        op.block_number = block_num
                        op.last_error_code = "TRANSACTION_REVERTED"
                        op.updated_at = now()

                    wallet = session.get(
                        OracleWallet,
                        {"chain_id": self.chain_id, "sender_address": self.sender_address},
                        with_for_update=True,
                    )
                    if wallet and wallet.active_operation_id == op.id:
                        wallet.active_operation_id = None
                        wallet.updated_at = now()
                    return op

            # Neither an absent receipt nor an absent mempool transaction proves failure.
            with session_scope(self.session_factory) as session:
                candidate = session.get(OracleOperation, operation_id)
            try:
                transaction = self.w3.eth.get_transaction(tx_hash)
            except TransactionNotFound:
                transaction = None
            except Exception:
                return self._mark_uncertain(operation_id, "RPC_UNAVAILABLE")
            if transaction is not None:
                try:
                    self._verify_transaction(candidate, transaction)
                except Exception:
                    return self._mark_uncertain(operation_id, "TRANSACTION_UNVERIFIED")
                return self._mark_uncertain(operation_id, "PENDING_RECEIPT")
            try:
                latest_nonce = self.w3.eth.get_transaction_count(self.sender_address, "latest")
                pending_nonce = self.w3.eth.get_transaction_count(self.sender_address, "pending")
            except Exception:
                return self._mark_uncertain(operation_id, "RPC_UNAVAILABLE")
            if candidate.nonce is not None and latest_nonce > candidate.nonce:
                return self._mark_uncertain(operation_id, "NONCE_ADVANCED_WITHOUT_EFFECT")
            if candidate.nonce is not None and pending_nonce > candidate.nonce:
                return self._mark_uncertain(operation_id, "PENDING_NONCE_UNVERIFIED")
            if candidate.status == OracleOperationStatus.FIRMADA.value:
                # ENVIADA is committed before every send; FIRMADA proves this
                # process never attempted broadcast. Resume the same signed bytes
                # only when neither a mined nor pending nonce competes for them.
                try:
                    raw = candidate.raw_signed_transaction
                    signed_hash = bytes(self.w3.keccak(raw))
                    if (
                        candidate.nonce != latest_nonce
                        or candidate.nonce != pending_nonce
                        or self.w3.eth.chain_id != candidate.chain_id
                        or signed_hash != bytes.fromhex(candidate.transaction_hash.removeprefix("0x"))
                        or self.w3.eth.account.recover_transaction(raw).lower()
                           != candidate.sender_address.lower()
                    ):
                        return self._mark_uncertain(operation_id, "SIGNED_INTENT_UNVERIFIED")
                except Exception:
                    return self._mark_uncertain(operation_id, "SIGNED_INTENT_UNVERIFIED")
                self._execute_web3_operation(operation_id, candidate.operation, candidate.payload)
                with session_scope(self.session_factory) as session:
                    return session.get(OracleOperation, operation_id)
            return self._mark_uncertain(operation_id, "TRANSACTION_ABSENCE_UNVERIFIED")

        finally:
            self._release_session_lock(lock_conn, lock_k)

    def reconcile_pending(self, *, max_operations: int = 50) -> list[OracleOperation]:
        """Reconciliación acotada automática ejecutada al arranque o por CLI."""
        with session_scope(self.session_factory) as session:
            # Operaciones pendientes de confirmación o inciertas
            stmt = (
                select(OracleOperation)
                .where(
                    OracleOperation.status.in_(
                        [
                            OracleOperationStatus.PREPARADA.value,
                            OracleOperationStatus.FIRMADA.value,
                            OracleOperationStatus.ENVIADA.value,
                            OracleOperationStatus.INCIERTA.value,
                        ]
                    )
                )
                .order_by(OracleOperation.created_at)
                .limit(max_operations)
            )
            pendientes = list(session.scalars(stmt))
            op_ids = [p.id for p in pendientes]

        reconciliadas = []
        for op_id in op_ids:
            try:
                with session_scope(self.session_factory) as session:
                    pending = session.get(OracleOperation, op_id)
                    prepared = pending.status == OracleOperationStatus.PREPARADA.value
                    if prepared:
                        frozen = (pending.claim_id, pending.operation, dict(pending.payload),
                                  pending.configuration_id, pending.job_id, pending.operation_key)
                if prepared:
                    self.submit_operation(
                        claim_id=frozen[0], operation_type=frozen[1], payload=frozen[2],
                        configuration_id=frozen[3], job_id=frozen[4], operation_key=frozen[5],
                    )
                    with session_scope(self.session_factory) as session:
                        res = session.get(OracleOperation, op_id)
                else:
                    res = self.reconcile_operation(op_id)
                reconciliadas.append(res)
            except Exception as err:
                logger.warning("Oracle reconciliation failed for %s: %s", op_id, type(err).__name__)
        return reconciliadas

    # -----------------------------------------------------------------------
    # Wrappers de alto nivel para los cinco emisores
    # -----------------------------------------------------------------------

    def register_claim(
        self,
        claim_id: int,
        number: str,
        evidence_id: str,
        configuration_id: int,
        job_id: int | None = None,
    ) -> ChainReceipt:
        payload = {
            "number": number,
            "evidence_id": evidence_id,
        }
        return self.submit_operation(
            claim_id=claim_id,
            operation_type=OracleOperationType.REGISTER_CLAIM.value,
            payload=payload,
            configuration_id=configuration_id,
            job_id=job_id,
        )

    def submit_inference(
        self,
        claim_id: int,
        number: str,
        proof: ProofResult,
        *,
        confidence: float,
        amount_above_threshold: bool,
        rules_consistent: bool,
        configuration_id: int,
        job_id: int | None = None,
    ) -> ChainReceipt:
        if self.is_web3:
            bytes_prueba, instancias = Web3ChainClient._instancias(proof)
            hex_proof = bytes_prueba.hex()
        else:
            instancias = []
            hex_proof = ""

        payload = {
            "number": number,
            "proof_result": {
                "proof_hex": proof.proof.hex(),
                "public_inputs_hex": proof.public_inputs.hex(),
                "proof_hash": proof.proof_hash,
                "circuit_parameters": proof.circuit_parameters,
                "declared_visibility": proof.declared_visibility,
                "verified_locally": proof.verified_locally,
            } if not self.is_web3 else None,
            "proof_hash": proof.proof_hash,
            "hex_proof": hex_proof,
            "instances": instancias,
            "confidence": confidence,
            "confidence_bps": round(confidence * 10_000),
            "amount_above_threshold": amount_above_threshold,
            "rules_consistent": rules_consistent,
        }
        return self.submit_operation(
            claim_id=claim_id,
            operation_type=OracleOperationType.SUBMIT_INFERENCE.value,
            payload=payload,
            configuration_id=configuration_id,
            job_id=job_id,
        )

    def settle(
        self,
        claim_id: int,
        number: str,
        *,
        automatic: bool,
        configuration_id: int,
        job_id: int | None = None,
    ) -> ChainReceipt:
        payload = {
            "number": number,
            "automatic": automatic,
        }
        return self.submit_operation(
            claim_id=claim_id,
            operation_type=OracleOperationType.SETTLE.value,
            payload=payload,
            configuration_id=configuration_id,
            job_id=job_id,
        )

    def resolve(
        self,
        claim_id: int,
        number: str,
        *,
        favorable: bool,
        decision_hash: str,
        reviewer_identity: str,
        justification: str,
        configuration_id: int,
        review_id: int | None = None,
    ) -> ChainReceipt:
        payload = {
            "number": number,
            "favorable": favorable,
            "decision_hash": decision_hash,
            "reviewer_identity": reviewer_identity,
            "justification": justification,
            "review_id": review_id,
        }
        return self.submit_operation(
            claim_id=claim_id,
            operation_type=OracleOperationType.RESOLVE.value,
            payload=payload,
            configuration_id=configuration_id,
            job_id=None,
        )

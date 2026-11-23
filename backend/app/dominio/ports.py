"""Puertos que el dominio necesita y que los adaptadores satisfacen.

Se declaran como protocolos y no como clases base para que el dominio no
importe a los adaptadores: la dependencia apunta hacia adentro, que es lo que
permite probar los servicios sin red, sin ONNX y sin cadena, y lo que deja
cambiar de proveedor sin tocar las reglas.

Tres de los cuatro puertos tienen hoy dos implementaciones, una real y una
simulada, y se eligen por variable de entorno. La simulada no está para
disimular que falta la real: está para que el resto del sistema se pueda
ejercitar y para que el día que entre EZKL o web3 no haya que reescribir el
dominio, solo enchufar la otra.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.dominio.vocabulary import ClaimStatus, DamageClass, TransitionCause


class EvidencePublisher(Protocol):
    """Publica los bytes de una fotografía y devuelve su identificador de
    contenido. `IpfsClient` lo satisface: publica en el nodo propio, sube la
    copia remota y aborta si los dos identificadores no coinciden, de modo que
    un retorno normal significa que los dos custodios quedaron con el mismo
    CID."""

    def publish(self, content: bytes, filename: str = "evidencia") -> str: ...

    def retrieve(self, cid: str, *, max_bytes: int | None = None) -> bytes: ...


@dataclass(frozen=True)
class InferenceResult:
    """Salida del tramo off-chain de features-afuera.

    El vector de características y el hash del artefacto que lo produjo viajan
    juntos porque esa combinación es lo que vuelve ejecutable la replicación:
    sin saber con qué versión se produjo el vector, el tercero que reejecuta no
    tendría contra qué contrastar su resultado.
    """

    feature_vector: bytes
    logits: bytes
    predicted_class: DamageClass
    confidence: float
    extractor_artifact_hash: str
    model_version: str


class InferenceEngine(Protocol):
    """Extrae las características fuera del circuito y clasifica con la cabeza."""

    def run(self, images: list[bytes]) -> InferenceResult: ...

    def artifacts_identity(self) -> str: ...


@dataclass(frozen=True)
class ProofResult:
    proof: bytes
    public_inputs: bytes
    proof_hash: str
    circuit_parameters: dict[str, Any]
    declared_visibility: dict[str, Any]
    verified_locally: bool


class ProofGenerator(Protocol):
    """Genera la prueba de la cabeza y la verifica localmente antes de
    mandarla. Verificar del lado propio evita gastar una transacción en una
    prueba que ya se sabe inválida."""

    def prove(self, inference: InferenceResult) -> ProofResult: ...

    def artifacts_identity(self) -> str: ...


@dataclass(frozen=True)
class ChainReceipt:
    """Lo que la cadena devuelve al asentar una transición.

    `transaction_hash` y `block_number` son nulos en la implementación
    simulada, y ese nulo es la marca de que todavía no hay respaldo on-chain.
    No se rellenan con valores plausibles.
    """

    status: ClaimStatus
    # Nula en el registro: la entrada al sistema no es una transicion de
    # la matriz de adr-001 y no tiene causa que asentar.
    cause: TransitionCause | None
    transaction_hash: str | None = None
    block_number: int | None = None
    proof_hash: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ChainClient(Protocol):
    """Oráculo del contrato. Las firmas replican las de `ClaimRegistry.sol`.

    `submit_inference` no devuelve un error cuando la prueba no verifica:
    asienta `VerificacionFallida` y sigue, que es lo que RNF-17 exige y lo que
    sostiene el argumento de auditabilidad. Revertir borraría el episodio.
    """

    def register_claim(self, claim_id: str, evidence_id: str) -> ChainReceipt: ...

    def submit_inference(
        self,
        claim_id: str,
        proof: ProofResult,
        *,
        confidence: float,
        amount_above_threshold: bool,
        rules_consistent: bool,
    ) -> ChainReceipt: ...

    def settle(self, claim_id: str, *, automatic: bool) -> ChainReceipt: ...

    def resolve(
        self, claim_id: str, *, favorable: bool, decision_hash: str
    ) -> ChainReceipt: ...

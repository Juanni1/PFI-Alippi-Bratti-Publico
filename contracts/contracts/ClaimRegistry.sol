// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

import {IProofVerifier} from "./IProofVerifier.sol";

/// Contrato de reclamos del PFI. Implementa la máquina de siete estados de
/// adr-001 (esa ADR es la especificación; esta implementación se valida
/// contra su matriz de transiciones) con el patrón onlyOracle: ninguna
/// transición la dispara un humano en forma directa, el backend de la
/// aseguradora es la única cuenta habilitada, incluso cuando lo que asienta
/// es una decisión humana (RF-25).
///
/// Correspondencia de estados con adr-001 (identificadores en inglés por
/// regla de repo; los nombres en español son los del documento):
///   Registered             = Registrado
///   Approvable             = Aprobable
///   ReviewRequired         = RequiereRevision
///   EvidenceInconsistency  = InconsistenciaEvidencia
///   VerificationFailed     = VerificacionFallida
///   Approved               = Aprobado (terminal)
///   Rejected               = Rechazado (terminal)
///
/// Sobre la cadena solo se escriben identificadores, estado, hashes y
/// marcas temporales (RNF-24). El monto declarado y las reglas del caso se
/// evalúan off-chain contra la configuración versionada (RF-32) y entran
/// como predicados booleanos: pasar el monto en claro por calldata lo haría
/// público y contradiría la matriz de visibilidad (RF-47). La confianza sí
/// entra en claro porque es derivable de los logits, que ya son públicos.
contract ClaimRegistry {
    /// El valor None (0) distingue el reclamo inexistente en el mapping.
    enum Status {
        None,
        Registered,
        Approvable,
        ReviewRequired,
        EvidenceInconsistency,
        VerificationFailed,
        Approved,
        Rejected
    }

    /// Causa de cada transición, para el evento que RF-26 exige y para la
    /// explicación de RF-42. Cuando concurren varias causas de derivación
    /// se asienta una sola, con esta prioridad: inconsistencia con las
    /// reglas, confianza bajo el umbral, monto sobre el umbral, política de
    /// confirmación humana (RF-35 alcanza solo al reclamo que sin el modo
    /// sería Aprobable).
    enum Cause {
        None,
        FavorableClassification,
        LowConfidence,
        AmountAboveThreshold,
        HumanConfirmationPolicy,
        RuleInconsistency,
        VerificationFailure,
        MandatoryEscalation,
        AutomaticSettlement,
        OperatorSettlement,
        FavorableResolution,
        UnfavorableResolution
    }

    struct Claim {
        Status status;
        bytes32 evidenceId;
        uint64 registeredAt;
    }

    /// Única cuenta habilitada a disparar transiciones (RF-25). Inmutable:
    /// rotar el oráculo implica redesplegar, decisión asumida del prototipo.
    address public immutable oracle;

    /// Verificador vigente. Sustituible sin tocar este contrato (RF-49,
    /// RNF-28): el verificador se regenera con cada cambio de modelo.
    IProofVerifier public verifier;

    /// Umbral de confianza en puntos básicos (RF-32). El valor canónico
    /// vive en la configuración versionada del proyecto; acá se asienta la
    /// copia operativa, con evento por cada cambio.
    uint32 public confidenceThresholdBps;

    /// Modo de confirmación humana obligatoria (RF-35, adr-007): activo,
    /// ningún reclamo clasifica en Approvable.
    bool public humanConfirmationRequired;

    mapping(bytes32 => Claim) public claims;

    event ClaimRegistered(bytes32 indexed claimId, bytes32 evidenceId);

    /// Evento por transición con origen, destino, causa y hash de la prueba
    /// (RF-26). En las transiciones sin prueba el hash va en cero.
    event StateTransition(
        bytes32 indexed claimId,
        Status indexed from,
        Status indexed to,
        Cause cause,
        bytes32 proofHash
    );

    /// Hash de la decisión humana: identidad del revisor, marca temporal y
    /// justificación quedan off-chain y acá se asienta su digesto (RF-39).
    event ResolutionRecorded(
        bytes32 indexed claimId,
        bool favorable,
        bytes32 decisionHash
    );

    /// Sin este evento, un auditor que reconstruye el ciclo no sabría
    /// contra qué verificador se validó cada prueba (RF-49).
    event VerifierChanged(
        address indexed previousVerifier,
        address indexed newVerifier
    );

    event ConfidenceThresholdChanged(
        uint32 previousThresholdBps,
        uint32 newThresholdBps
    );

    event HumanConfirmationModeChanged(bool enabled);

    error NotOracle();
    error ZeroAddress();
    error ClaimAlreadyExists(bytes32 claimId);
    error UnknownClaim(bytes32 claimId);
    /// La función invocada no admite el estado vigente del reclamo: la
    /// transición no figura en la matriz de adr-001 (RF-31).
    error InvalidTransition(bytes32 claimId, Status from);
    error MissingEvidence();
    error MissingDecisionHash();

    modifier onlyOracle() {
        if (msg.sender != oracle) revert NotOracle();
        _;
    }

    constructor(address initialOracle, address initialVerifier) {
        if (initialOracle == address(0) || initialVerifier == address(0)) {
            revert ZeroAddress();
        }
        oracle = initialOracle;
        verifier = IProofVerifier(initialVerifier);
    }

    // ------------------------------------------------------------------
    // Entrada a la máquina
    // ------------------------------------------------------------------

    /// Crea el reclamo en Registered con el identificador de su evidencia
    /// (digesto del CID). La entrada no es una transición de la matriz:
    /// adr-001 la enumera aparte de las nueve permitidas.
    function registerClaim(
        bytes32 claimId,
        bytes32 evidenceId
    ) external onlyOracle {
        if (evidenceId == bytes32(0)) revert MissingEvidence();
        Claim storage claim = claims[claimId];
        if (claim.status != Status.None) revert ClaimAlreadyExists(claimId);
        claim.status = Status.Registered;
        claim.evidenceId = evidenceId;
        claim.registeredAt = uint64(block.timestamp);
        emit ClaimRegistered(claimId, evidenceId);
    }

    // ------------------------------------------------------------------
    // Clasificación (Registered -> Approvable | ReviewRequired |
    // EvidenceInconsistency | VerificationFailed)
    // ------------------------------------------------------------------

    /// Verifica la prueba y clasifica. El try/catch no es un borde
    /// defensivo: es por donde pasa el fallo de verificación, porque el
    /// verificador de EZKL revierte ante prueba inválida (RF-23). Tanto el
    /// false como el revert asientan VerificationFailed sin abortar la
    /// transacción (RF-24, RNF-17); la vía fallida consume gas y escribe
    /// estado a cambio de la traza, costo asumido en adr-001.
    ///
    /// @param confidenceBps Confianza de la clase predicha, softmax
    ///        off-chain, en puntos básicos.
    /// @param amountAboveThreshold Predicado monto declarado > umbral,
    ///        evaluado off-chain contra configuración versionada (RF-34;
    ///        el monto en claro no viaja, ver nota de cabecera).
    /// @param rulesConsistent Predicado de consistencia entre la clase
    ///        predicha y las reglas de negocio del caso (RF-30).
    function submitInference(
        bytes32 claimId,
        bytes calldata proof,
        uint256[] calldata instances,
        uint32 confidenceBps,
        bool amountAboveThreshold,
        bool rulesConsistent
    ) external onlyOracle {
        Claim storage claim = _existingClaim(claimId);
        if (claim.status != Status.Registered) {
            revert InvalidTransition(claimId, claim.status);
        }
        bytes32 proofHash = keccak256(proof);
        try verifier.verifyProof(proof, instances) returns (bool ok) {
            if (ok) {
                // Las reglas se evalúan únicamente después de una
                // verificación exitosa (RF-27).
                (Status target, Cause cause) = _classify(
                    confidenceBps,
                    amountAboveThreshold,
                    rulesConsistent
                );
                _transition(claimId, claim, target, cause, proofHash);
            } else {
                _transition(
                    claimId,
                    claim,
                    Status.VerificationFailed,
                    Cause.VerificationFailure,
                    proofHash
                );
            }
        } catch {
            _transition(
                claimId,
                claim,
                Status.VerificationFailed,
                Cause.VerificationFailure,
                proofHash
            );
        }
    }

    /// Reglas deterministas de clasificación (RF-28 a RF-30, RF-34, RF-35).
    function _classify(
        uint32 confidenceBps,
        bool amountAboveThreshold,
        bool rulesConsistent
    ) private view returns (Status, Cause) {
        if (!rulesConsistent) {
            return (Status.EvidenceInconsistency, Cause.RuleInconsistency);
        }
        if (confidenceBps < confidenceThresholdBps) {
            return (Status.ReviewRequired, Cause.LowConfidence);
        }
        if (amountAboveThreshold) {
            return (Status.ReviewRequired, Cause.AmountAboveThreshold);
        }
        if (humanConfirmationRequired) {
            return (Status.ReviewRequired, Cause.HumanConfirmationPolicy);
        }
        return (Status.Approvable, Cause.FavorableClassification);
    }

    // ------------------------------------------------------------------
    // Resto de la matriz
    // ------------------------------------------------------------------

    /// VerificationFailed -> ReviewRequired. Única salida del estado y
    /// obligatoria (RF-33): un fallo de verificación señala un problema del
    /// lado del operador, no de la evidencia del reclamante; no hay
    /// reintento automático que diluya el episodio.
    function escalate(bytes32 claimId) external onlyOracle {
        Claim storage claim = _existingClaim(claimId);
        if (claim.status != Status.VerificationFailed) {
            revert InvalidTransition(claimId, claim.status);
        }
        _transition(
            claimId,
            claim,
            Status.ReviewRequired,
            Cause.MandatoryEscalation,
            bytes32(0)
        );
    }

    /// Approvable -> Approved (RF-37). La política que decide liquidar sin
    /// intervención humana (adr-007) vive en el backend; para el contrato es
    /// siempre el oráculo quien liquida. Lo que sí distingue el contrato es
    /// por cuál de los dos caminos se llegó, porque la causa del evento es
    /// lo único que el auditor externo tiene para reconstruirlo (RF-26) y de
    /// ella dependen los textos que las pantallas del reclamante muestran.
    /// El backend declara el camino; el contrato lo asienta y no lo infiere.
    function settle(bytes32 claimId, bool automatic) external onlyOracle {
        Claim storage claim = _existingClaim(claimId);
        if (claim.status != Status.Approvable) {
            revert InvalidTransition(claimId, claim.status);
        }
        _transition(
            claimId,
            claim,
            Status.Approved,
            automatic ? Cause.AutomaticSettlement : Cause.OperatorSettlement,
            bytes32(0)
        );
    }

    /// Resolución humana asentada por el oráculo (RF-38). Favorable solo
    /// desde ReviewRequired; desfavorable desde ReviewRequired o
    /// EvidenceInconsistency, según la matriz. El hash de la decisión es
    /// obligatorio (RF-39).
    function resolve(
        bytes32 claimId,
        bool favorable,
        bytes32 decisionHash
    ) external onlyOracle {
        if (decisionHash == bytes32(0)) revert MissingDecisionHash();
        Claim storage claim = _existingClaim(claimId);
        Status current = claim.status;
        if (favorable) {
            if (current != Status.ReviewRequired) {
                revert InvalidTransition(claimId, current);
            }
            _transition(
                claimId,
                claim,
                Status.Approved,
                Cause.FavorableResolution,
                bytes32(0)
            );
        } else {
            if (
                current != Status.ReviewRequired &&
                current != Status.EvidenceInconsistency
            ) {
                revert InvalidTransition(claimId, current);
            }
            _transition(
                claimId,
                claim,
                Status.Rejected,
                Cause.UnfavorableResolution,
                bytes32(0)
            );
        }
        emit ResolutionRecorded(claimId, favorable, decisionHash);
    }

    // ------------------------------------------------------------------
    // Configuración operativa
    // ------------------------------------------------------------------

    /// Sustituye el verificador ante un cambio de modelo (RF-49, RNF-28).
    /// No es una transición, pero la garantía criptográfica entera depende
    /// de esta función: queda restringida al oráculo y con su evento.
    function setVerifier(address newVerifier) external onlyOracle {
        if (newVerifier == address(0)) revert ZeroAddress();
        address previous = address(verifier);
        verifier = IProofVerifier(newVerifier);
        emit VerifierChanged(previous, newVerifier);
    }

    function setConfidenceThreshold(
        uint32 newThresholdBps
    ) external onlyOracle {
        uint32 previous = confidenceThresholdBps;
        confidenceThresholdBps = newThresholdBps;
        emit ConfidenceThresholdChanged(previous, newThresholdBps);
    }

    function setHumanConfirmationMode(bool enabled) external onlyOracle {
        humanConfirmationRequired = enabled;
        emit HumanConfirmationModeChanged(enabled);
    }

    // ------------------------------------------------------------------
    // Internas
    // ------------------------------------------------------------------

    function _existingClaim(
        bytes32 claimId
    ) private view returns (Claim storage claim) {
        claim = claims[claimId];
        if (claim.status == Status.None) revert UnknownClaim(claimId);
    }

    /// Asienta la transición y emite el evento del ciclo de vida (RF-26,
    /// RNF-26). Los estados terminales no tienen salida por construcción:
    /// ninguna función acepta Approved ni Rejected como origen (RF-40).
    function _transition(
        bytes32 claimId,
        Claim storage claim,
        Status to,
        Cause cause,
        bytes32 proofHash
    ) private {
        Status from = claim.status;
        claim.status = to;
        emit StateTransition(claimId, from, to, cause, proofHash);
    }
}

// Arnés compartido de la suite. Los valores replican los enums del
// contrato; la correspondencia con los nombres en español de adr-001 está
// en la cabecera de ClaimRegistry.sol.
const { ethers } = require("hardhat");

const Status = {
  None: 0n,
  Registered: 1n, // Registrado
  Approvable: 2n, // Aprobable
  ReviewRequired: 3n, // RequiereRevision
  EvidenceInconsistency: 4n, // InconsistenciaEvidencia
  VerificationFailed: 5n, // VerificacionFallida
  Approved: 6n, // Aprobado (terminal)
  Rejected: 7n, // Rechazado (terminal)
};

const Cause = {
  None: 0n,
  FavorableClassification: 1n,
  LowConfidence: 2n,
  AmountAboveThreshold: 3n,
  HumanConfirmationPolicy: 4n,
  RuleInconsistency: 5n,
  VerificationFailure: 6n,
  MandatoryEscalation: 7n,
  AutomaticSettlement: 8n,
  OperatorSettlement: 9n,
  FavorableResolution: 10n,
  UnfavorableResolution: 11n,
};

const MockMode = {
  Accept: 0n,
  Reject: 1n,
  RevertOnVerify: 2n,
  Tagged: 3n,
};

// Umbral de referencia de la suite: 70,00 %. El valor operativo real sale
// de la configuración versionada (RF-32); este es solo un valor de prueba.
const THRESHOLD_BPS = 7000;
const HIGH_CONFIDENCE = 9000;
const LOW_CONFIDENCE = 5000;

const SAMPLE_PROOF = "0xaa1122";
const SAMPLE_INSTANCES = [1n, 2n, 3n];
const EVIDENCE_ID = ethers.keccak256(ethers.toUtf8Bytes("cid-evidencia"));
const DECISION_HASH = ethers.keccak256(ethers.toUtf8Bytes("acta-decision"));

let claimCounter = 0;

/// Identificador fresco por reclamo, para que cada caso de la matriz corra
/// sobre un expediente propio.
function freshClaimId() {
  claimCounter += 1;
  return ethers.keccak256(ethers.toUtf8Bytes(`claim-${claimCounter}`));
}

/// Despliega mock (Accept) y registro, con el primer firmante como oráculo.
async function deployFixture() {
  const [oracle, outsider] = await ethers.getSigners();
  const MockVerifier = await ethers.getContractFactory("MockVerifier");
  const mock = await MockVerifier.deploy(MockMode.Accept, "0xaa");
  const ClaimRegistry = await ethers.getContractFactory("ClaimRegistry");
  const registry = await ClaimRegistry.deploy(oracle.address, mock.target);
  await registry.setConfidenceThreshold(THRESHOLD_BPS);
  return { registry, mock, oracle, outsider };
}

/// Lleva un reclamo nuevo hasta el estado pedido recorriendo solo
/// transiciones permitidas de adr-001. Devuelve el claimId.
async function driveTo(registry, mock, targetStatus) {
  const claimId = freshClaimId();
  await registry.registerClaim(claimId, EVIDENCE_ID);
  if (targetStatus === Status.Registered) return claimId;

  const submit = (confidence, amountAbove, rulesConsistent) =>
    registry.submitInference(
      claimId,
      SAMPLE_PROOF,
      SAMPLE_INSTANCES,
      confidence,
      amountAbove,
      rulesConsistent,
    );

  switch (targetStatus) {
    case Status.Approvable:
      await submit(HIGH_CONFIDENCE, false, true);
      return claimId;
    case Status.ReviewRequired:
      await submit(LOW_CONFIDENCE, false, true);
      return claimId;
    case Status.EvidenceInconsistency:
      await submit(HIGH_CONFIDENCE, false, false);
      return claimId;
    case Status.VerificationFailed:
      // Rechazo transitorio del verificador solo para producir el estado.
      await mock.setMode(MockMode.Reject);
      await submit(HIGH_CONFIDENCE, false, true);
      await mock.setMode(MockMode.Accept);
      return claimId;
    case Status.Approved:
      await submit(HIGH_CONFIDENCE, false, true);
      await registry.settle(claimId, false);
      return claimId;
    case Status.Rejected:
      await submit(HIGH_CONFIDENCE, false, false);
      await registry.resolve(claimId, false, DECISION_HASH);
      return claimId;
    default:
      throw new Error(`estado no alcanzable: ${targetStatus}`);
  }
}

module.exports = {
  Status,
  Cause,
  MockMode,
  THRESHOLD_BPS,
  HIGH_CONFIDENCE,
  LOW_CONFIDENCE,
  SAMPLE_PROOF,
  SAMPLE_INSTANCES,
  EVIDENCE_ID,
  DECISION_HASH,
  freshClaimId,
  deployFixture,
  driveTo,
};

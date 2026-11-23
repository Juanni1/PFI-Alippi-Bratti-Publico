// RNF-17 / RF-23 / RF-24: ante prueba inválida el contrato no revierte;
// asienta VerificationFailed y la transacción termina bien, tanto cuando
// el verificador devuelve false como cuando revierte (el comportamiento
// real del artefacto de EZKL). Sin este test, la garantía de auditabilidad
// es una afirmación del documento sin respaldo ejecutable (adr-001).
const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture } = require("@nomicfoundation/hardhat-toolbox/network-helpers");
const {
  Status,
  Cause,
  MockMode,
  HIGH_CONFIDENCE,
  SAMPLE_PROOF,
  SAMPLE_INSTANCES,
  EVIDENCE_ID,
  freshClaimId,
  deployFixture,
} = require("./helpers");

async function submitWith(registry, mock, mode) {
  const claimId = freshClaimId();
  await registry.registerClaim(claimId, EVIDENCE_ID);
  await mock.setMode(mode);
  const tx = registry.submitInference(
    claimId, SAMPLE_PROOF, SAMPLE_INSTANCES, HIGH_CONFIDENCE, false, true,
  );
  return { claimId, tx };
}

describe("verificación de la prueba (RNF-17)", function () {
  const proofHash = ethers.keccak256(SAMPLE_PROOF);

  it("prueba adulterada con verificador que devuelve false: asienta VerificationFailed sin revertir", async function () {
    const { registry, mock } = await loadFixture(deployFixture);
    const { claimId, tx } = await submitWith(registry, mock, MockMode.Reject);
    await expect(tx)
      .to.emit(registry, "StateTransition")
      .withArgs(
        claimId,
        Status.Registered,
        Status.VerificationFailed,
        Cause.VerificationFailure,
        proofHash,
      );
    const claim = await registry.claims(claimId);
    expect(claim.status).to.equal(Status.VerificationFailed);
  });

  it("prueba adulterada con verificador que revierte: asienta VerificationFailed sin revertir", async function () {
    const { registry, mock } = await loadFixture(deployFixture);
    const { claimId, tx } = await submitWith(
      registry, mock, MockMode.RevertOnVerify,
    );
    await expect(tx)
      .to.emit(registry, "StateTransition")
      .withArgs(
        claimId,
        Status.Registered,
        Status.VerificationFailed,
        Cause.VerificationFailure,
        proofHash,
      );
    const claim = await registry.claims(claimId);
    expect(claim.status).to.equal(Status.VerificationFailed);
  });

  it("prueba válida: clasifica y asienta el hash de la prueba en el evento", async function () {
    const { registry, mock } = await loadFixture(deployFixture);
    const { claimId, tx } = await submitWith(registry, mock, MockMode.Accept);
    await expect(tx)
      .to.emit(registry, "StateTransition")
      .withArgs(
        claimId,
        Status.Registered,
        Status.Approvable,
        Cause.FavorableClassification,
        proofHash,
      );
  });
});

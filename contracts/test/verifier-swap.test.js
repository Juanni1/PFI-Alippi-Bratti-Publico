// RF-49 / RNF-28: sustitución del verificador sin tocar el contrato de
// negocio, restringida al oráculo y con evento. El caso de la prueba que
// deja de verificar tras el cambio es el análogo a nivel contrato de
// RNF-33 (una prueba de una preparación no verifica contra otra); la
// corrida con dos preparaciones reales de EZKL es de integración y queda
// para el hito del 75 %.
const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture } = require("@nomicfoundation/hardhat-toolbox/network-helpers");
const {
  Status,
  MockMode,
  HIGH_CONFIDENCE,
  SAMPLE_INSTANCES,
  EVIDENCE_ID,
  freshClaimId,
  deployFixture,
} = require("./helpers");

// Prueba "ligada" a la preparación A: su primer byte es la etiqueta que el
// verificador A exige.
const PROOF_FOR_A = "0xaa0102";

async function deployTaggedVerifier(tag) {
  const MockVerifier = await ethers.getContractFactory("MockVerifier");
  return MockVerifier.deploy(MockMode.Tagged, tag);
}

describe("sustitución del verificador (RF-49, RNF-28)", function () {
  it("la misma prueba verifica contra su preparación y falla contra la otra", async function () {
    const { registry, oracle } = await loadFixture(deployFixture);
    const verifierA = await deployTaggedVerifier("0xaa");
    const verifierB = await deployTaggedVerifier("0xbb");

    await registry.setVerifier(verifierA.target);

    // Contra la preparación A la prueba clasifica.
    const first = freshClaimId();
    await registry.registerClaim(first, EVIDENCE_ID);
    await registry.submitInference(
      first, PROOF_FOR_A, SAMPLE_INSTANCES, HIGH_CONFIDENCE, false, true,
    );
    expect((await registry.claims(first)).status).to.equal(Status.Approvable);

    // Se sustituye el verificador, con su evento (RF-49).
    await expect(registry.setVerifier(verifierB.target))
      .to.emit(registry, "VerifierChanged")
      .withArgs(verifierA.target, verifierB.target);

    // La misma prueba, contra la preparación B, queda en
    // VerificationFailed: falla en todos los casos (RNF-33, análogo).
    const second = freshClaimId();
    await registry.registerClaim(second, EVIDENCE_ID);
    await registry.submitInference(
      second, PROOF_FOR_A, SAMPLE_INSTANCES, HIGH_CONFIDENCE, false, true,
    );
    expect((await registry.claims(second)).status).to.equal(
      Status.VerificationFailed,
    );
  });

  it("rechaza el verificador en cero", async function () {
    const { registry } = await loadFixture(deployFixture);
    await expect(
      registry.setVerifier("0x" + "00".repeat(20)),
    ).to.be.revertedWithCustomError(registry, "ZeroAddress");
  });

  it("la sustitución no exige redesplegar: los reclamos previos conservan su estado", async function () {
    const { registry } = await loadFixture(deployFixture);
    const verifierA = await deployTaggedVerifier("0xaa");
    const claimId = freshClaimId();
    await registry.registerClaim(claimId, EVIDENCE_ID);
    await registry.submitInference(
      claimId, PROOF_FOR_A, SAMPLE_INSTANCES, HIGH_CONFIDENCE, false, true,
    );
    await registry.setVerifier(verifierA.target);
    expect((await registry.claims(claimId)).status).to.equal(Status.Approvable);
  });
});

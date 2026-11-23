// Módulo de reglas deterministas (RF-28 a RF-30, RF-34, RF-35): cada rama
// de la clasificación, la prioridad entre causas concurrentes y el efecto
// de la configuración operativa. La cobertura de este módulo es la que
// RNF-30 pide medir con la herramienta del entorno de contratos.
const { expect } = require("chai");
const { loadFixture } = require("@nomicfoundation/hardhat-toolbox/network-helpers");
const {
  Status,
  Cause,
  THRESHOLD_BPS,
  HIGH_CONFIDENCE,
  LOW_CONFIDENCE,
  SAMPLE_PROOF,
  SAMPLE_INSTANCES,
  EVIDENCE_ID,
  freshClaimId,
  deployFixture,
} = require("./helpers");

async function classify(registry, { confidence, amountAbove, consistent }) {
  const claimId = freshClaimId();
  await registry.registerClaim(claimId, EVIDENCE_ID);
  const tx = await registry.submitInference(
    claimId, SAMPLE_PROOF, SAMPLE_INSTANCES, confidence, amountAbove, consistent,
  );
  const claim = await registry.claims(claimId);
  return { claimId, tx, status: claim.status };
}

describe("reglas de clasificación (RF-28 a RF-35)", function () {
  it("RF-28: confianza sobre el umbral, monto bajo, reglas favorables y modo inactivo -> Approvable", async function () {
    const { registry } = await loadFixture(deployFixture);
    const { status } = await classify(registry, {
      confidence: HIGH_CONFIDENCE, amountAbove: false, consistent: true,
    });
    expect(status).to.equal(Status.Approvable);
  });

  it("la confianza que alcanza exactamente el umbral clasifica favorable", async function () {
    // "Alcanza el umbral" (RF-28): la comparación es estricta por debajo.
    const { registry } = await loadFixture(deployFixture);
    const { status } = await classify(registry, {
      confidence: THRESHOLD_BPS, amountAbove: false, consistent: true,
    });
    expect(status).to.equal(Status.Approvable);
  });

  it("RF-29: confianza bajo el umbral -> ReviewRequired con causa LowConfidence", async function () {
    const { registry } = await loadFixture(deployFixture);
    const { tx, status } = await classify(registry, {
      confidence: LOW_CONFIDENCE, amountAbove: false, consistent: true,
    });
    expect(status).to.equal(Status.ReviewRequired);
    await expect(tx).to.emit(registry, "StateTransition").withArgs(
      (v) => true, Status.Registered, Status.ReviewRequired,
      Cause.LowConfidence, (v) => true,
    );
  });

  it("RF-34: monto sobre el umbral deriva cualquiera sea la confianza", async function () {
    const { registry } = await loadFixture(deployFixture);
    const { status } = await classify(registry, {
      confidence: HIGH_CONFIDENCE, amountAbove: true, consistent: true,
    });
    expect(status).to.equal(Status.ReviewRequired);
  });

  it("RF-30: clase inconsistente con las reglas -> EvidenceInconsistency", async function () {
    const { registry } = await loadFixture(deployFixture);
    const { status } = await classify(registry, {
      confidence: HIGH_CONFIDENCE, amountAbove: false, consistent: false,
    });
    expect(status).to.equal(Status.EvidenceInconsistency);
  });

  it("la inconsistencia tiene prioridad sobre las causas de derivación", async function () {
    // Con reglas en contra y confianza baja a la vez, el destino es
    // EvidenceInconsistency: la derivación por confianza no lo tapa.
    const { registry } = await loadFixture(deployFixture);
    const { status } = await classify(registry, {
      confidence: LOW_CONFIDENCE, amountAbove: true, consistent: false,
    });
    expect(status).to.equal(Status.EvidenceInconsistency);
  });

  it("RF-35: con el modo de confirmación humana activo, lo que sería Approvable cae en ReviewRequired", async function () {
    const { registry } = await loadFixture(deployFixture);
    await registry.setHumanConfirmationMode(true);
    const { tx, status } = await classify(registry, {
      confidence: HIGH_CONFIDENCE, amountAbove: false, consistent: true,
    });
    expect(status).to.equal(Status.ReviewRequired);
    await expect(tx).to.emit(registry, "StateTransition").withArgs(
      (v) => true, Status.Registered, Status.ReviewRequired,
      Cause.HumanConfirmationPolicy, (v) => true,
    );
  });

  it("RF-35 no alcanza al reclamo inconsistente: el modo activo no lo desvía de EvidenceInconsistency", async function () {
    const { registry } = await loadFixture(deployFixture);
    await registry.setHumanConfirmationMode(true);
    const { status } = await classify(registry, {
      confidence: HIGH_CONFIDENCE, amountAbove: false, consistent: false,
    });
    expect(status).to.equal(Status.EvidenceInconsistency);
  });

  it("RF-32: cambiar el umbral cambia la clasificación y emite su evento", async function () {
    const { registry } = await loadFixture(deployFixture);
    await expect(registry.setConfidenceThreshold(LOW_CONFIDENCE - 1))
      .to.emit(registry, "ConfidenceThresholdChanged")
      .withArgs(THRESHOLD_BPS, LOW_CONFIDENCE - 1);
    // Con el umbral más bajo, la misma confianza que antes derivaba ahora
    // clasifica favorable.
    const { status } = await classify(registry, {
      confidence: LOW_CONFIDENCE, amountAbove: false, consistent: true,
    });
    expect(status).to.equal(Status.Approvable);
  });

  it("el cambio de modo de confirmación humana emite su evento", async function () {
    const { registry } = await loadFixture(deployFixture);
    await expect(registry.setHumanConfirmationMode(true))
      .to.emit(registry, "HumanConfirmationModeChanged")
      .withArgs(true);
  });
});

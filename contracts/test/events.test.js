// RNF-26 / RF-26: el ciclo de vida completo debe poder reconstruirse desde
// los eventos de la cadena. La suite recorre dos ciclos (el feliz y el que
// pasa por el fallo de verificación), lee todos los eventos desde el bloque
// inicial y compara la secuencia reconstruida contra la esperada.
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
  DECISION_HASH,
  freshClaimId,
  deployFixture,
} = require("./helpers");

/// Reconstruye la secuencia de transiciones de un reclamo desde los
/// eventos, tal como lo haría un auditor externo.
async function reconstruct(registry, claimId) {
  const events = await registry.queryFilter(
    registry.filters.StateTransition(claimId),
    0,
  );
  return events.map((event) => ({
    from: event.args.from,
    to: event.args.to,
    cause: event.args.cause,
    proofHash: event.args.proofHash,
  }));
}

describe("reconstrucción del ciclo de vida (RNF-26)", function () {
  it("el ciclo feliz se reconstruye completo desde los eventos", async function () {
    const { registry } = await loadFixture(deployFixture);
    const claimId = freshClaimId();
    const proofHash = ethers.keccak256(SAMPLE_PROOF);

    await registry.registerClaim(claimId, EVIDENCE_ID);
    await registry.submitInference(
      claimId, SAMPLE_PROOF, SAMPLE_INSTANCES, HIGH_CONFIDENCE, false, true,
    );
    await registry.settle(claimId, false);

    // El registro inicial queda en su propio evento.
    const registered = await registry.queryFilter(
      registry.filters.ClaimRegistered(claimId),
      0,
    );
    expect(registered).to.have.lengthOf(1);
    expect(registered[0].args.evidenceId).to.equal(EVIDENCE_ID);

    const sequence = await reconstruct(registry, claimId);
    expect(sequence).to.deep.equal([
      {
        from: Status.Registered,
        to: Status.Approvable,
        cause: Cause.FavorableClassification,
        proofHash,
      },
      {
        from: Status.Approvable,
        to: Status.Approved,
        cause: Cause.OperatorSettlement,
        proofHash: ethers.ZeroHash,
      },
    ]);
  });

  it("el ciclo con fallo de verificación se reconstruye completo, incluida la resolución humana", async function () {
    const { registry, mock } = await loadFixture(deployFixture);
    const claimId = freshClaimId();
    const proofHash = ethers.keccak256(SAMPLE_PROOF);

    await registry.registerClaim(claimId, EVIDENCE_ID);
    await mock.setMode(MockMode.RevertOnVerify);
    await registry.submitInference(
      claimId, SAMPLE_PROOF, SAMPLE_INSTANCES, HIGH_CONFIDENCE, false, true,
    );
    await registry.escalate(claimId);
    await registry.resolve(claimId, false, DECISION_HASH);

    const sequence = await reconstruct(registry, claimId);
    expect(sequence).to.deep.equal([
      {
        from: Status.Registered,
        to: Status.VerificationFailed,
        cause: Cause.VerificationFailure,
        proofHash,
      },
      {
        from: Status.VerificationFailed,
        to: Status.ReviewRequired,
        cause: Cause.MandatoryEscalation,
        proofHash: ethers.ZeroHash,
      },
      {
        from: Status.ReviewRequired,
        to: Status.Rejected,
        cause: Cause.UnfavorableResolution,
        proofHash: ethers.ZeroHash,
      },
    ]);

    // RF-39: el hash de la decisión humana queda asentado en su evento.
    const resolutions = await registry.queryFilter(
      registry.filters.ResolutionRecorded(claimId),
      0,
    );
    expect(resolutions).to.have.lengthOf(1);
    expect(resolutions[0].args.favorable).to.equal(false);
    expect(resolutions[0].args.decisionHash).to.equal(DECISION_HASH);
  });

  it("resolve exige el hash de la decisión (RF-39)", async function () {
    const { registry, mock } = await loadFixture(deployFixture);
    const claimId = freshClaimId();
    await registry.registerClaim(claimId, EVIDENCE_ID);
    await mock.setMode(MockMode.Reject);
    await registry.submitInference(
      claimId, SAMPLE_PROOF, SAMPLE_INSTANCES, HIGH_CONFIDENCE, false, true,
    );
    await registry.escalate(claimId);
    await expect(
      registry.resolve(claimId, true, ethers.ZeroHash),
    ).to.be.revertedWithCustomError(registry, "MissingDecisionHash");
  });

  // adr-007 distingue los dos caminos de Aprobable a Aprobado y de esa
  // distinción dependen los textos de MK-03 y MK-04 y la traza que MK-08 le
  // muestra al auditor externo. Mientras el contrato asentaba una única causa
  // Settlement, esos tres dibujos no tenían de dónde elegir su texto.
  it("la causa de la liquidación distingue el camino automático del operador (adr-007)", async function () {
    const casos = [
      { automatic: true, esperada: Cause.AutomaticSettlement },
      { automatic: false, esperada: Cause.OperatorSettlement },
    ];

    for (const { automatic, esperada } of casos) {
      const { registry } = await loadFixture(deployFixture);
      const claimId = freshClaimId();
      await registry.registerClaim(claimId, EVIDENCE_ID);
      await registry.submitInference(
        claimId, SAMPLE_PROOF, SAMPLE_INSTANCES, HIGH_CONFIDENCE, false, true,
      );

      await expect(registry.settle(claimId, automatic))
        .to.emit(registry, "StateTransition")
        .withArgs(
          claimId,
          Status.Approvable,
          Status.Approved,
          esperada,
          ethers.ZeroHash,
        );

      // El estado de llegada es el mismo por los dos caminos: lo único que
      // cambia es la causa, que es justamente lo que el auditor lee.
      const [status] = await registry.claims(claimId);
      expect(status).to.equal(Status.Approved);
    }
  });

  it("los dos valores de causa de liquidación son distintos entre sí", async function () {
    expect(Cause.AutomaticSettlement).to.not.equal(Cause.OperatorSettlement);
  });
});

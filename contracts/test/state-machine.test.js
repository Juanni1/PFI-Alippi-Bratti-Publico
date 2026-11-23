// RNF-16: recorrido exhaustivo de la matriz de transiciones de adr-001.
// Las nueve permitidas con prueba positiva y todas las prohibidas con
// prueba negativa (RF-31: error explícito). Cada intento corre sobre un
// reclamo propio llevado al estado de origen solo por vías permitidas.
const { expect } = require("chai");
const { loadFixture } = require("@nomicfoundation/hardhat-toolbox/network-helpers");
const {
  Status,
  MockMode,
  HIGH_CONFIDENCE,
  LOW_CONFIDENCE,
  SAMPLE_PROOF,
  SAMPLE_INSTANCES,
  EVIDENCE_ID,
  DECISION_HASH,
  freshClaimId,
  deployFixture,
  driveTo,
} = require("./helpers");

// Matriz de adr-001, transcripta como pares permitidos (origen, destino).
// Nueve transiciones; la entrada a Registered va aparte (registerClaim).
const ALLOWED = [
  [Status.Registered, Status.Approvable],
  [Status.Registered, Status.ReviewRequired],
  [Status.Registered, Status.EvidenceInconsistency],
  [Status.Registered, Status.VerificationFailed],
  [Status.VerificationFailed, Status.ReviewRequired],
  [Status.Approvable, Status.Approved],
  [Status.ReviewRequired, Status.Approved],
  [Status.ReviewRequired, Status.Rejected],
  [Status.EvidenceInconsistency, Status.Rejected],
];

const ALL_STATES = [
  Status.Registered,
  Status.Approvable,
  Status.ReviewRequired,
  Status.EvidenceInconsistency,
  Status.VerificationFailed,
  Status.Approved,
  Status.Rejected,
];

const STATE_NAMES = {
  [Status.Registered]: "Registered",
  [Status.Approvable]: "Approvable",
  [Status.ReviewRequired]: "ReviewRequired",
  [Status.EvidenceInconsistency]: "EvidenceInconsistency",
  [Status.VerificationFailed]: "VerificationFailed",
  [Status.Approved]: "Approved",
  [Status.Rejected]: "Rejected",
};

// Cada intento es una función del contrato que produce un destino
// concreto. allowedFrom sale de la tabla de condiciones de adr-001.
const ATTEMPTS = [
  {
    name: "submitInference favorable (-> Approvable)",
    target: Status.Approvable,
    allowedFrom: [Status.Registered],
    run: (registry, _mock, claimId) =>
      registry.submitInference(
        claimId, SAMPLE_PROOF, SAMPLE_INSTANCES, HIGH_CONFIDENCE, false, true,
      ),
  },
  {
    name: "submitInference con confianza baja (-> ReviewRequired)",
    target: Status.ReviewRequired,
    allowedFrom: [Status.Registered],
    run: (registry, _mock, claimId) =>
      registry.submitInference(
        claimId, SAMPLE_PROOF, SAMPLE_INSTANCES, LOW_CONFIDENCE, false, true,
      ),
  },
  {
    name: "submitInference inconsistente (-> EvidenceInconsistency)",
    target: Status.EvidenceInconsistency,
    allowedFrom: [Status.Registered],
    run: (registry, _mock, claimId) =>
      registry.submitInference(
        claimId, SAMPLE_PROOF, SAMPLE_INSTANCES, HIGH_CONFIDENCE, false, false,
      ),
  },
  {
    name: "submitInference con prueba invalida (-> VerificationFailed)",
    target: Status.VerificationFailed,
    allowedFrom: [Status.Registered],
    run: async (registry, mock, claimId) => {
      await mock.setMode(MockMode.Reject);
      try {
        return await registry.submitInference(
          claimId, SAMPLE_PROOF, SAMPLE_INSTANCES, HIGH_CONFIDENCE, false, true,
        );
      } finally {
        await mock.setMode(MockMode.Accept);
      }
    },
  },
  {
    name: "escalate (-> ReviewRequired)",
    target: Status.ReviewRequired,
    allowedFrom: [Status.VerificationFailed],
    run: (registry, _mock, claimId) => registry.escalate(claimId),
  },
  {
    name: "settle (-> Approved)",
    target: Status.Approved,
    allowedFrom: [Status.Approvable],
    run: (registry, _mock, claimId) => registry.settle(claimId, false),
  },
  {
    name: "resolve favorable (-> Approved)",
    target: Status.Approved,
    allowedFrom: [Status.ReviewRequired],
    run: (registry, _mock, claimId) =>
      registry.resolve(claimId, true, DECISION_HASH),
  },
  {
    name: "resolve desfavorable (-> Rejected)",
    target: Status.Rejected,
    allowedFrom: [Status.ReviewRequired, Status.EvidenceInconsistency],
    run: (registry, _mock, claimId) =>
      registry.resolve(claimId, false, DECISION_HASH),
  },
];

describe("máquina de estados (RNF-16)", function () {
  it("los intentos de la suite cubren exactamente las nueve transiciones de adr-001", function () {
    // Control de la propia suite: los pares (origen, destino) derivados de
    // los intentos tienen que coincidir con la matriz transcripta.
    const derived = ATTEMPTS.flatMap((attempt) =>
      attempt.allowedFrom.map((from) => `${from}->${attempt.target}`),
    ).sort();
    const declared = ALLOWED.map(([from, to]) => `${from}->${to}`).sort();
    expect(derived).to.deep.equal(declared);
    expect(declared).to.have.lengthOf(9);
  });

  for (const from of ALL_STATES) {
    for (const attempt of ATTEMPTS) {
      const allowed = attempt.allowedFrom.includes(from);
      const label = `${allowed ? "permite" : "prohíbe"} ${attempt.name} desde ${STATE_NAMES[from]}`;
      it(label, async function () {
        const { registry, mock } = await loadFixture(deployFixture);
        const claimId = await driveTo(registry, mock, from);
        if (allowed) {
          await attempt.run(registry, mock, claimId);
          const claim = await registry.claims(claimId);
          expect(claim.status).to.equal(attempt.target);
        } else {
          await expect(attempt.run(registry, mock, claimId))
            .to.be.revertedWithCustomError(registry, "InvalidTransition")
            .withArgs(claimId, from);
        }
      });
    }
  }

  it("rechaza registrar dos veces el mismo reclamo", async function () {
    const { registry } = await loadFixture(deployFixture);
    const claimId = freshClaimId();
    await registry.registerClaim(claimId, EVIDENCE_ID);
    await expect(registry.registerClaim(claimId, EVIDENCE_ID))
      .to.be.revertedWithCustomError(registry, "ClaimAlreadyExists")
      .withArgs(claimId);
  });

  it("rechaza registrar sin identificador de evidencia", async function () {
    const { registry } = await loadFixture(deployFixture);
    await expect(
      registry.registerClaim(freshClaimId(), "0x" + "00".repeat(32)),
    ).to.be.revertedWithCustomError(registry, "MissingEvidence");
  });

  it("rechaza toda operación sobre un reclamo inexistente", async function () {
    const { registry } = await loadFixture(deployFixture);
    const ghost = freshClaimId();
    await expect(registry.settle(ghost, false))
      .to.be.revertedWithCustomError(registry, "UnknownClaim")
      .withArgs(ghost);
    await expect(registry.escalate(ghost))
      .to.be.revertedWithCustomError(registry, "UnknownClaim");
    await expect(registry.resolve(ghost, true, DECISION_HASH))
      .to.be.revertedWithCustomError(registry, "UnknownClaim");
    await expect(
      registry.submitInference(
        ghost, SAMPLE_PROOF, SAMPLE_INSTANCES, HIGH_CONFIDENCE, false, true,
      ),
    ).to.be.revertedWithCustomError(registry, "UnknownClaim");
  });

  it("invariante 5 de adr-001: el camino desde VerificationFailed a Approved pasa por resolución humana", async function () {
    const { registry, mock } = await loadFixture(deployFixture);
    const claimId = await driveTo(registry, mock, Status.VerificationFailed);
    await registry.escalate(claimId);
    await registry.resolve(claimId, true, DECISION_HASH);
    const claim = await registry.claims(claimId);
    expect(claim.status).to.equal(Status.Approved);
  });
});

// RNF-21 / RF-25: toda función que muta estado queda inaccesible desde una
// cuenta que no sea la del oráculo, con prueba negativa por función. La
// lista incluye setVerifier: no es una transición, pero la garantía
// criptográfica entera depende de ella (RF-49).
const { expect } = require("chai");
const { ethers } = require("hardhat");
const { loadFixture } = require("@nomicfoundation/hardhat-toolbox/network-helpers");
const {
  HIGH_CONFIDENCE,
  SAMPLE_PROOF,
  SAMPLE_INSTANCES,
  EVIDENCE_ID,
  DECISION_HASH,
  freshClaimId,
  deployFixture,
} = require("./helpers");

describe("restricción al oráculo (RNF-21)", function () {
  const CASES = [
    {
      name: "registerClaim",
      call: (registry) => registry.registerClaim(freshClaimId(), EVIDENCE_ID),
    },
    {
      name: "submitInference",
      call: (registry) =>
        registry.submitInference(
          freshClaimId(), SAMPLE_PROOF, SAMPLE_INSTANCES,
          HIGH_CONFIDENCE, false, true,
        ),
    },
    { name: "escalate", call: (registry) => registry.escalate(freshClaimId()) },
    { name: "settle", call: (registry) => registry.settle(freshClaimId(), false) },
    {
      name: "resolve",
      call: (registry) => registry.resolve(freshClaimId(), true, DECISION_HASH),
    },
    {
      name: "setVerifier",
      call: (registry, mock) => registry.setVerifier(mock.target),
    },
    {
      name: "setConfidenceThreshold",
      call: (registry) => registry.setConfidenceThreshold(1),
    },
    {
      name: "setHumanConfirmationMode",
      call: (registry) => registry.setHumanConfirmationMode(true),
    },
  ];

  for (const { name, call } of CASES) {
    it(`rechaza ${name} desde una cuenta ajena al oráculo`, async function () {
      const { registry, mock, outsider } = await loadFixture(deployFixture);
      await expect(
        call(registry.connect(outsider), mock),
      ).to.be.revertedWithCustomError(registry, "NotOracle");
    });
  }

  it("el despliegue rechaza oráculo o verificador en cero", async function () {
    const { mock, oracle } = await loadFixture(deployFixture);
    const ClaimRegistry = await ethers.getContractFactory("ClaimRegistry");
    const zero = "0x" + "00".repeat(20);
    await expect(
      ClaimRegistry.deploy(zero, mock.target),
    ).to.be.revertedWithCustomError(ClaimRegistry, "ZeroAddress");
    await expect(
      ClaimRegistry.deploy(oracle.address, zero),
    ).to.be.revertedWithCustomError(ClaimRegistry, "ZeroAddress");
  });
});

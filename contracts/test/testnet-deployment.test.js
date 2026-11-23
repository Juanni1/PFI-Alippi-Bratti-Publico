const { expect } = require("chai");
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const hre = require("hardhat");
const { ethers } = require("ethers");
const vm = require("vm");

const script = path.join(__dirname, "..", "scripts", "deploy-testnet.js");
const output = path.join(__dirname, "testnet-deployment-fixture.json");
const config = require("../config/base-sepolia.json");
const { run } = require("../scripts/deploy-testnet");
const testNetwork = { ...config, chainId: hre.network.config.chainId }; // Red efímera de Hardhat, nunca RPC pública.
const key = "0x" + "11".repeat(32);

function invoke(extra = {}) {
  return spawnSync(process.execPath, [script], {
    encoding: "utf8",
    env: { ...process.env, L2_RPC_URL: "", L2_PRIVATE_KEY: "", TESTNET_ORACLE_ADDRESS: "", TESTNET_OUTPUT: "", TESTNET_MAX_WEI: "", TESTNET_SEND: "", ...extra },
  });
}

function rejected(result, message) {
  expect(result.status).to.equal(1);
  expect(result.stdout).to.equal("");
  expect(result.stderr).to.equal(`Error: ${message}\n`);
  expect(fs.existsSync(output)).to.equal(false);
}

function feeOracle(extra = 0n) {
  return { getL1FeeUpperBound: async () => extra, getOperatorFee: async () => extra };
}

async function fundedWallet() {
  const [funder] = await hre.ethers.getSigners();
  const wallet = ethers.Wallet.createRandom().connect(hre.ethers.provider);
  await (await funder.sendTransaction({ to: wallet.address, value: ethers.parseEther("2") })).wait();
  return wallet;
}

describe("despliegue testnet seguro", function () {
  before(function () {
    if (hre.__SOLIDITY_COVERAGE_RUNNING) {
      this.skip();
    }
  });

  afterEach(function () {
    if (fs.existsSync(output)) fs.unlinkSync(output);
    const pending = `${output}.pending-${process.pid}`;
    if (fs.existsSync(pending)) fs.unlinkSync(pending);
  });

  it("rechaza destino ausente sin RPC ni envio", function () {
    rejected(invoke(), "Indique TESTNET_OUTPUT para el artefacto.");
  });

  it("rechaza un artefacto existente sin sobrescribirlo", function () {
    fs.writeFileSync(output, "identidad previa\n");
    const result = invoke({ TESTNET_OUTPUT: output });
    expect(result.status).to.equal(1);
    expect(result.stdout).to.equal("");
    expect(result.stderr).to.equal("Error: El artefacto de destino ya existe.\n");
    expect(fs.readFileSync(output, "utf8")).to.equal("identidad previa\n");
  });

  it("rechaza credenciales ausentes sin revelar el proveedor", function () {
    rejected(invoke({ TESTNET_OUTPUT: output, L2_RPC_URL: "https://secret.example.invalid/token" }), "Indique L2_RPC_URL y L2_PRIVATE_KEY.");
  });

  it("rechaza limite de gasto ausente antes de consultar RPC", function () {
    rejected(invoke({ TESTNET_OUTPUT: output, L2_RPC_URL: "https://secret.example.invalid/token", L2_PRIVATE_KEY: key, TESTNET_ORACLE_ADDRESS: "0x1111111111111111111111111111111111111111" }), "Indique TESTNET_MAX_WEI como entero positivo.");
  });

  it("rechaza confirmacion de envio invalida antes de consultar RPC", function () {
    rejected(invoke({ TESTNET_OUTPUT: output, L2_RPC_URL: "https://secret.example.invalid/token", L2_PRIVATE_KEY: key, TESTNET_ORACLE_ADDRESS: "0x1111111111111111111111111111111111111111", TESTNET_MAX_WEI: "1", TESTNET_SEND: "yes" }), "TESTNET_SEND solo admite SI_ENVIO_TRANSACCIONES.");
  });

  it("exige identidad de oraculo explícita antes de consultar RPC", function () {
    rejected(invoke({ TESTNET_OUTPUT: output, L2_RPC_URL: "https://secret.example.invalid/token", L2_PRIVATE_KEY: key }), "Indique TESTNET_ORACLE_ADDRESS válido.");
  });

  it("rechaza red equivocada sin modificar nonce, balance o salida", async function () {
    const { run } = require("../scripts/deploy-testnet");
    const signer = (await hre.ethers.getSigners())[0];
    const nonce = await signer.getNonce();
    const balance = await hre.ethers.provider.getBalance(signer.address);
    try {
      await run({ output, rpcUrl: "aislado", privateKey: key, oracleAddress: signer.address, maxWei: "999999999999999999", send: false }, { provider: hre.ethers.provider, signer });
      expect.fail("Debio rechazar la red");
    } catch (error) {
      expect(error.message).to.equal("La RPC no coincide con la red configurada.");
    }
    expect(await signer.getNonce()).to.equal(nonce);
    expect(await hre.ethers.provider.getBalance(signer.address)).to.equal(balance);
    expect(fs.existsSync(output)).to.equal(false);
  });

  it("rechaza oráculo diferente sin enviar ni guardar", async function () {
    const [signer, other] = await hre.ethers.getSigners();
    const nonce = await signer.getNonce();
    const balance = await hre.ethers.provider.getBalance(signer.address);
    try {
      await run({ output, rpcUrl: "aislado", privateKey: key, oracleAddress: other.address,
        maxWei: "999999999999999999", send: "SI_ENVIO_TRANSACCIONES" },
      { provider: hre.ethers.provider, signer, network: testNetwork, feeOracle: feeOracle() });
      expect.fail("Debio rechazar el oráculo");
    } catch (error) {
      expect(error.message).to.equal("El firmante no corresponde a la RPC.");
    }
    expect(await signer.getNonce()).to.equal(nonce);
    expect(await hre.ethers.provider.getBalance(signer.address)).to.equal(balance);
    expect(fs.existsSync(output)).to.equal(false);
  });

  it("preflight aislado no firma ni envia ni escribe, con limite positivo", async function () {
    const signer = (await hre.ethers.getSigners())[0];
    const nonce = await signer.getNonce();
    const balance = await hre.ethers.provider.getBalance(signer.address);
    const fee = await hre.ethers.provider.getFeeData();
    expect(fee.maxFeePerGas !== null, "maxFeePerGas").to.equal(true);
    expect(fee.maxPriorityFeePerGas !== null, "maxPriorityFeePerGas").to.equal(true);
    const verifierArtifact = await hre.artifacts.readArtifact("Halo2Verifier");
    const estimated = await hre.ethers.provider.estimateGas({ from: signer.address, data: verifierArtifact.bytecode });
    expect(estimated <= BigInt(config.gasLimits.verificador), `verificador gas ${estimated}`).to.equal(true);
    const registryArtifact = await hre.artifacts.readArtifact("ClaimRegistry");
    const factory = new hre.ethers.ContractFactory(registryArtifact.abi, registryArtifact.bytecode, signer);
    const address = hre.ethers.getCreateAddress({ from: signer.address, nonce });
    const registryTx = await factory.getDeployTransaction(signer.address, address);
    const registryGas = await hre.ethers.provider.estimateGas({ ...registryTx, from: signer.address });
    expect(registryGas <= BigInt(config.gasLimits.registro), `registro gas ${registryGas}`).to.equal(true);
    const plan = await run({ output, rpcUrl: "aislado", privateKey: key, oracleAddress: signer.address, maxWei: "999999999999999999", send: false },
      { provider: hre.ethers.provider, signer, network: testNetwork, feeOracle: feeOracle() });
    expect(plan.chainId).to.equal(testNetwork.chainId);
    expect(plan.oraculo).to.equal(signer.address);
    expect(await signer.getNonce()).to.equal(nonce);
    expect(await hre.ethers.provider.getBalance(signer.address)).to.equal(balance);
    expect(fs.existsSync(output)).to.equal(false);
  });

  it("rechaza gasto insuficiente sin enviar ni guardar", async function () {
    const signer = (await hre.ethers.getSigners())[0];
    const nonce = await signer.getNonce();
    const balance = await hre.ethers.provider.getBalance(signer.address);
    try {
      await run({ output, rpcUrl: "aislado", privateKey: key, oracleAddress: signer.address, maxWei: "1", send: "SI_ENVIO_TRANSACCIONES" },
        { provider: hre.ethers.provider, signer, network: testNetwork, feeOracle: feeOracle() });
      expect.fail("Debio rechazar el gasto");
    } catch (error) {
      expect(error.message).to.equal("El saldo o el límite de gasto no cubre el gas máximo.");
    }
    expect(await signer.getNonce()).to.equal(nonce);
    expect(await hre.ethers.provider.getBalance(signer.address)).to.equal(balance);
    expect(fs.existsSync(output)).to.equal(false);
  });

  it("envia solo en red efimera y guarda recibos y contratos comprobables", async function () {
    const signer = await fundedWallet();
    const before = await signer.getNonce();
    const artifact = await run({ output, rpcUrl: "aislado", privateKey: signer.privateKey, oracleAddress: signer.address, maxWei: "999999999999999999", send: "SI_ENVIO_TRANSACCIONES" },
      { provider: hre.ethers.provider, signer, network: testNetwork, feeOracle: feeOracle() });
    expect(await signer.getNonce()).to.equal(before + 4);
    expect(JSON.parse(fs.readFileSync(output, "utf8"))).to.deep.equal(artifact);
    expect(artifact.red).to.deep.equal({ nombre: config.nombre, chainId: testNetwork.chainId });
    expect(artifact.bytecodeDelVerificador.bytes).to.be.greaterThan(0);
    expect(artifact.bytecodeDelVerificador.bytes).to.be.at.most(24576);
    expect(artifact.abi).to.be.an("array").that.is.not.empty;
    expect(artifact.codigo.verificadorHash).to.equal(artifact.codigo.verificadorEsperadoHash);
    expect(artifact.codigo.registroHash).to.equal(artifact.codigo.registroEsperadoHash);
    for (const receipt of Object.values(artifact.recibos)) {
      expect(receipt.estado).to.equal(1);
      expect((await hre.ethers.provider.getTransactionReceipt(receipt.hash)).status).to.equal(1);
    }
    const registry = await hre.ethers.getContractAt("ClaimRegistry", artifact.registro);
    expect(await registry.oracle()).to.equal(artifact.oraculo);
    expect(await registry.verifier()).to.equal(artifact.verificador);
    expect(await registry.confidenceThresholdBps()).to.equal(BigInt(config.umbralDeDerivacionBps));
    expect(await registry.humanConfirmationRequired()).to.equal(false);
    expect(JSON.stringify(artifact)).not.to.include("aislado");
    expect(JSON.stringify(artifact)).not.to.include(key);
  });

  it("rechaza nonce pendiente sin crear identidad ni enviar", async function () {
    const wallet = await fundedWallet();
    const original = hre.ethers.provider.getTransactionCount.bind(hre.ethers.provider);
    const provider = new Proxy(hre.ethers.provider, { get(target, key) {
      if (key === "getTransactionCount") return async (address, tag) => (await original(address, tag)) + (tag === "pending" ? 1 : 0);
      const value = target[key];
      return typeof value === "function" ? value.bind(target) : value;
    } });
    const nonce = await wallet.getNonce();
    try {
      await run({ output, rpcUrl: "aislado", privateKey: wallet.privateKey, oracleAddress: wallet.address,
        maxWei: "999999999999999999", send: "SI_ENVIO_TRANSACCIONES" },
      { provider, signer: wallet.connect(provider), network: testNetwork, feeOracle: feeOracle() });
      expect.fail("Debio rechazar nonce pendiente");
    } catch (error) { expect(error.message).to.equal("El oráculo tiene transacciones pendientes."); }
    expect(await wallet.getNonce()).to.equal(nonce);
    expect(fs.existsSync(output)).to.equal(false);
  });

  it("incluye L1 y operador; rechaza una cotización que supera el presupuesto", async function () {
    const signer = (await hre.ethers.getSigners())[0];
    const nonce = await signer.getNonce();
    try {
      await run({ output, rpcUrl: "aislado", privateKey: key, oracleAddress: signer.address,
        maxWei: "999999999999999999", send: "SI_ENVIO_TRANSACCIONES" },
      { provider: hre.ethers.provider, signer, network: testNetwork, feeOracle: feeOracle(10n ** 18n) });
      expect.fail("Debio rechazar presupuesto");
    } catch (error) { expect(error.message).to.equal("El saldo o el límite de gasto no cubre el gas máximo."); }
    expect(await signer.getNonce()).to.equal(nonce);
    expect(fs.existsSync(output)).to.equal(false);
  });

  it("rechaza cotizaciones L1 ausentes sin interpretarlas como cero", async function () {
    const signer = (await hre.ethers.getSigners())[0];
    const nonce = await signer.getNonce();
    try {
      await run({ output, rpcUrl: "aislado", privateKey: key, oracleAddress: signer.address,
        maxWei: "999999999999999999", send: "SI_ENVIO_TRANSACCIONES" },
      { provider: hre.ethers.provider, signer, network: testNetwork, feeOracle: {} });
      expect.fail("Debio rechazar oracle ausente");
    } catch (error) { expect(error.message).to.equal("La RPC no ofrece cotizaciones L1 y operador utilizables."); }
    expect(await signer.getNonce()).to.equal(nonce);
    expect(fs.existsSync(output)).to.equal(false);
  });

  it("acepta prioridad cero en preflight aislado sin enviar", async function () {
    const signer = (await hre.ethers.getSigners())[0];
    const provider = new Proxy(hre.ethers.provider, { get(target, property) {
      if (property === "getFeeData") return async () => ({ ...await target.getFeeData(), maxPriorityFeePerGas: 0n });
      const value = target[property];
      return typeof value === "function" ? value.bind(target) : value;
    } });
    const nonce = await signer.getNonce();
    const injectedSigner = Object.create(signer);
    Object.defineProperty(injectedSigner, "provider", { value: provider });
    const plan = await run({ output, rpcUrl: "aislado", privateKey: key, oracleAddress: signer.address,
      maxWei: "999999999999999999", send: false },
    { provider, signer: injectedSigner, network: testNetwork, feeOracle: feeOracle() });
    expect(plan.oraculo).to.equal(signer.address);
    expect(await signer.getNonce()).to.equal(nonce);
    expect(fs.existsSync(output)).to.equal(false);
  });

  it("conserva identidad y hash previo si la RPC confirma envío pero devuelve error", async function () {
    const wallet = await fundedWallet();
    let sentHash;
    const provider = new Proxy(hre.ethers.provider, { get(target, property) {
      if (property === "broadcastTransaction") return async (raw) => {
        const response = await target.broadcastTransaction(raw);
        sentHash = response.hash;
        throw new Error("RPC incierta con URL sensible");
      };
      const value = target[property];
      return typeof value === "function" ? value.bind(target) : value;
    } });
    const signer = wallet.connect(provider);
    const nonce = await wallet.getNonce();
    try {
      await run({ output, rpcUrl: "aislado", privateKey: wallet.privateKey, oracleAddress: wallet.address,
        maxWei: "999999999999999999", send: "SI_ENVIO_TRANSACCIONES" },
      { provider, signer, network: testNetwork, feeOracle: feeOracle() });
      expect.fail("Debio informar RPC incierta");
    } catch (error) { expect(error.message).to.include("RPC incierta"); }
    const pending = JSON.parse(fs.readFileSync(output, "utf8"));
    expect(pending.estado).to.equal("pendiente");
    expect(pending.nonceInicial).to.equal(nonce);
    expect(pending.transacciones.verificador.hash).to.equal(sentHash);
    expect(pending).not.to.have.property("abi");
    expect(JSON.stringify(pending)).not.to.include(wallet.privateKey);
  });

  it("conserva el último registro íntegro si falla el reemplazo antes de enviar", async function () {
    const wallet = await fundedWallet();
    const nonce = await wallet.getNonce();
    const rename = fs.renameSync;
    fs.renameSync = () => { throw new Error("fallo de archivo simulado"); };
    try {
      await run({ output, rpcUrl: "aislado", privateKey: wallet.privateKey, oracleAddress: wallet.address,
        maxWei: "999999999999999999", send: "SI_ENVIO_TRANSACCIONES" },
      { provider: hre.ethers.provider, signer: wallet, network: testNetwork, feeOracle: feeOracle() });
      expect.fail("Debio fallar escritura");
    } catch (error) { expect(error.message).to.equal("fallo de archivo simulado"); }
    finally { fs.renameSync = rename; }
    const record = JSON.parse(fs.readFileSync(output, "utf8"));
    expect(record.estado).to.equal("pendiente");
    expect(record.nonceInicial).to.equal(nonce);
    expect(record.transacciones).to.deep.equal({});
    expect(await wallet.getNonce()).to.equal(nonce);
  });

  it("rechaza cambio de nonce antes del envío sin reemplazar identidad", async function () {
    const wallet = await fundedWallet();
    const nonce = await wallet.getNonce();
    let checks = 0;
    const provider = new Proxy(hre.ethers.provider, { get(target, property) {
      if (property === "getTransactionCount") return async (...args) => {
        const value = await target.getTransactionCount(...args);
        return args[1] === "pending" && ++checks > 1 ? value + 1 : value;
      };
      const value = target[property];
      return typeof value === "function" ? value.bind(target) : value;
    } });
    try {
      await run({ output, rpcUrl: "aislado", privateKey: wallet.privateKey, oracleAddress: wallet.address,
        maxWei: "999999999999999999", send: "SI_ENVIO_TRANSACCIONES" },
      { provider, signer: wallet.connect(provider), network: testNetwork, feeOracle: feeOracle() });
      expect.fail("Debio rechazar cambio de nonce");
    } catch (error) { expect(error.message).to.equal("El nonce del oráculo cambió; concilie el artefacto pendiente."); }
    expect(JSON.parse(fs.readFileSync(output, "utf8")).nonceInicial).to.equal(nonce);
    expect(await wallet.getNonce()).to.equal(nonce);
  });

  it("rechaza bytecode alterado sin producir artefacto final", async function () {
    const wallet = await fundedWallet();
    let calls = 0;
    const provider = new Proxy(hre.ethers.provider, { get(target, property) {
      if (property === "getCode") return async (...args) => {
        const code = await target.getCode(...args);
        if (code !== "0x" && ++calls === 1) return "0x6000";
        return code;
      };
      const value = target[property];
      return typeof value === "function" ? value.bind(target) : value;
    } });
    try {
      await run({ output, rpcUrl: "aislado", privateKey: wallet.privateKey, oracleAddress: wallet.address,
        maxWei: "999999999999999999", send: "SI_ENVIO_TRANSACCIONES" },
      { provider, signer: wallet.connect(provider), network: testNetwork, feeOracle: feeOracle() });
      expect.fail("Debio rechazar codigo incorrecto");
    } catch (error) { expect(error.message).to.equal("El bytecode desplegado no coincide con el compilado."); }
    expect(JSON.parse(fs.readFileSync(output, "utf8")).estado).to.equal("pendiente");
  });

  it("ejecuta el script local real sin tocar el artefacto personal", async function () {
    const source = fs.readFileSync(path.join(__dirname, "..", "scripts", "deploy-local.js"), "utf8");
    let content;
    const messages = [];
    const localFs = { writeFileSync(destination, data) {
      expect(path.basename(destination)).to.equal("despliegue-local.json");
      content = JSON.parse(data);
    } };
    const localRequire = (name) => name === "fs" ? localFs : name === "hardhat" ? hre : require(name);
    vm.runInNewContext(source, { require: localRequire, __dirname: path.dirname(script),
      process: { env: {}, exitCode: 0 }, console: { log: (message) => messages.push(message), error: (error) => { throw error; } } });
    // main() es asíncrono en el script público y concluye al producir salida.
    for (let i = 0; !content && i < 300; i++) await new Promise((resolve) => setTimeout(resolve, 10));
    expect(content, messages.join("\n")).to.be.an("object");
    const registry = await hre.ethers.getContractAt("ClaimRegistry", content.registro);
    expect(content.red.nombre).to.equal(hre.network.name);
    expect(content.red.chainId).to.equal(Number((await hre.ethers.provider.getNetwork()).chainId));
    expect(content.oraculo).to.equal(await registry.oracle());
    expect(content.verificador).to.equal(await registry.verifier());
    expect(content.umbralDeDerivacionBps).to.equal(9000);
    expect(content.bytecodeDelVerificador.bytes).to.be.greaterThan(0);
    expect(content.abi).to.be.an("array").that.is.not.empty;
    expect(await registry.confidenceThresholdBps()).to.equal(9000n);
    expect(await registry.humanConfirmationRequired()).to.equal(false);
  });
});

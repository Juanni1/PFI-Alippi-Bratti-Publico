// Entrada remota: por defecto solo consulta la red; el envío exige confirmación.
const fs = require("fs");
const { ethers } = require("ethers");
const network = require("../config/base-sepolia.json");
const EIP170_MAX_CODE_SIZE = 24576;

function validate(options) {
  if (!options.output) throw new Error("Indique TESTNET_OUTPUT para el artefacto.");
  if (fs.existsSync(options.output)) throw new Error("El artefacto de destino ya existe.");
  if (!options.rpcUrl || !options.privateKey) throw new Error("Indique L2_RPC_URL y L2_PRIVATE_KEY.");
  if (!ethers.isAddress(options.oracleAddress || "")) throw new Error("Indique TESTNET_ORACLE_ADDRESS válido.");
  if (!/^[1-9][0-9]*$/.test(options.maxWei || "")) throw new Error("Indique TESTNET_MAX_WEI como entero positivo.");
  if (options.send && options.send !== "SI_ENVIO_TRANSACCIONES") {
    throw new Error("TESTNET_SEND solo admite SI_ENVIO_TRANSACCIONES.");
  }
  if (!ethers.isHexString(options.privateKey, 32)) throw new Error("La clave del oráculo no es válida.");
  if (!Number.isInteger(network.umbralDeDerivacionBps) || network.umbralDeDerivacionBps < 1 || network.umbralDeDerivacionBps > 10000) {
    throw new Error("El umbral configurado no es válido.");
  }
}

function syncDirectory(filename) {
  // Algunas plataformas no permiten fsync en directorios; el archivo ya fue fsync.
  try {
    const fd = fs.openSync(require("path").dirname(filename), "r");
    try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  } catch (error) {
    if (!["EINVAL", "EISDIR", "ENOTSUP", "EBADF"].includes(error.code)) throw error;
  }
}

function writeRecord(filename, data, first = false) {
  const target = first ? filename : `${filename}.pending-${process.pid}`;
  const fd = fs.openSync(target, "wx", 0o600);
  try {
    fs.writeFileSync(fd, JSON.stringify(data, null, 2) + "\n");
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  if (!first) fs.renameSync(target, filename);
  syncDirectory(filename);
}

function runtimeIdentity(artifact, address, registry = false) {
  if (!registry) return artifact.deployedBytecode;
  // Solo se sustituyen las referencias inmutables definidas por solc.
  const refs = artifact.immutableReferences;
  if (!refs || !Object.keys(refs).length) throw new Error("Faltan referencias inmutables del registro.");
  let code = artifact.deployedBytecode.slice(2);
  for (const positions of Object.values(refs)) {
    for (const { start, length } of positions) {
      if (length !== 32) throw new Error("Referencia inmutable no reconocida.");
      const word = address.toLowerCase().slice(2).padStart(length * 2, "0");
      code = code.slice(0, start * 2) + word + code.slice((start + length) * 2);
    }
  }
  return `0x${code}`;
}

function confirmed(receipt, expected, label) {
  if (!receipt || receipt.status !== 1 ||
      (receipt.contractAddress && receipt.contractAddress.toLowerCase() !== expected.toLowerCase())) {
    throw new Error(`No se confirmó ${label}; consulte la cadena antes de reintentar.`);
  }
  return { hash: receipt.hash, bloque: receipt.blockNumber, gasUsado: receipt.gasUsed.toString(), estado: receipt.status };
}

async function quote(oracle, tx, gasLimit, maxFeePerGas) {
  // La firma serializada varía; 300 bytes adicionales cubren campos y firma.
  const size = (tx.data.length - 2) / 2 + 300;
  const [l1, operator] = await Promise.all([
    oracle.getL1FeeUpperBound(size), oracle.getOperatorFee(gasLimit),
  ]);
  if (typeof l1 !== "bigint" || typeof operator !== "bigint" || l1 < 0n || operator < 0n) {
    throw new Error("La RPC no ofrece cotizaciones L1 y operador utilizables.");
  }
  return { total: BigInt(gasLimit) * maxFeePerGas + l1 + operator, l1, operator };
}

async function run(options, injected = {}) {
  validate(options);
  // Las dependencias inyectadas se usan solo en el nodo efímero de pruebas.
  const provider = injected.provider || new ethers.JsonRpcProvider(options.rpcUrl);
  const signer = injected.signer || new ethers.Wallet(options.privateKey, provider);
  const expected = injected.network || network;
  const chain = await provider.getNetwork();
  if (Number(chain.chainId) !== expected.chainId) throw new Error("La RPC no coincide con la red configurada.");
  const address = await signer.getAddress();
  if (!ethers.isAddress(address) || address === ethers.ZeroAddress || signer.provider !== provider ||
      address.toLowerCase() !== options.oracleAddress.toLowerCase()) {
    throw new Error("El firmante no corresponde a la RPC.");
  }
  const hre = require("hardhat");
  const verifierArtifact = await hre.artifacts.readArtifact("Halo2Verifier");
  const registryArtifact = await hre.artifacts.readArtifact("ClaimRegistry");
  const size = (verifierArtifact.deployedBytecode.length - 2) / 2;
  if (size <= 0 || size > EIP170_MAX_CODE_SIZE) throw new Error("El verificador supera EIP-170.");
  const latest = await provider.getTransactionCount(address, "latest");
  const nonce = await provider.getTransactionCount(address, "pending");
  if (nonce !== latest) throw new Error("El oráculo tiene transacciones pendientes.");
  const verifierAddress = ethers.getCreateAddress({ from: address, nonce });
  const registryAddress = ethers.getCreateAddress({ from: address, nonce: nonce + 1 });
  if (await provider.getCode(verifierAddress) !== "0x" || await provider.getCode(registryAddress) !== "0x") {
    throw new Error("Las direcciones previstas ya tienen código.");
  }
  const verifierFactory = new ethers.ContractFactory(verifierArtifact.abi, verifierArtifact.bytecode);
  const registryFactory = new ethers.ContractFactory(registryArtifact.abi, registryArtifact.bytecode);
  const verifierTx = await verifierFactory.getDeployTransaction();
  const registryTx = await registryFactory.getDeployTransaction(address, verifierAddress);
  const iface = registryFactory.interface;
  const limits = expected.gasLimits;
  const transactions = [
    { label: "verificador", tx: verifierTx, gasLimit: limits.verificador, contract: verifierAddress },
    { label: "registro", tx: registryTx, gasLimit: limits.registro, contract: registryAddress },
    { label: "umbral", tx: { to: registryAddress, data: iface.encodeFunctionData("setConfidenceThreshold", [expected.umbralDeDerivacionBps]) }, gasLimit: limits.configuracion },
    { label: "confirmacionHumana", tx: { to: registryAddress, data: iface.encodeFunctionData("setHumanConfirmationMode", [false]) }, gasLimit: limits.configuracion },
  ];
  const feeOracle = injected.feeOracle || new ethers.Contract(expected.gasPriceOracle,
    ["function getL1FeeUpperBound(uint256) view returns (uint256)", "function getOperatorFee(uint256) view returns (uint256)"], provider);
  const [verifierGas, registryGas, fee, balance, block] = await Promise.all([
    provider.estimateGas({ ...verifierTx, from: address }),
    provider.estimateGas({ ...registryTx, from: address }),
    provider.getFeeData(), provider.getBalance(address), provider.getBlock("latest"),
  ]);
  if (!fee.maxFeePerGas || fee.maxPriorityFeePerGas === null ||
      fee.maxPriorityFeePerGas > fee.maxFeePerGas || !block ||
      verifierGas > BigInt(limits.verificador) || registryGas > BigInt(limits.registro) ||
      transactions.some(({ gasLimit }) => !Number.isSafeInteger(gasLimit) || gasLimit <= 0 || BigInt(gasLimit) > block.gasLimit)) {
    throw new Error("La estimación o los límites de gas de la RPC no son seguros.");
  }
  let quotes;
  try { quotes = await Promise.all(transactions.map(({ tx, gasLimit }) => quote(feeOracle, tx, gasLimit, fee.maxFeePerGas))); }
  catch { throw new Error("La RPC no ofrece cotizaciones L1 y operador utilizables."); }
  const exposure = quotes.reduce((sum, value) => sum + value.total, 0n);
  if (exposure > BigInt(options.maxWei) || balance < exposure) throw new Error("El saldo o el límite de gasto no cubre el gas máximo.");
  const plan = { red: expected.nombre, chainId: Number(chain.chainId), oraculo: address,
    verificador: verifierAddress, registro: registryAddress, nonceInicial: nonce,
    cotizacionesWei: quotes.map((value) => ({ total: value.total.toString(),
      l1: value.l1.toString(), operador: value.operator.toString() })), gastoEstimadoWei: exposure.toString() };
  if (options.send !== "SI_ENVIO_TRANSACCIONES") return plan;
  const wallet = new ethers.Wallet(options.privateKey);
  if (wallet.address.toLowerCase() !== address.toLowerCase()) throw new Error("El firmante no corresponde a la clave.");

  // La primera reserva es exclusiva. Ningún error posterior permite reiniciar este destino.
  const state = { estado: "pendiente", ...plan, transacciones: {} };
  writeRecord(options.output, state, true);
  let spentObserved = 0n;
  for (let i = 0; i < transactions.length; i++) {
    const { label, tx, gasLimit, contract } = transactions[i];
    const nextNonce = nonce + i;
    if (await provider.getTransactionCount(address, "pending") !== nextNonce) {
      throw new Error("El nonce del oráculo cambió; concilie el artefacto pendiente.");
    }
    const currentFee = await provider.getFeeData();
    if (!currentFee.maxFeePerGas || currentFee.maxPriorityFeePerGas === null ||
        currentFee.maxPriorityFeePerGas > currentFee.maxFeePerGas) throw new Error("La RPC no ofrece tarifa EIP-1559.");
    let remaining;
    try {
      remaining = await Promise.all(transactions.slice(i).map((item) => quote(feeOracle, item.tx, item.gasLimit, currentFee.maxFeePerGas)));
    } catch { throw new Error("La RPC no ofrece cotizaciones L1 y operador utilizables."); }
    const remainingTotal = remaining.reduce((sum, value) => sum + value.total, 0n);
    if (spentObserved + remainingTotal > BigInt(options.maxWei) || await provider.getBalance(address) < remainingTotal) {
      throw new Error("El saldo o el límite de gasto no cubre el gas máximo.");
    }
    const raw = await wallet.signTransaction({ ...tx, chainId: expected.chainId, type: 2, nonce: nextNonce,
      gasLimit, maxFeePerGas: currentFee.maxFeePerGas, maxPriorityFeePerGas: currentFee.maxPriorityFeePerGas, value: 0 });
    if (ethers.getBytes(raw).length > (tx.data.length - 2) / 2 + 300) {
      throw new Error("El tamaño firmado supera la cotización L1.");
    }
    const hash = ethers.keccak256(raw);
    state.transacciones[label] = { nonce: nextNonce, hash, destino: tx.to || null, contratoEsperado: contract || null,
      cotizacionWei: remaining[0].total.toString() };
    // El hash determinista y nonce se hacen durables antes de llamar a la RPC.
    writeRecord(options.output, state);
    const response = await provider.broadcastTransaction(raw);
    const receipt = confirmed(await response.wait(), contract || registryAddress, label);
    state.transacciones[label].recibo = receipt;
    writeRecord(options.output, state);
    // El receipt Base expone l1Fee; Hardhat no lo hace y allí se conserva la
    // cotización superior. Nunca se trata un valor ausente como cero.
    const rawReceipt = await provider.send("eth_getTransactionReceipt", [hash]);
    const actualL1 = rawReceipt?.l1Fee == null ? remaining[0].l1 : BigInt(rawReceipt.l1Fee);
    const actualOperator = await feeOracle.getOperatorFee(BigInt(receipt.gasUsado));
    if (actualL1 < 0n || typeof actualOperator !== "bigint" || actualOperator < 0n) {
      throw new Error("No se pudieron contabilizar las tarifas del recibo.");
    }
    const observed = BigInt(receipt.gasUsado) * (rawReceipt?.effectiveGasPrice == null
      ? currentFee.maxFeePerGas : BigInt(rawReceipt.effectiveGasPrice)) + actualL1 + actualOperator;
    spentObserved += observed;
    state.transacciones[label].gastoContabilizadoWei = observed.toString();
    writeRecord(options.output, state);
  }
  const [code, registryCode] = await Promise.all([provider.getCode(verifierAddress), provider.getCode(registryAddress)]);
  const build = await hre.artifacts.getBuildInfo("contracts/ClaimRegistry.sol:ClaimRegistry");
  const references = build?.output.contracts["contracts/ClaimRegistry.sol"].ClaimRegistry.evm.deployedBytecode.immutableReferences;
  const expectedVerifier = runtimeIdentity(verifierArtifact);
  const expectedRegistry = runtimeIdentity({ ...registryArtifact, immutableReferences: references }, address, true);
  if (code.toLowerCase() !== expectedVerifier.toLowerCase() || registryCode.toLowerCase() !== expectedRegistry.toLowerCase()) {
    throw new Error("El bytecode desplegado no coincide con el compilado.");
  }
  const registry = new ethers.Contract(registryAddress, registryArtifact.abi, provider);
  if (await registry.oracle() !== address || await registry.verifier() !== verifierAddress ||
      await registry.confidenceThresholdBps() !== BigInt(expected.umbralDeDerivacionBps) ||
      await registry.humanConfirmationRequired() !== false) {
    throw new Error("La configuración on-chain no coincide; verifique los recibos.");
  }
  const artifact = { red: { nombre: expected.nombre, chainId: Number(chain.chainId) }, oraculo: address,
    verificador: verifierAddress, registro: registryAddress, umbralDeDerivacionBps: expected.umbralDeDerivacionBps,
    bytecodeDelVerificador: { bytes: (code.length - 2) / 2, limiteEip170: EIP170_MAX_CODE_SIZE },
    abi: registryArtifact.abi, nonceInicial: nonce,
    recibos: Object.fromEntries(Object.entries(state.transacciones).map(([label, tx]) => [label, tx.recibo])),
    transacciones: state.transacciones,
    codigo: { verificadorHash: ethers.keccak256(code), verificadorEsperadoHash: ethers.keccak256(expectedVerifier),
      registroHash: ethers.keccak256(registryCode), registroEsperadoHash: ethers.keccak256(expectedRegistry),
      registroBytes: (registryCode.length - 2) / 2 } };
  writeRecord(options.output, artifact);
  return artifact;
}

if (require.main === module) {
  run({ output: process.env.TESTNET_OUTPUT, rpcUrl: process.env.L2_RPC_URL,
    privateKey: process.env.L2_PRIVATE_KEY, oracleAddress: process.env.TESTNET_ORACLE_ADDRESS,
    maxWei: process.env.TESTNET_MAX_WEI, send: process.env.TESTNET_SEND }).then((result) => {
    console.log(result.recibos ? "Despliegue confirmado; consulte el artefacto." : `Preflight correcto: ${JSON.stringify(result)}`);
  }).catch((error) => {
    // No imprimir errores de proveedor: pueden contener URL, bytes firmados o credenciales.
    const safe = ["Indique TESTNET_OUTPUT para el artefacto.", "El artefacto de destino ya existe.",
      "Indique L2_RPC_URL y L2_PRIVATE_KEY.", "Indique TESTNET_ORACLE_ADDRESS válido.",
      "Indique TESTNET_MAX_WEI como entero positivo.", "TESTNET_SEND solo admite SI_ENVIO_TRANSACCIONES.",
      "La clave del oráculo no es válida.", "La RPC no coincide con la red configurada.",
      "El firmante no corresponde a la RPC.", "El firmante no corresponde a la clave.",
      "El umbral configurado no es válido.", "El verificador supera EIP-170.",
      "El oráculo tiene transacciones pendientes.", "Las direcciones previstas ya tienen código.",
      "La estimación o los límites de gas de la RPC no son seguros.",
      "La RPC no ofrece cotizaciones L1 y operador utilizables.", "La RPC no ofrece tarifa EIP-1559.",
      "El saldo o el límite de gasto no cubre el gas máximo.",
      "El nonce del oráculo cambió; concilie el artefacto pendiente.",
      "El bytecode desplegado no coincide con el compilado.",
      "El tamaño firmado supera la cotización L1.",
      "No se pudieron contabilizar las tarifas del recibo."];
    console.error("Error: " + (safe.includes(error.message) ? error.message : "Fallo de RPC o transacción; consulte la cadena y el artefacto antes de reintentar."));
    process.exitCode = 1;
  });
}
module.exports = { run };

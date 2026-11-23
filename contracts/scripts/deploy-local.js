// Despliegue local: verificador de EZKL + contrato de reclamos, sobre el nodo
// de Hardhat. Es lo que le da al backend una cadena real contra la cual
// asentar transiciones, en lugar del cliente simulado.
//
// Uso, con `npx hardhat node` corriendo en otra terminal:
//   npx hardhat run scripts/deploy-local.js --network localhost
//
// Deja `despliegue-local.json` en la raiz del repo con las direcciones y el
// ABI que el backend lee. Ese archivo es artefacto de una corrida, no fuente:
// esta en .gitignore y se regenera cada vez que el nodo se reinicia, porque el
// nodo de Hardhat no persiste estado entre arranques y las direcciones que
// quedaran anotadas dejarian de existir.
const fs = require("fs");
const path = require("path");
const hre = require("hardhat");

// EIP-170. El verificador de EZKL entra por debajo, y por eso este despliegue
// no necesita levantar allowUnlimitedContractSize.
const EIP170_MAX_CODE_SIZE = 24576;

// Umbral de derivacion de adr-014 (0,90) en puntos basicos, que es la unidad
// en que el contrato guarda su copia operativa.
const UMBRAL_DE_DERIVACION_BPS = Number(process.env.UMBRAL_DERIVACION_BPS || 9000);

async function main() {
  const [oraculo] = await hre.ethers.getSigners();
  const red = await hre.ethers.provider.getNetwork();

  const verificador = await (
    await hre.ethers.getContractFactory("Halo2Verifier")
  ).deploy();
  await verificador.waitForDeployment();
  const direccionVerificador = await verificador.getAddress();

  const bytecode = await hre.ethers.provider.getCode(direccionVerificador);
  const tamano = (bytecode.length - 2) / 2;
  if (tamano > EIP170_MAX_CODE_SIZE) {
    throw new Error(
      `El verificador desplegado ocupa ${tamano} B y EIP-170 admite ` +
        `${EIP170_MAX_CODE_SIZE} B.`
    );
  }

  const registro = await (
    await hre.ethers.getContractFactory("ClaimRegistry")
  ).deploy(oraculo.address, direccionVerificador);
  await registro.waitForDeployment();
  const direccionRegistro = await registro.getAddress();

  // La copia operativa del umbral vive en el contrato (RF-32). Dejarla en cero
  // haria que todo reclamo superara la comparacion y clasificara Aprobable sin
  // que ningun umbral lo respalde.
  await (await registro.setConfidenceThreshold(UMBRAL_DE_DERIVACION_BPS)).wait();
  await (await registro.setHumanConfirmationMode(false)).wait();

  const artefacto = {
    red: { nombre: hre.network.name, chainId: Number(red.chainId) },
    oraculo: oraculo.address,
    verificador: direccionVerificador,
    registro: direccionRegistro,
    umbralDeDerivacionBps: UMBRAL_DE_DERIVACION_BPS,
    bytecodeDelVerificador: { bytes: tamano, limiteEip170: EIP170_MAX_CODE_SIZE },
    abi: (await hre.artifacts.readArtifact("ClaimRegistry")).abi,
  };

  const destino = path.join(__dirname, "..", "despliegue-local.json");
  fs.writeFileSync(destino, JSON.stringify(artefacto, null, 2) + "\n");

  console.log(`red            : ${hre.network.name} (chainId ${red.chainId})`);
  console.log(`oraculo        : ${oraculo.address}`);
  console.log(`verificador    : ${direccionVerificador}`);
  console.log(`  bytecode     : ${tamano} B | EIP-170 ${EIP170_MAX_CODE_SIZE} B -> CUMPLE`);
  console.log(`registro       : ${direccionRegistro}`);
  console.log(`umbral (bps)   : ${UMBRAL_DE_DERIVACION_BPS}`);
  console.log(`artefacto      : ${destino}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

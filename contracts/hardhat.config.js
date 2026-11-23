// Config de Hardhat del PFI. Solidity 0.8.20: la misma versión con la que
// EZKL genera el verificador, así el toolchain no diverge del artefacto.
//
// Red local: Hardhat Network con los límites del protocolo POR DEFECTO.
// Nunca activar allowUnlimitedContractSize: ese flag fue lo que ocultó el
// incumplimiento de RNF-31 en la arquitectura anterior (28.276 B contra el
// límite EIP-170 de 24.576). El entorno local tiene que decir la verdad.
require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

const config = {
  solidity: {
    // Los contratos del proyecto se compilan con los valores por defecto,
    // para que la suite mida lo que la red real veria.
    compilers: [{ version: "0.8.20" }],
    // El verificador que genera EZKL es la excepcion: se compila con el
    // optimizador en runs=1, que es como lo despliega `ezkl.deploy_evm` y la
    // unica configuracion bajo la que su bytecode entra en EIP-170. Sin el
    // override no deploya, y subir allowUnlimitedContractSize para taparlo es
    // justamente lo que este repo no hace.
    overrides: {
      "contracts/verifier/Halo2Verifier.sol": {
        version: "0.8.20",
        settings: { optimizer: { enabled: true, runs: 1 } },
      },
    },
  },
};

// Red remota (Base Sepolia): se activa únicamente si L2_RPC_URL está definida.
// Las credenciales se suministran por variables de entorno (ver .env.example).
if (process.env.L2_RPC_URL) {
  config.networks = {
    testnet: {
      url: process.env.L2_RPC_URL,
      accounts: process.env.L2_PRIVATE_KEY ? [process.env.L2_PRIVATE_KEY] : [],
    },
  };
}

module.exports = config;

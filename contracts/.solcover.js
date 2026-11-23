// Config de solidity-coverage (RNF-30). Los mocks no son parte del sistema
// y el verificador generado se excluye para evitar Stack too deep del optimizador Yul.
module.exports = {
  skipFiles: ["mocks/", "verifier/"],
  configureYulOptimizer: true,
  solcOptimizerDetails: {
    yul: true,
    yulDetails: {
      stackAllocation: true,
    },
  },
};

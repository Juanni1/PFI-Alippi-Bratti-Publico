// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

import {IProofVerifier} from "../IProofVerifier.sol";

/// Verificador de prueba para la suite. Reemplaza al artefacto de EZKL con
/// cuatro comportamientos: aceptar todo, rechazar (false), revertir (el
/// comportamiento real del verificador generado ante prueba inválida) y
/// aceptar solo pruebas con un prefijo dado, que simula la ligadura de una
/// prueba a su preparación (RNF-33 a nivel contrato; la corrida con dos
/// preparaciones reales de EZKL es de integración).
contract MockVerifier is IProofVerifier {
    enum Mode {
        Accept,
        Reject,
        RevertOnVerify,
        Tagged
    }

    Mode public mode;
    bytes1 public tag;

    constructor(Mode initialMode, bytes1 initialTag) {
        mode = initialMode;
        tag = initialTag;
    }

    function setMode(Mode newMode) external {
        mode = newMode;
    }

    function verifyProof(
        bytes calldata proof,
        uint256[] calldata
    ) external view returns (bool) {
        if (mode == Mode.Accept) return true;
        if (mode == Mode.Reject) return false;
        if (mode == Mode.RevertOnVerify) revert("prueba invalida");
        return proof.length > 0 && proof[0] == tag;
    }
}

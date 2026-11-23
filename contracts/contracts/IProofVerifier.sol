// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

/// Interfaz del verificador de pruebas zk. La implementación real es el
/// contrato Solidity que genera EZKL a partir del circuito Halo2 de la
/// cabeza (adr-009); acá solo se declara la firma que el contrato de
/// reclamos invoca. ADVERTENCIA (adr-001, punto resuelto 1): el verificador
/// generado REVIERTE ante una prueba inválida, no devuelve false. Toda
/// invocación tiene que ir dentro de un bloque protegido (RF-23).
interface IProofVerifier {
    /// @param proof La prueba serializada.
    /// @param instances Las instancias públicas del circuito: el vector de
    ///        576 características como entrada pública y los logits como
    ///        salida pública (adr-009; softmax/argmax quedan off-chain).
    function verifyProof(
        bytes calldata proof,
        uint256[] calldata instances
    ) external returns (bool);
}

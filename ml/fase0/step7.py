"""Paso 7 de Fase 0 — EVM verifier (Solidity) + estimación de gas.

Genera el contrato Solidity del verifier a partir de la VK. El gas de verificación
on-chain es en buena medida independiente del logrows, así que este paso se corre
una sola vez (no en cada resolución del barrido).

`ezkl.create_evm_verifier` es async → se envuelve en `asyncio.run()`. Se imprime
`help()` al arrancar para facilitar debugging si cambian los argumentos.

Ejecutar:  uv run python -m fase0.step7 --resolution 32
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os

# HOME-fix Windows.
if not os.environ.get("HOME"):
    os.environ["HOME"] = os.environ.get("USERPROFILE", os.path.expanduser("~"))

import ezkl

from fase0.circuit import circuit_paths, read_metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Paso 7 Fase 0: EVM verifier Solidity")
    parser.add_argument("--resolution", type=int, default=32)
    parser.add_argument("--out-dir", type=str, default="artifacts")
    parser.add_argument("--num-classes", type=int, default=7)
    parser.add_argument("--width-mult", type=float, default=0.5)
    args = parser.parse_args()

    tag = f"r{args.resolution}"
    settings_path, compiled_path = circuit_paths(args.out_dir, args.resolution)
    metrics = read_metrics(settings_path, compiled_path, args.resolution)
    logrows = metrics.logrows

    srs_path = os.path.join(args.out_dir, f"kzg_{logrows}.srs")
    vk_path = os.path.join(args.out_dir, f"vk_{tag}.key")
    sol_path = os.path.join(args.out_dir, f"verifier_{tag}.sol")
    abi_path = os.path.join(args.out_dir, f"verifier_{tag}.abi")

    for path, label in [(vk_path, "vk"), (srs_path, "srs")]:
        if not os.path.exists(path):
            raise FileNotFoundError(f"{label} no encontrado: {path} — correr Paso 4 primero")

    # Imprimir firma para debugging.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        help(ezkl.create_evm_verifier)
    print("CREATE_EVM_VERIFIER_HELP:", buf.getvalue().split("\n")[2])

    # Firma confirmada (help): create_evm_verifier(vk, settings, sol, abi, srs_path=None, reusable=...).
    # `create_evm_verifier` necesita el event loop YA corriendo al invocarla (pyo3-asyncio):
    # hay que llamarla DENTRO de una corutina, no evaluarla como argumento de asyncio.run
    # (eso la llama fuera del loop -> "no running event loop").
    print("Generando EVM verifier (asyncio.run)...")

    async def _gen():
        return await ezkl.create_evm_verifier(
            vk_path,
            settings_path,
            sol_path,
            abi_path,
            srs_path=srs_path,
        )

    asyncio.run(_gen())

    sol_exists = os.path.exists(sol_path)
    abi_exists = os.path.exists(abi_path)

    print("\n" + "=" * 60)
    print(f"PASO 7 — resolucion {args.resolution}x{args.resolution} | logrows {logrows}")
    print("=" * 60)
    if sol_exists:
        sol_size = os.path.getsize(sol_path)
        print(f"  verifier.sol     : {sol_path}  ({sol_size / 1024:.1f} KB)")
    else:
        print("  verifier.sol     : NO generado")
    if abi_exists:
        print(f"  verifier.abi     : {abi_path}")
    else:
        print("  verifier.abi     : NO generado (puede ser opcional segun version ezkl)")

    if not sol_exists:
        raise RuntimeError("create_evm_verifier no generó el .sol — ver help arriba")

    # Calldata: los bytes exactos que se mandarían on-chain para llamar verify(). Se
    # genera desde la prueba (no requiere chain). Firma: encode_evm_calldata(proof, calldata).
    proof_path = os.path.join(args.out_dir, f"proof_{tag}.json")
    calldata_path = os.path.join(args.out_dir, f"calldata_{tag}.bytes")
    if os.path.exists(proof_path):
        ezkl.encode_evm_calldata(proof_path, calldata_path)
        cd_size = os.path.getsize(calldata_path)
        print(f"  calldata         : {calldata_path}  ({cd_size / 1024:.1f} KB)")
    else:
        print(f"  calldata         : NO generado (falta {proof_path} — correr Paso 5)")

    print()
    print("  Para verificar on-chain de verdad: instalar Foundry (anvil + solc), luego")
    print("  ezkl.deploy_evm(...) a un nodo anvil local y ezkl.verify_evm(addr, rpc, ...).")
    print(f"  -> EVM verifier + calldata generados para resolucion {args.resolution}")


if __name__ == "__main__":
    main()

"""Verificación ON-CHAIN de la prueba zk — cierre del objetivo del proyecto.

Toma el verifier Solidity (generado por `fase0.step7`) y la prueba del modelo
entrenado, los lleva a una EVM local (anvil), **deploya el contrato**, **verifica
la prueba on-chain** y **mide el gas** de esa verificación. Es la demostración de que
el loop completo cierra: modelo entrenado → prueba zk → verificación en blockchain.

Requisitos:
- **Foundry** instalado (binario `anvil`). En esta máquina: `G:\foundry\anvil.exe`.
- Un nodo anvil corriendo:  `anvil --code-size-limit 200000 --silent`
  (el límite alto es necesario: el verifier monolítico de halo2 pesa ~28 KB de
  bytecode y supera el límite EIP-170 de 24.577 B — en mainnet habría que usar la
  variante "reusable" con la VKA separada; para el demo local anvil lo permite).
- Artefactos de Fase 0/1 en `artifacts/`: `verifier_r{res}.sol`, `proof_r{res}.json`.

`deploy_evm` y `verify_evm` de ezkl son **async** → se llaman dentro de una corutina
(si se evalúan fuera del loop dan "no running event loop"). La private key va **sin
prefijo 0x**. ezkl baja `solc` solo (vía svm) en el primer deploy.

Además del gas, mide el **tamaño del bytecode deployado** (`eth_getCode`) y lo contrasta
contra el límite EIP-170. Esa cifra no la produce ningún otro paso: `step7` reporta el
tamaño del `.sol` (código fuente), no el del bytecode.

Ejecutar (con anvil corriendo):
  uv run python -m fase1.verify_onchain --resolution 64

La cabeza de features-afuera usa `resolution = feat_dim` en sus artefactos (así los
nombra `prove_head.py`), de modo que el mismo comando sirve sin cambios:
  uv run python -m fase0.step7 --resolution 576 --out-dir artifacts_head
  uv run python -m fase1.verify_onchain --resolution 576 --out-dir artifacts_head
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import urllib.request

# HOME-fix Windows antes de importar ezkl.
if not os.environ.get("HOME"):
    os.environ["HOME"] = os.environ.get("USERPROFILE", os.path.expanduser("~"))

import ezkl

# Cuenta #0 de anvil (clave de test pública y conocida — NO usar fuera de un nodo local).
ANVIL_KEY = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

# EIP-170 fija MAX_CODE_SIZE = 0x6000: un contrato de más de 24.576 bytes de bytecode no
# se puede deployar. 24.577 es el primer tamaño rechazado, no el límite.
EIP170_MAX_CODE_SIZE = 24_576


def _rpc(url: str, method: str, params: list) -> dict:
    """Llamada JSON-RPC mínima (sin web3) para estimar gas."""
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, payload, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def deployed_code_size(url: str, addr: str) -> int:
    """Bytes de bytecode que quedaron en la cuenta del verifier, vía `eth_getCode`."""
    resp = _rpc(url, "eth_getCode", [addr, "latest"])
    return len(bytes.fromhex(resp["result"].removeprefix("0x")))


def main() -> None:
    p = argparse.ArgumentParser(description="Verifica la prueba zk on-chain en anvil y mide el gas.")
    p.add_argument("--resolution", type=int, default=64)
    p.add_argument("--out-dir", default="artifacts")
    p.add_argument("--rpc-url", default="http://127.0.0.1:8545")
    p.add_argument("--private-key", default=ANVIL_KEY, help="Sin prefijo 0x (default: cuenta #0 de anvil).")
    args = p.parse_args()

    tag = f"r{args.resolution}"
    sol_path = os.path.join(args.out_dir, f"verifier_{tag}.sol")
    proof_path = os.path.join(args.out_dir, f"proof_{tag}.json")
    addr_path = os.path.join(args.out_dir, f"verifier_addr_{tag}.txt")
    calldata_path = os.path.join(args.out_dir, f"calldata_{tag}.bytes")

    for path, label in [(sol_path, "verifier.sol"), (proof_path, "proof")]:
        if not os.path.exists(path):
            raise FileNotFoundError(f"{label} no encontrado: {path} — correr step7 / prove primero")

    # --- 1. Deploy del verifier al nodo local ------------------------------------
    print(f"Deployando verifier a {args.rpc_url} ...")

    async def _deploy():
        return await ezkl.deploy_evm(
            addr_path, args.rpc_url, sol_code_path=sol_path,
            contract_type="verifier", optimizer_runs=1, private_key=args.private_key,
        )

    asyncio.run(_deploy())
    addr = open(addr_path).read().strip()
    print(f"  verifier deployado en: {addr}")

    # --- 2. Verificación on-chain ------------------------------------------------
    async def _verify():
        return await ezkl.verify_evm(addr, args.rpc_url, proof_path=proof_path)

    ok = asyncio.run(_verify())

    # --- 3. Gas de la verificación -----------------------------------------------
    if not os.path.exists(calldata_path):
        ezkl.encode_evm_calldata(proof_path, calldata_path)
    with open(calldata_path, "rb") as f:
        data_hex = "0x" + f.read().hex()
    gas = None
    try:
        resp = _rpc(args.rpc_url, "eth_estimateGas", [{"to": addr, "data": data_hex}])
        gas = int(resp["result"], 16)
    except Exception as e:  # noqa: BLE001
        print(f"  (no se pudo estimar el gas: {e})")

    # --- 4. Tamaño del bytecode deployado contra EIP-170 --------------------------
    code_size = None
    try:
        code_size = deployed_code_size(args.rpc_url, addr)
    except Exception as e:  # noqa: BLE001
        print(f"  (no se pudo leer el bytecode: {e})")

    # --- Reporte ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"VERIFICACIÓN ON-CHAIN — resolución {args.resolution} | logrows {read_logrows(args.out_dir, args.resolution)}")
    print("=" * 60)
    print(f"  verify_evm devolvió : {ok}  {'OK' if ok else 'FALLÓ'}")
    if gas is not None:
        print(f"  gas de verificación : {gas:,}")
    print(f"  prueba              : {os.path.getsize(proof_path):,} B ({proof_path})")
    print(f"  calldata            : {os.path.getsize(calldata_path):,} B")
    if code_size is not None:
        margen = EIP170_MAX_CODE_SIZE - code_size
        veredicto = f"CUMPLE (sobran {margen:,} B)" if margen >= 0 else f"NO CUMPLE (excede por {-margen:,} B)"
        print(f"  bytecode deployado  : {code_size:,} B | límite EIP-170 {EIP170_MAX_CODE_SIZE:,} B -> {veredicto}")
        print("    (anvil corre con --code-size-limit levantado: deploya igual, la red real no)")
    print(f"  contrato            : {addr}")
    if not ok:
        raise SystemExit(1)
    print("\n  -> loop completo: modelo entrenado -> prueba zk -> verificación on-chain OK")


def read_logrows(out_dir: str, resolution: int) -> int | str:
    from fase0.circuit import circuit_paths, read_metrics
    try:
        s, c = circuit_paths(out_dir, resolution)
        return read_metrics(s, c, resolution).logrows
    except Exception:  # noqa: BLE001
        return "?"


if __name__ == "__main__":
    main()

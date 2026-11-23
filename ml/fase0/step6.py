"""Paso 6 de Fase 0 — Verificación local de la prueba.

Confirma que el pipeline cierra de punta a punta: la prueba generada en el Paso 5
es válida según la VK y el SRS del Paso 4.

Se imprime `help(ezkl.verify)` al arrancar para facilitar debugging si cambia la
firma en esta versión de EZKL.

Ejecutar:  uv run python -m fase0.step6 --resolution 32
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os

# HOME-fix Windows (mismo que en todos los scripts que llaman a ezkl directamente).
if not os.environ.get("HOME"):
    os.environ["HOME"] = os.environ.get("USERPROFILE", os.path.expanduser("~"))

import ezkl

from fase0.circuit import circuit_paths, read_metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Paso 6 Fase 0: verificacion local de la prueba")
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
    proof_path = os.path.join(args.out_dir, f"proof_{tag}.json")

    for path, label in [
        (proof_path, "proof"), (vk_path, "vk"), (srs_path, "srs"), (settings_path, "settings")
    ]:
        if not os.path.exists(path):
            raise FileNotFoundError(f"{label} no encontrado: {path} — correr pasos anteriores primero")

    # Imprimir firma para facilitar debugging.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        help(ezkl.verify)
    print("VERIFY_HELP:", buf.getvalue().split("\n")[2])  # primera línea de la firma

    # Firma esperada: ezkl.verify(proof_path, settings_path, vk_path, srs_path)
    # Si falla con TypeError, ver VERIFY_HELP arriba para ajustar argumentos.
    result = ezkl.verify(proof_path, settings_path, vk_path, srs_path)

    print("\n" + "=" * 60)
    print(f"PASO 6 — resolucion {args.resolution}x{args.resolution} | logrows {logrows}")
    print("=" * 60)
    if result:
        print("  OK — verificacion local exitosa")
        print(f"  -> pipeline cierra de punta a punta a resolucion {args.resolution}")
    else:
        print("  FALLO — ezkl.verify devolvio False")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

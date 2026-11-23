"""Paso 5 de Fase 0 — Witness + prueba (mide tiempo, RAM pico, tamaño de prueba).

Requiere que los artifacts de los Pasos 3 y 4 existan en `--out-dir`.

Ejecutar:  uv run python -m fase0.step5 --resolution 32 --ram-limit-gb 19
"""

from __future__ import annotations

import argparse

from fase0.proving import _fmt_gb, run_prove

_GB = 1024 ** 3


def main() -> None:
    parser = argparse.ArgumentParser(description="Paso 5 Fase 0: witness + prove, mide RAM y tiempo")
    parser.add_argument("--resolution", type=int, default=32)
    parser.add_argument("--out-dir", type=str, default="artifacts")
    parser.add_argument("--ram-limit-gb", type=float, default=19.0)
    parser.add_argument("--num-classes", type=int, default=7)
    parser.add_argument("--width-mult", type=float, default=0.5)
    args = parser.parse_args()

    m = run_prove(
        resolution=args.resolution,
        out_dir=args.out_dir,
        ram_limit_gb=args.ram_limit_gb,
        num_classes=args.num_classes,
        width_mult=args.width_mult,
    )

    print("\n" + "=" * 60)
    print(f"PASO 5 — resolucion {args.resolution}x{args.resolution} | logrows {m.logrows}")
    print("=" * 60)

    if m.aborted:
        print(f"  ABORTADO: supero el presupuesto de {args.ram_limit_gb:.0f} GB de RAM")
        print(f"  RAM pico (muestreada) antes de abortar: {_fmt_gb(m.peak_bytes)}")
        print(f"  -> resolucion {args.resolution} NO viable para proving con {args.ram_limit_gb:.0f} GB")
        return

    print(f"  RAM pico (prove)   : {_fmt_gb(m.peak_bytes)}   [presupuesto {args.ram_limit_gb:.0f} GB]")
    print(f"  tiempo witness     : {m.witness_time_s:.1f}s")
    print(f"  tiempo prove       : {m.prove_time_s:.1f}s")
    print(f"  tamano prueba      : {m.proof_bytes / 1024:.1f} KB")
    print(f"  prueba en          : {m.proof_path}")
    headroom = args.ram_limit_gb * _GB - (m.peak_bytes or 0)
    verdict = "VIABLE" if headroom > 0 else "EXCEDE presupuesto"
    print(f"  -> {verdict} (margen {_fmt_gb(headroom)} sobre {args.ram_limit_gb:.0f} GB)")


if __name__ == "__main__":
    main()

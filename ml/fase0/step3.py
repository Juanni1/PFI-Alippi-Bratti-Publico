"""Paso 3 de Fase 0 — gen_settings + calibrate + compile (lee `logrows`).

Encadena: build modelo → export ONNX (Paso 2) → datos de calibración en rango real
→ gen_settings/calibrate/compile → lee el `logrows` del settings.json.

Ejecutar:  uv run python -m fase0.step3 --resolution 96
           uv run python -m fase0.step3 --resolution 96 --scales 7 --num-samples 2

Nota de tiempo: la calibración corre forward passes del modelo por cada escala y
muestra. Acotar `--scales` a un único valor y `--num-samples` reduce el tiempo sin
mover el `logrows` (que es estructural). Sin `--scales`, EZKL busca escalas (lento).
"""

from __future__ import annotations

import argparse
import time

from fase0.circuit import prepare_circuit


def run(
    resolution: int = 96,
    num_classes: int = 7,
    width_mult: float = 0.5,
    out_dir: str = "artifacts",
    target: str = "resources",
    num_samples: int = 4,
    scales: list[int] | None = None,
    num_inner_cols: int = 2,
    arch: str = "mobilenetv2",
    smallcnn_kwargs: dict | None = None,
):
    # --- gen_settings + calibrate + compile (Pasos 1-3) ---------------------------
    print(f"Compilando circuito '{arch}' a {resolution}x{resolution} (target={target}, "
          f"scales={scales}, num_samples={num_samples}, num_inner_cols={num_inner_cols})...")
    t0 = time.time()
    m = prepare_circuit(
        resolution,
        out_dir,
        num_classes=num_classes,
        width_mult=width_mult,
        target=target,
        num_samples=num_samples,
        scales=scales,
        num_inner_cols=num_inner_cols,
        arch=arch,
        smallcnn_kwargs=smallcnn_kwargs,
        reuse=False,  # el Paso 3 siempre recalibra
    )
    elapsed = time.time() - t0

    # --- Reporte ------------------------------------------------------------------
    print(f"\nResolucion {resolution}x{resolution}  ->  LOGROWS = {m.logrows}   2^{m.logrows} = {2 ** m.logrows:,} filas")
    print(f"  input_scale / param_scale : {m.input_scale} / {m.param_scale}")
    print(f"  num_inner_cols            : {m.num_inner_cols}")
    print(f"  num_rows                  : {m.num_rows:,}")
    print(f"  total_assignments         : {m.total_assignments:,}  (domina el logrows; estructura)")
    print(f"  lookup_range              : {m.lookup_range}")
    print(f"  settings                  : {m.settings_path}")
    print(f"  tiempo gen+calibrate+compile: {elapsed:.1f}s")
    return m


def main() -> None:
    parser = argparse.ArgumentParser(description="Paso 3 Fase 0: settings + calibrate + compile")
    parser.add_argument("--resolution", type=int, default=96)
    parser.add_argument("--num-classes", type=int, default=7)
    parser.add_argument("--width-mult", type=float, default=0.5)
    parser.add_argument("--out-dir", type=str, default="artifacts")
    parser.add_argument("--target", type=str, default="resources", choices=["resources", "accuracy"])
    parser.add_argument("--num-samples", type=int, default=4)
    parser.add_argument("--num-inner-cols", type=int, default=2)
    parser.add_argument(
        "--scales", type=int, nargs="*", default=None,
        help="escalas a probar en calibracion (p. ej. --scales 7). Vacio = busqueda completa (lento)",
    )
    parser.add_argument("--arch", type=str, default="mobilenetv2",
                        choices=["mobilenetv2", "smallcnn"])
    parser.add_argument("--base-channels", type=int, default=8,
                        help="solo smallcnn: canales del stem")
    parser.add_argument("--num-blocks", type=int, default=4,
                        help="solo smallcnn: nº de bloques depthwise-separable")
    parser.add_argument("--num-downsample", type=int, default=3,
                        help="solo smallcnn: cuántos bloques bajan resolución (stride 2)")
    args = parser.parse_args()
    smallcnn_kwargs = None
    if args.arch == "smallcnn":
        smallcnn_kwargs = {
            "base_channels": args.base_channels,
            "num_blocks": args.num_blocks,
            "num_downsample": args.num_downsample,
        }
    run(
        resolution=args.resolution,
        num_classes=args.num_classes,
        width_mult=args.width_mult,
        out_dir=args.out_dir,
        target=args.target,
        num_samples=args.num_samples,
        scales=args.scales,
        num_inner_cols=args.num_inner_cols,
        arch=args.arch,
        smallcnn_kwargs=smallcnn_kwargs,
    )


if __name__ == "__main__":
    main()

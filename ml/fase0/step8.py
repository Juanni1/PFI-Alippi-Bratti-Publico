"""Paso 8 de Fase 0 — Barrido de resoluciones (el experimento central).

Para cada resolución (de menor a mayor) corre el pipeline completo:
  Paso 2 (ONNX) → Paso 3 (circuit) → Paso 4 (SRS+setup) → Paso 5 (prove) → Paso 6 (verify)

Detiene el barrido en cuanto un paso aborta por RAM (OOM), acumula métricas y
genera la tabla de viabilidad en `artifacts/sweep_results.md`.

Ejecutar:
  uv run python -m fase0.step8
  uv run python -m fase0.step8 --resolutions 32 64 --ram-limit-gb 19
  uv run python -m fase0.step8 --skip-to 64   # reanudar desde res 64
"""

from __future__ import annotations

import argparse
import os

# HOME-fix para los pasos que llaman a ezkl directamente en este proceso.
if not os.environ.get("HOME"):
    os.environ["HOME"] = os.environ.get("USERPROFILE", os.path.expanduser("~"))

import ezkl

from fase0.circuit import circuit_paths, prepare_circuit, read_metrics
from fase0.export import export_onnx
from fase0.model import MobileNetV2
from fase0.proving import _fmt_gb, run_prove
from fase0 import step4

import torch

_GB = 1024 ** 3
_DEFAULT_RESOLUTIONS = [32, 64, 96, 128, 160, 192, 224]


def _run_verify(out_dir: str, resolution: int, logrows: int) -> bool:
    """Verificación local inline (sin subproceso; la prueba ya existe en disco)."""
    tag = f"r{resolution}"
    settings_path, _ = circuit_paths(out_dir, resolution)
    srs_path = os.path.join(out_dir, f"kzg_{logrows}.srs")
    vk_path = os.path.join(out_dir, f"vk_{tag}.key")
    proof_path = os.path.join(out_dir, f"proof_{tag}.json")
    try:
        return bool(ezkl.verify(proof_path, settings_path, vk_path, srs_path))
    except Exception as e:
        print(f"    ezkl.verify error: {e}")
        return False


def _md_table(rows: list[dict]) -> str:
    headers = ["Res", "logrows", "RAM setup", "RAM prove", "T. prove", "Proof size", "Verif OK", "Notas"]
    sep = "|" + "|".join("---" for _ in headers) + "|"
    lines = ["| " + " | ".join(headers) + " |", sep]
    for r in rows:
        lines.append(
            f"| {r['resolution']} "
            f"| {r.get('logrows', '?')} "
            f"| {r.get('ram_setup', 'n/a')} "
            f"| {r.get('ram_prove', 'n/a')} "
            f"| {r.get('prove_time', 'n/a')} "
            f"| {r.get('proof_size', 'n/a')} "
            f"| {r.get('verify_ok', 'n/a')} "
            f"| {r.get('notes', '')} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Paso 8 Fase 0: barrido de resoluciones")
    parser.add_argument("--resolutions", type=int, nargs="+", default=_DEFAULT_RESOLUTIONS)
    parser.add_argument("--out-dir", type=str, default="artifacts")
    parser.add_argument("--ram-limit-gb", type=float, default=19.0)
    parser.add_argument("--num-classes", type=int, default=7)
    parser.add_argument("--width-mult", type=float, default=0.5)
    parser.add_argument("--num-samples", type=int, default=2,
                        help="muestras de calibracion por escala (mantener bajo, ~2)")
    parser.add_argument("--scales", type=int, nargs="*", default=[7],
                        help="escalas de calibracion (default [7] — acelera ~3x sin mover logrows)")
    parser.add_argument("--param-visibility", type=str, default="fixed",
                        choices=["fixed", "hashed", "public", "private"],
                        help="visibilidad de los pesos. 'fixed' = columnas fijas (PK gigante); "
                             "'hashed'/'public' los saca de las columnas (PK mas chica)")
    parser.add_argument("--skip-to", type=int, default=None,
                        help="saltar resoluciones por debajo de este valor (para reanudar)")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    resolutions = sorted(args.resolutions)
    if args.skip_to:
        resolutions = [r for r in resolutions if r >= args.skip_to]

    table_rows: list[dict] = []
    stopped_at: int | None = None

    for res in resolutions:
        print(f"\n{'=' * 60}")
        print(f"BARRIDO: resolucion {res}x{res}")
        print("=" * 60)
        row: dict = {"resolution": res}

        # --- Paso 2+3: ONNX + circuit (reutiliza si existen) ----------------------
        print(f"  [2/3] ONNX + circuit...")
        try:
            metrics = prepare_circuit(
                res, args.out_dir,
                num_classes=args.num_classes,
                width_mult=args.width_mult,
                num_samples=args.num_samples,
                scales=args.scales,
                param_visibility=args.param_visibility,
                reuse=True,
            )
            row["logrows"] = metrics.logrows
            print(f"    logrows = {metrics.logrows}")
        except Exception as e:
            print(f"    ERROR en circuit: {e}")
            row["notes"] = f"circuit error: {e}"
            table_rows.append(row)
            stopped_at = res
            break

        # --- Paso 4: SRS + setup --------------------------------------------------
        print(f"  [4] SRS + setup...")
        try:
            r4 = step4.run(
                resolution=res,
                out_dir=args.out_dir,
                ram_limit_gb=args.ram_limit_gb,
                num_inner_cols=2,
                scales=args.scales,
                num_samples=args.num_samples,
                param_visibility=args.param_visibility,
                force=False,
            )
            if r4.get("aborted"):
                reason = r4.get("reason", "ram_limit")
                row["ram_setup"] = _fmt_gb(r4.get("peak_rss_bytes"))
                row["notes"] = f"OOM setup ({reason})"
                table_rows.append(row)
                stopped_at = res
                break
            row["ram_setup"] = _fmt_gb(r4.get("peak_bytes"))
        except Exception as e:
            print(f"    ERROR en setup: {e}")
            row["notes"] = f"setup error: {e}"
            table_rows.append(row)
            stopped_at = res
            break

        # --- Paso 5: witness + prove -----------------------------------------------
        print(f"  [5] witness + prove...")
        try:
            m5 = run_prove(
                resolution=res,
                out_dir=args.out_dir,
                ram_limit_gb=args.ram_limit_gb,
                num_classes=args.num_classes,
                width_mult=args.width_mult,
            )
            if m5.aborted:
                row["ram_prove"] = _fmt_gb(m5.peak_bytes)
                row["notes"] = "OOM prove"
                table_rows.append(row)
                stopped_at = res
                break
            row["ram_prove"] = _fmt_gb(m5.peak_bytes)
            row["prove_time"] = f"{m5.prove_time_s:.0f}s"
            row["proof_size"] = f"{m5.proof_bytes / 1024:.0f} KB"
        except Exception as e:
            print(f"    ERROR en prove: {e}")
            row["notes"] = f"prove error: {e}"
            table_rows.append(row)
            stopped_at = res
            break

        # --- Paso 6: verificación --------------------------------------------------
        print(f"  [6] verify...")
        ok = _run_verify(args.out_dir, res, metrics.logrows)
        row["verify_ok"] = "SI" if ok else "NO"
        if not ok:
            row["notes"] = "verify fallo"

        table_rows.append(row)
        print(f"    -> resolucion {res} COMPLETADA (verify={'OK' if ok else 'FALLO'})")

    # --- Tabla final -----------------------------------------------------------
    print("\n\n" + "=" * 60)
    print("TABLA DE VIABILIDAD — Fase 0")
    print("=" * 60)
    table_md = _md_table(table_rows)
    print(table_md)

    if stopped_at:
        print(f"\n-> Barrido detenido en resolucion {stopped_at} (OOM o error).")
        if stopped_at > resolutions[0]:
            viable = [r["resolution"] for r in table_rows if not r.get("notes", "").startswith("OOM")]
            if viable:
                print(f"-> Resolucion maxima viable: {max(viable)}")
    else:
        viable = [r["resolution"] for r in table_rows if r.get("verify_ok") == "SI"]
        if viable:
            print(f"\n-> Resolucion maxima viable: {max(viable)}")

    out_file = os.path.join(args.out_dir, "sweep_results.md")
    with open(out_file, "w") as f:
        f.write("# Tabla de viabilidad Fase 0\n\n")
        f.write(table_md)
        f.write("\n")
    print(f"\nTabla guardada en: {out_file}")


if __name__ == "__main__":
    main()

"""Barrido accuracy-vs-presupuesto de circuito — Fase 1.

Entrena varias configuraciones de la SmallCNN con la misma **receta de accuracy
global** y, para cada una, mide la **test accuracy** y el **logrows** (costo de
circuito). Emite una tabla markdown con la curva accuracy-vs-presupuesto — el
resultado central que pidió la tesis.

Todas las corridas se entrenan/evalúan en la PC (GPU); solo la *prueba zk* de las
configs con logrows ≥ 22 necesitaría una VM (el entrenamiento no).

Receta global-accuracy (fija para todas): sin pesos de clase, selección por
accuracy, label smoothing 0.1. (Sin pesos inversos, que castigan la clase
mayoritaria y bajan la accuracy global.)

Ejecutar:
  uv run python -m fase1.sweep_accuracy                       # configs default: bc8 y bc16 @ res64
  uv run python -m fase1.sweep_accuracy --configs 8:64 16:64 8:96 16:96 --epochs 60
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

# HOME-fix Windows (los subprocesos de step3 importan ezkl).
if not os.environ.get("HOME"):
    os.environ["HOME"] = os.environ.get("USERPROFILE", os.path.expanduser("~"))

RECIPE = ["--class-weights", "none", "--select-metric", "accuracy", "--label-smoothing", "0.1"]


def run_train(bc: int, res: int, epochs: int, workers: int, log_dir: str) -> tuple[float | None, float | None, str]:
    """Entrena una config y devuelve (test_acc, test_macroF1, ruta_log)."""
    tag = "acc"  # sufijo de receta para no pisar el baseline (pesos inversos)
    log_path = os.path.join(log_dir, f"sweep_r{res}_bc{bc}_{tag}.log")
    cmd = [
        sys.executable, "-u", "-m", "fase1.train",
        "--base-channels", str(bc), "--resolution", str(res),
        "--epochs", str(epochs), "--num-workers", str(workers),
        "--tag", tag, "--eval-test", *RECIPE,
    ]
    with open(log_path, "w", encoding="utf-8") as f:
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, text=True)
    text = open(log_path, encoding="utf-8").read()
    m = re.search(r"TEST \| acc ([\d.]+) \| macroF1 ([\d.]+)", text)
    if proc.returncode != 0 or not m:
        print(f"  [!] train bc{bc} r{res} no produjo TEST (rc={proc.returncode}); ver {log_path}")
        return None, None, log_path
    return float(m.group(1)), float(m.group(2)), log_path


def measure_logrows(bc: int, res: int) -> int | None:
    """Mide el logrows de la config con la MISMA instanciación que el entrenamiento (width_mult 1.0)."""
    cmd = [
        sys.executable, "-m", "fase0.step3", "--arch", "smallcnn",
        "--base-channels", str(bc), "--width-mult", "1.0", "--resolution", str(res),
        "--num-classes", "6", "--scales", "7", "--num-samples", "2",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"LOGROWS = (\d+)", out.stdout + out.stderr)
    return int(m.group(1)) if m else None


def parse_config(s: str) -> tuple[int, int]:
    bc, res = s.split(":")
    return int(bc), int(res)


def main() -> None:
    p = argparse.ArgumentParser(description="Barrido accuracy-vs-logrows de la SmallCNN.")
    p.add_argument("--configs", nargs="+", default=["8:64", "16:64"],
                   help="Configs base_channels:resolution (ej. 8:64 16:64 8:96).")
    p.add_argument("--epochs", type=int, default=60)
    p.add_argument("--num-workers", type=int, default=8)
    p.add_argument("--log-dir", default="fase1")
    p.add_argument("--out", default="fase1/accuracy_sweep.md")
    args = p.parse_args()

    configs = [parse_config(c) for c in args.configs]
    rows = []
    for bc, res in configs:
        print(f"\n=== config bc{bc} @ res{res} (epochs {args.epochs}) ===")
        t0 = time.time()
        logrows = measure_logrows(bc, res)
        acc, macro_f1, log_path = run_train(bc, res, args.epochs, args.num_workers, args.log_dir)
        dt = time.time() - t0
        budget = "PC (~30 GB)" if (logrows or 99) <= 21 else f"VM (logrows {logrows})"
        rows.append({"bc": bc, "res": res, "logrows": logrows, "acc": acc, "macro_f1": macro_f1, "budget": budget})
        print(f"  -> logrows {logrows} | test acc {acc} | macroF1 {macro_f1} | {dt/60:.1f} min")

    # Tabla markdown.
    lines = [
        "# Barrido accuracy-vs-presupuesto (SmallCNN, receta global-accuracy)",
        "",
        f"Receta: `--class-weights none --select-metric accuracy --label-smoothing 0.1 --epochs {args.epochs}`.",
        "Entrenamiento/eval en PC (GPU); la *prueba zk* de logrows ≥ 22 necesita VM.",
        "",
        "| base_channels | resolución | logrows | test accuracy | test macro-F1 | presupuesto de prueba |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        acc = f"{r['acc']:.3f}" if r["acc"] is not None else "—"
        mf1 = f"{r['macro_f1']:.3f}" if r["macro_f1"] is not None else "—"
        lines.append(f"| {r['bc']} | {r['res']} | {r['logrows']} | {acc} | {mf1} | {r['budget']} |")
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nTabla escrita en {args.out}")
    print("\n".join(lines[5:]))


if __name__ == "__main__":
    main()

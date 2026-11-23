"""gen_witness + prove — Paso 5 de Fase 0.

Lanza `_prove_worker.py` en un subproceso monitoreado (igual que step4 con setup),
para poder medir RAM pico y abortar si supera el presupuesto sin colgar la sesión.

Requiere que los artifacts del Paso 4 existan:
  - model_{tag}.compiled
  - pk_r{res}.key
  - kzg_{logrows}.srs
  - settings_{tag}.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass

import psutil

from fase0.calibration import make_calibration_batch, write_calibration_json
from fase0.circuit import circuit_paths, read_metrics

_GB = 1024 ** 3


@dataclass
class ProveMetrics:
    resolution: int
    logrows: int
    peak_bytes: int | None
    witness_time_s: float
    prove_time_s: float
    proof_bytes: int
    proof_path: str
    witness_path: str
    aborted: bool = False
    abort_reason: str = ""


def _fmt_gb(n: float | None) -> str:
    if n is None:
        return "n/a"
    return f"{n / _GB:.2f} GB"


def _monitor(proc: subprocess.Popen, limit_bytes: float, poll_s: float = 0.1):
    """Muestrea RSS del hijo; aborta si supera el límite. Devuelve (peak, aborted)."""
    p = psutil.Process(proc.pid)
    peak = 0
    aborted = False
    while proc.poll() is None:
        try:
            rss = p.memory_info().rss
            for child in p.children(recursive=True):
                try:
                    rss += child.memory_info().rss
                except psutil.Error:
                    pass
        except psutil.Error:
            break
        peak = max(peak, rss)
        if limit_bytes and rss > limit_bytes:
            proc.kill()
            aborted = True
            break
        time.sleep(poll_s)
    return peak, aborted


def run_prove(
    resolution: int,
    out_dir: str = "artifacts",
    *,
    ram_limit_gb: float = 19.0,
    num_classes: int = 7,
    width_mult: float = 0.5,
    seed: int = 0,
) -> ProveMetrics:
    """Corre gen_witness + prove en subproceso monitoreado; devuelve ProveMetrics."""
    tag = f"r{resolution}"
    settings_path, compiled_path = circuit_paths(out_dir, resolution)

    # Leer logrows del settings para construir la ruta del SRS.
    if not os.path.exists(settings_path):
        raise FileNotFoundError(f"settings no encontrado: {settings_path} — correr Paso 3 primero")
    metrics = read_metrics(settings_path, compiled_path, resolution)
    logrows = metrics.logrows

    srs_path = os.path.join(out_dir, f"kzg_{logrows}.srs")
    pk_path = os.path.join(out_dir, f"pk_{tag}.key")
    witness_input_path = os.path.join(out_dir, f"witness_input_{tag}.json")
    witness_out_path = os.path.join(out_dir, f"witness_{tag}.json")
    proof_path = os.path.join(out_dir, f"proof_{tag}.json")

    for path, label in [(compiled_path, "compiled"), (pk_path, "pk"), (srs_path, "srs")]:
        if not os.path.exists(path):
            raise FileNotFoundError(f"{label} no encontrado: {path} — correr Paso 4 primero")

    # Input de prueba: una muestra en rango real (igual que calibración).
    batch = make_calibration_batch(resolution, num_samples=1, seed=seed)
    write_calibration_json(batch, witness_input_path)

    cmd = [
        sys.executable, "-m", "fase0._prove_worker",
        "--compiled", compiled_path,
        "--witness-input", witness_input_path,
        "--witness-out", witness_out_path,
        "--pk", pk_path,
        "--proof-out", proof_path,
        "--srs", srs_path,
        "--settings", settings_path,
    ]
    print("Corriendo gen_witness + prove (subproceso)...")
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    peak_rss, aborted = _monitor(proc, ram_limit_gb * _GB)
    stdout, _ = proc.communicate()
    elapsed = time.time() - t0

    if aborted:
        return ProveMetrics(
            resolution=resolution, logrows=logrows,
            peak_bytes=peak_rss,
            witness_time_s=0.0, prove_time_s=elapsed,
            proof_bytes=0, proof_path=proof_path, witness_path=witness_out_path,
            aborted=True, abort_reason="ram_limit",
        )

    result = None
    for line in stdout.splitlines():
        if line.startswith("RESULT:"):
            result = json.loads(line[len("RESULT:"):])

    if result is None:
        print("  ERROR: el worker no devolvio resultado. Salida:")
        print(stdout)
        raise RuntimeError("prove falló (ver salida del worker)")

    peak = result.get("peak_wset_bytes") or peak_rss
    return ProveMetrics(
        resolution=resolution, logrows=logrows,
        peak_bytes=peak,
        witness_time_s=result["witness_time_s"],
        prove_time_s=result["prove_time_s"],
        proof_bytes=result["proof_bytes"],
        proof_path=proof_path,
        witness_path=witness_out_path,
        aborted=False,
    )

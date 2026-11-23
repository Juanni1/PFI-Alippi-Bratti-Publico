"""Paso 4 de Fase 0 — SRS local + setup (mide RAM pico y tamaño de PK).

1. Asegura el circuito compilado del Paso 3 (lo reutiliza si existe).
2. `ezkl.gen_srs(srs, logrows)` + `ezkl.setup()` en un **subproceso monitoreado**:
   se muestrea la RAM y se aborta si supera el presupuesto (`--ram-limit-gb`),
   para no congelar la máquina por swap a logrows altos.
3. Reporta RAM pico, tamaño de PK/VK/SRS y tiempos.

Ejecutar:  uv run python -m fase0.step4 --resolution 96 --ram-limit-gb 19
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import psutil

from fase0.circuit import circuit_paths, prepare_circuit

_GB = 1024 ** 3

# Anclaje empírico (Fase 0, α=0.5, num_inner_cols=2): PK ≈ 35.3 GiB a logrows 22.
# La PK escala ~×2 por cada +1 de logrows (≈ #columnas × 2^logrows × 32 bytes).
_PK_BYTES_AT_LOGROWS_22 = 35.3 * _GB


def estimate_pk_bytes(logrows: int) -> float:
    """Estimación del tamaño de PK (≈ RAM de proving) para un logrows dado."""
    return _PK_BYTES_AT_LOGROWS_22 * (2 ** (logrows - 22))


def _fmt_gb(n_bytes: float | None) -> str:
    if n_bytes is None:
        return "n/a"
    return f"{n_bytes / _GB:.2f} GB"


def _monitor(proc: subprocess.Popen, limit_bytes: float, poll_s: float = 0.1):
    """Muestrea el RSS del hijo (y sus descendientes); aborta si supera el límite.

    Devuelve (peak_rss_bytes, aborted).
    """
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


def run(
    resolution: int = 96,
    out_dir: str = "artifacts",
    ram_limit_gb: float = 19.0,
    num_inner_cols: int = 2,
    scales: list[int] | None = None,
    num_samples: int = 2,
    param_visibility: str = "fixed",
    force: bool = False,
):
    # --- 1. Circuito compilado (reutiliza el del Paso 3 si está) ------------------
    print(f"Asegurando circuito compilado a {resolution}x{resolution}...")
    metrics = prepare_circuit(
        resolution,
        out_dir,
        num_inner_cols=num_inner_cols,
        scales=scales,
        num_samples=num_samples,
        param_visibility=param_visibility,
        reuse=True,
    )
    logrows = metrics.logrows
    settings_path, compiled_path = circuit_paths(out_dir, resolution)
    srs_path = os.path.join(out_dir, f"kzg_{logrows}.srs")
    pk_path = os.path.join(out_dir, f"pk_r{resolution}.key")
    vk_path = os.path.join(out_dir, f"vk_r{resolution}.key")

    print(f"  logrows = {logrows} (2^{logrows} = {2 ** logrows:,} filas)")
    print(f"  presupuesto RAM = {ram_limit_gb:.0f} GB -> se aborta el setup si lo supera")

    # --- Preflight: disco + RAM de proving estimada -------------------------------
    pk_est = estimate_pk_bytes(logrows)
    free_disk = psutil.disk_usage(os.path.abspath(out_dir)).free
    print(f"  PK estimada (~RAM de proving): {_fmt_gb(pk_est)}  | disco libre: {_fmt_gb(free_disk)}")
    if pk_est > free_disk * 0.9 and not force:
        print(f"  ABORTADO (preflight): la PK estimada ({_fmt_gb(pk_est)}) no entra en disco "
              f"({_fmt_gb(free_disk)} libres). Usar --force para intentar igual.")
        return {"resolution": resolution, "logrows": logrows, "aborted": True,
                "reason": "preflight_disk", "pk_est_bytes": pk_est}
    if pk_est > ram_limit_gb * _GB:
        print(f"  AVISO: la PK estimada ({_fmt_gb(pk_est)}) excede el presupuesto de RAM "
              f"({ram_limit_gb:.0f} GB) -> el PROVING (Paso 5) no entrara, aunque el setup pueda escribir a disco.")

    # --- 2. gen_srs + setup en subproceso monitoreado -----------------------------
    cmd = [
        sys.executable, "-m", "fase0._setup_worker",
        "--compiled", compiled_path,
        "--srs", srs_path,
        "--pk", pk_path,
        "--vk", vk_path,
        "--logrows", str(logrows),
    ]
    print("Corriendo gen_srs + setup (subproceso)...")
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    peak_rss, aborted = _monitor(proc, ram_limit_gb * _GB)
    stdout, _ = proc.communicate()
    elapsed = time.time() - t0

    # --- 3. Reporte ---------------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"PASO 4 — resolucion {resolution}x{resolution} | logrows {logrows}")
    print("=" * 60)

    if aborted:
        print(f"  ABORTADO: el setup supero el presupuesto de {ram_limit_gb:.0f} GB")
        print(f"  RAM pico (muestreada) antes de abortar: {_fmt_gb(peak_rss)}")
        print(f"  -> resolucion {resolution} NO viable con {ram_limit_gb:.0f} GB de RAM")
        print(f"  tiempo hasta abortar: {elapsed:.1f}s")
        return {"resolution": resolution, "logrows": logrows, "aborted": True,
                "peak_rss_bytes": peak_rss}

    result = None
    for line in stdout.splitlines():
        if line.startswith("RESULT:"):
            result = json.loads(line[len("RESULT:"):])
    if result is None:
        print("  ERROR: el worker no devolvio resultado. Salida:")
        print(stdout)
        raise RuntimeError("setup falló (ver salida del worker)")

    pk_size = os.path.getsize(pk_path) if os.path.exists(pk_path) else None
    vk_size = os.path.getsize(vk_path) if os.path.exists(vk_path) else None
    srs_size = os.path.getsize(srs_path) if os.path.exists(srs_path) else None
    peak = result.get("peak_wset_bytes") or peak_rss

    print(f"  RAM pico (setup)   : {_fmt_gb(peak)}   [presupuesto {ram_limit_gb:.0f} GB]")
    print(f"  tamano PK          : {_fmt_gb(pk_size)}")
    print(f"  tamano VK          : {_fmt_gb(vk_size)}")
    print(f"  tamano SRS         : {_fmt_gb(srs_size)}")
    print(f"  tiempo gen_srs     : {result['srs_time_s']:.1f}s")
    print(f"  tiempo setup       : {result['setup_time_s']:.1f}s")
    headroom = ram_limit_gb * _GB - peak
    verdict = "VIABLE" if headroom > 0 else "EXCEDE presupuesto"
    print(f"  -> {verdict} (margen {_fmt_gb(headroom)} sobre {ram_limit_gb:.0f} GB)")
    return {
        "resolution": resolution, "logrows": logrows, "aborted": False,
        "peak_bytes": peak, "pk_bytes": pk_size, "vk_bytes": vk_size, "srs_bytes": srs_size,
        "srs_time_s": result["srs_time_s"], "setup_time_s": result["setup_time_s"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Paso 4 Fase 0: SRS + setup, mide RAM y PK")
    parser.add_argument("--resolution", type=int, default=96)
    parser.add_argument("--out-dir", type=str, default="artifacts")
    parser.add_argument("--ram-limit-gb", type=float, default=19.0)
    parser.add_argument("--num-inner-cols", type=int, default=2)
    parser.add_argument("--num-samples", type=int, default=2)
    parser.add_argument("--scales", type=int, nargs="*", default=None)
    parser.add_argument("--force", action="store_true",
                        help="ignora el preflight de disco (PK que no entra en disco)")
    args = parser.parse_args()
    run(
        resolution=args.resolution,
        out_dir=args.out_dir,
        ram_limit_gb=args.ram_limit_gb,
        num_inner_cols=args.num_inner_cols,
        scales=args.scales,
        num_samples=args.num_samples,
        force=args.force,
    )


if __name__ == "__main__":
    main()

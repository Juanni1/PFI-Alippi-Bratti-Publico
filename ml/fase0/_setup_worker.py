"""Worker del Paso 4: gen_srs + setup en un subproceso aislado.

Se ejecuta como `python -m fase0._setup_worker ...` desde `step4.py`, que lo
monitorea y lo aborta si supera el presupuesto de RAM. Aislarlo en su propio
proceso permite (a) medir su pico de working set sin el ruido del proceso padre y
(b) matarlo sin arrastrar la sesión si se dispara la memoria.

Imprime una única línea JSON con tiempos y pico de RAM (clave `RESULT:`).
"""

from __future__ import annotations

import argparse
import json
import os
import time

# EZKL (Rust) hace `std::env::var("HOME").unwrap()` durante setup/prove; en Windows
# HOME no existe por defecto y panickea con `NotPresent`. Lo derivamos de USERPROFILE
# ANTES de importar ezkl.
if not os.environ.get("HOME"):
    os.environ["HOME"] = os.environ.get("USERPROFILE", os.path.expanduser("~"))

import ezkl
import psutil


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--compiled", required=True)
    p.add_argument("--srs", required=True)
    p.add_argument("--pk", required=True)
    p.add_argument("--vk", required=True)
    p.add_argument("--logrows", type=int, required=True)
    a = p.parse_args()

    # Nota: gen_srs devuelve None (no bool) y solo escribe el archivo; verificamos
    # por existencia, no por valor de retorno.
    t0 = time.time()
    ezkl.gen_srs(a.srs, a.logrows)
    if not os.path.exists(a.srs):
        raise RuntimeError("ezkl.gen_srs no creó el archivo SRS")
    t1 = time.time()
    # setup(model, vk, pk, srs, witness=None, disable_selector_compression=False):
    # compresión activada (más chica la PK, aunque el ahorro es marginal en este modelo).
    ezkl.setup(a.compiled, a.vk, a.pk, a.srs, None, False)
    if not (os.path.exists(a.pk) and os.path.exists(a.vk)):
        raise RuntimeError("ezkl.setup no creó pk/vk")
    t2 = time.time()

    mi = psutil.Process().memory_info()
    result = {
        "ok": True,
        "srs_time_s": t1 - t0,
        "setup_time_s": t2 - t1,
        # peak_wset = pico de working set del proceso (exacto en Windows)
        "peak_wset_bytes": getattr(mi, "peak_wset", None),
        "rss_end_bytes": mi.rss,
    }
    print("RESULT:" + json.dumps(result))


if __name__ == "__main__":
    main()

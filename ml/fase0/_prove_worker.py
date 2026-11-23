"""Worker del Paso 5: gen_witness + prove en un subproceso aislado.

Se ejecuta como `python -m fase0._prove_worker ...` desde `proving.py`, que lo
monitorea y lo aborta si supera el presupuesto de RAM.

Imprime una única línea JSON con métricas (clave `RESULT:`).

Se imprime `help(ezkl.gen_witness)` al arrancar para facilitar debugging si la
firma de EZKL cambia entre versiones.
"""

from __future__ import annotations

import argparse
import json
import os
import time

# HOME-fix Windows: EZKL (Rust) hace `std::env::var("HOME").unwrap()` en prove.
if not os.environ.get("HOME"):
    os.environ["HOME"] = os.environ.get("USERPROFILE", os.path.expanduser("~"))

import ezkl
import psutil


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--compiled", required=True)
    p.add_argument("--witness-input", required=True, help="JSON con input_data para gen_witness")
    p.add_argument("--witness-out", required=True, help="Donde escribir el witness.json")
    p.add_argument("--pk", required=True)
    p.add_argument("--proof-out", required=True)
    p.add_argument("--srs", required=True)
    p.add_argument("--settings", required=True)
    a = p.parse_args()

    # Imprimir firma para facilitar debugging si cambia en una versión de ezkl.
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        help(ezkl.gen_witness)
    print("GEN_WITNESS_HELP:" + buf.getvalue().replace("\n", " "), flush=True)

    # gen_witness: firma esperada según patrones de ezkl 23.x.
    # Si falla, la salida de GEN_WITNESS_HELP arriba permite ajustar.
    t0 = time.time()
    ok = ezkl.gen_witness(
        a.witness_input,
        a.compiled,
        a.witness_out,
        vk_path=None,
        srs_path=a.srs,
    )
    if not ok and not os.path.exists(a.witness_out):
        raise RuntimeError("ezkl.gen_witness no generó el witness")
    t1 = time.time()

    # prove en EZKL 23.0.5 no recibe proof_type.
    ezkl.prove(a.witness_out, a.compiled, a.pk, a.proof_out, a.srs)
    if not os.path.exists(a.proof_out):
        raise RuntimeError("ezkl.prove no generó el archivo de prueba")
    t2 = time.time()

    proof_bytes = os.path.getsize(a.proof_out)
    mi = psutil.Process().memory_info()
    result = {
        "ok": True,
        "witness_time_s": t1 - t0,
        "prove_time_s": t2 - t1,
        "peak_wset_bytes": getattr(mi, "peak_wset", None),
        "rss_end_bytes": mi.rss,
        "proof_bytes": proof_bytes,
    }
    print("RESULT:" + json.dumps(result))


if __name__ == "__main__":
    main()

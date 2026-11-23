"""Prueba zk de la CABEZA del approach features-afuera — cierre end-to-end.

En la arquitectura features-afuera, lo único que va al circuito es la **cabeza**
(un `Linear` sobre el vector de features del backbone). Este script cierra ese
camino: exporta la cabeza entrenada a ONNX, arma el circuito EZKL, genera una prueba
sobre un **vector de features real** y la verifica. Demuestra que la cabeza se prueba
con un circuito **mínimo** (logrows chiquito) — el contraste con la SmallCNN
end-to-end (logrows 21) es el punto de la tesis.

Entrada del circuito = **vector de features** (no una imagen). Esquema de visibilidad:
- **features públicas** (`input_visibility="public"`): en el diseño (Caso A de las notas),
  el verificador recomputa el backbone y chequea que las features coincidan con la
  imagen, así que son dato público.
- **logits públicos**, **pesos de la cabeza fijos** en el circuito.

Reusa las features **cacheadas** por `fase1.features_afuera` (no recomputa el backbone)
y los workers monitoreados de Fase 0.

Ejecutar:
  uv run python -m fase1.prove_head --ckpt fase1/checkpoints/head_mobilenet_v3_small_r224.pt
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import torch

from fase0.circuit import build_circuit, circuit_paths, read_metrics
from fase1.features_afuera import build_head

_GB = 1024 ** 3


def load_head(ckpt_path: str):
    """Reconstruye la cabeza desde el `config` guardado y le carga los pesos."""
    ck = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    cfg = ck["config"]
    head = build_head(cfg["feat_dim"], cfg["num_classes"], cfg["hidden"])
    head.load_state_dict(ck["state_dict"])
    head.eval()
    return head, ck


def export_head_onnx(head, feat_dim: int, onnx_path: str) -> str:
    """Exporta la cabeza a ONNX (entrada = vector de features [1, feat_dim])."""
    os.makedirs(os.path.dirname(os.path.abspath(onnx_path)), exist_ok=True)
    dummy = torch.randn(1, feat_dim)
    torch.onnx.export(
        head, dummy, onnx_path,
        opset_version=13, do_constant_folding=True,
        input_names=["features"], output_names=["logits"], dynamo=False,
    )
    return onnx_path


def write_input_json(vectors: torch.Tensor, path: str) -> str:
    """Escribe features al formato EZKL `{"input_data": [...]}` (una fila por muestra)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = [vectors[i].flatten().tolist() for i in range(vectors.shape[0])]
    with open(path, "w") as f:
        json.dump({"input_data": data}, f)
    return path


def run_worker(cmd: list[str]) -> dict:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    stdout, _ = proc.communicate()
    result = None
    for line in stdout.splitlines():
        if line.startswith("RESULT:"):
            result = json.loads(line[len("RESULT:"):])
    if result is None:
        print(stdout)
        raise RuntimeError(f"worker sin RESULT: {cmd[2]}")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Prueba zk de la cabeza (features-afuera).")
    p.add_argument("--ckpt", required=True, help="Checkpoint de la cabeza (fase1.features_afuera).")
    p.add_argument("--cache-dir", default="fase1/feat_cache")
    p.add_argument("--split", default="test", choices=["train", "val", "test"])
    p.add_argument("--out-dir", default="artifacts_head")
    p.add_argument("--num-calib", type=int, default=8)
    p.add_argument("--ram-limit-gb", type=float, default=38.0)
    args = p.parse_args()

    if not os.environ.get("HOME"):
        os.environ["HOME"] = os.environ.get("USERPROFILE", os.path.expanduser("~"))

    os.makedirs(args.out_dir, exist_ok=True)
    head, ck = load_head(args.ckpt)
    cfg = ck["config"]
    feat_dim = cfg["feat_dim"]
    classes = ck.get("classes") or [str(i) for i in range(cfg["num_classes"])]
    print(f"Cabeza: {cfg['backbone']} feat_dim={feat_dim} hidden={cfg['hidden']} | clases {classes}")

    # --- Features reales cacheadas (no se recomputa el backbone) ------------------
    cache_path = os.path.join(args.cache_dir, f"{cfg['backbone']}_r{cfg['resolution']}_{args.split}.pt")
    if not os.path.exists(cache_path):
        raise FileNotFoundError(f"No hay caché de features: {cache_path} — correr fase1.features_afuera primero")
    blob = torch.load(cache_path, map_location="cpu")
    feats, labels = blob["features"], blob["labels"]

    # Witness: un vector de features real. Inferencia float de referencia.
    x = feats[0:1]  # [1, feat_dim]
    true_class = classes[int(labels[0])]
    with torch.no_grad():
        logits = head(x).squeeze(0)
    pred_idx = int(logits.argmax())
    print(f"Parche de {args.split}: clase real {true_class} | predicción float {classes[pred_idx]}")

    # --- Export ONNX de la cabeza + circuito -------------------------------------
    onnx_path = os.path.join(args.out_dir, f"head_{cfg['backbone']}.onnx")
    export_head_onnx(head, feat_dim, onnx_path)

    calib = feats[1: 1 + args.num_calib]
    calib_path = os.path.join(args.out_dir, "calib_features.json")
    write_input_json(calib, calib_path)

    print("\nCompilando circuito de la cabeza (features públicas, pesos fijos)...")
    metrics = build_circuit(
        onnx_path, calib_path, args.out_dir,
        resolution=feat_dim, input_visibility="public", scales=[7],
    )
    logrows = metrics.logrows
    print(f"  LOGROWS = {logrows} (2^{logrows} = {2 ** logrows:,} filas)  vs SmallCNN end-to-end: 21")

    settings_path, compiled_path = circuit_paths(args.out_dir, feat_dim)
    srs_path = os.path.join(args.out_dir, f"kzg_{logrows}.srs")
    pk_path = os.path.join(args.out_dir, f"pk_r{feat_dim}.key")
    vk_path = os.path.join(args.out_dir, f"vk_r{feat_dim}.key")

    # --- setup -------------------------------------------------------------------
    print("\nsetup (gen_srs + setup)...")
    t0 = time.time()
    sres = run_worker([sys.executable, "-m", "fase0._setup_worker",
                       "--compiled", compiled_path, "--srs", srs_path,
                       "--pk", pk_path, "--vk", vk_path, "--logrows", str(logrows)])
    pk_mb = os.path.getsize(pk_path) / (1024 ** 2)
    print(f"  setup OK | RAM pico {(sres.get('peak_wset_bytes') or 0)/_GB:.2f} GB | "
          f"PK {pk_mb:.1f} MB | {time.time()-t0:.1f}s")

    # --- witness (features reales) + prove ---------------------------------------
    win = os.path.join(args.out_dir, f"witness_input_r{feat_dim}.json")
    wout = os.path.join(args.out_dir, f"witness_r{feat_dim}.json")
    proof_path = os.path.join(args.out_dir, f"proof_r{feat_dim}.json")
    write_input_json(x, win)

    print("\ngen_witness (features reales) + prove...")
    t0 = time.time()
    pres = run_worker([sys.executable, "-m", "fase0._prove_worker",
                       "--compiled", compiled_path, "--witness-input", win,
                       "--witness-out", wout, "--pk", pk_path,
                       "--proof-out", proof_path, "--srs", srs_path, "--settings", settings_path])
    print(f"  prove OK | RAM pico {(pres.get('peak_wset_bytes') or 0)/_GB:.2f} GB | "
          f"prueba {pres['proof_bytes']/1024:.1f} KB | {time.time()-t0:.1f}s")

    # --- verify ------------------------------------------------------------------
    import ezkl
    ok = ezkl.verify(proof_path, settings_path, vk_path, srs_path)
    print("\n" + "=" * 60)
    if ok:
        print("VERIFY OK — prueba zk válida de la CABEZA (features-afuera)")
        print(f"  features reales ({true_class}) -> {classes[pred_idx]} | logrows {logrows} | "
              f"prueba {pres['proof_bytes']/1024:.1f} KB")
        print("  (la SmallCNN end-to-end necesitaba logrows 21 / ~30 GB — acá el circuito es mínimo)")
    else:
        print("VERIFY FALLÓ")
        raise SystemExit(1)
    print("=" * 60)


if __name__ == "__main__":
    main()

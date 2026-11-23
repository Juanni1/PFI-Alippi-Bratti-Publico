"""Fidelidad circuito-vs-float de la CABEZA (features-afuera) — el RNF-14 de esa arquitectura.

`fase1/circuit_fidelity.py` mide lo mismo para la SmallCNN end-to-end y da 97,7 % sobre 300
parches, pero no sirve acá: está atado al modelo entrenado vía `prove_trained.load_trained_model`
y `model.input_resolution`, y su entrada es una imagen. El circuito de features-afuera consume
un **vector de 576 features**, no un parche.

Lo que se mide es idéntico en espíritu: el circuito opera en punto fijo (scale 7) y la cabeza
en float32. Si la cuantización cambiara el argmax, la prueba sería fiel al circuito pero no al
clasificador que se auditó, y la garantía se estaría dando sobre otra decisión.

Reusa el circuito que dejó `prove_head.py` en `artifacts_head/` (mismos nombres: los artefactos
se etiquetan con `resolution = feat_dim`). Si no está, lo compila igual que `prove_head`. Corre
`gen_witness`, que ejecuta el circuito hacia adelante sin generar la prueba — barato.

Ejecutar:
  uv run python -m fase1.head_fidelity --ckpt fase1/checkpoints/head_mnv3_ft.pt --num-samples 300
"""

from __future__ import annotations

import argparse
import os
import random

# HOME-fix Windows antes de importar ezkl (igual que en los workers de Fase 0).
if not os.environ.get("HOME"):
    os.environ["HOME"] = os.environ.get("USERPROFILE", os.path.expanduser("~"))

import ezkl
import torch

from fase0.circuit import build_circuit, circuit_paths, read_metrics
from fase1.circuit_fidelity import circuit_logits
from fase1.prove_head import export_head_onnx, load_head, write_input_json


def ensure_head_circuit(head, cfg: dict, feats: torch.Tensor, out_dir: str, num_calib: int):
    """Reutiliza el circuito de la cabeza si `prove_head` ya lo compiló; si no, lo compila igual."""
    feat_dim = cfg["feat_dim"]
    settings_path, compiled_path = circuit_paths(out_dir, feat_dim)
    if os.path.exists(settings_path) and os.path.exists(compiled_path):
        return read_metrics(settings_path, compiled_path, feat_dim)

    onnx_path = os.path.join(out_dir, f"head_{cfg['backbone']}.onnx")
    export_head_onnx(head, feat_dim, onnx_path)
    calib_path = os.path.join(out_dir, "calib_features.json")
    write_input_json(feats[1: 1 + num_calib], calib_path)
    # Mismo esquema que prove_head: features públicas, logits públicos, pesos fijos.
    return build_circuit(
        onnx_path, calib_path, out_dir,
        resolution=feat_dim, input_visibility="public", scales=[7],
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Fidelidad circuito-vs-float de la cabeza (features-afuera).")
    p.add_argument("--ckpt", required=True, help="Checkpoint de la cabeza (features_afuera o prep_finetuned_proof).")
    p.add_argument("--cache-dir", default="fase1/feat_cache")
    p.add_argument("--split", default="test", choices=["train", "val", "test"])
    p.add_argument("--out-dir", default="artifacts_head")
    p.add_argument("--num-samples", type=int, default=300, help="Igual que la corrida de la SmallCNN.")
    p.add_argument("--num-calib", type=int, default=8)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    head, ck = load_head(args.ckpt)
    cfg = ck["config"]
    classes = ck.get("classes") or [str(i) for i in range(cfg["num_classes"])]

    cache_path = os.path.join(args.cache_dir, f"{cfg['backbone']}_r{cfg['resolution']}_{args.split}.pt")
    if not os.path.exists(cache_path):
        raise FileNotFoundError(
            f"No hay caché de features: {cache_path}\n"
            "  backbone congelado  -> uv run python -m fase1.features_afuera --eval-test\n"
            "  backbone fine-tuneado -> uv run python -m fase1.prep_finetuned_proof"
        )
    blob = torch.load(cache_path, map_location="cpu")
    feats, labels = blob["features"], blob["labels"]

    os.makedirs(args.out_dir, exist_ok=True)
    metrics = ensure_head_circuit(head, cfg, feats, args.out_dir, args.num_calib)
    _, compiled_path = circuit_paths(args.out_dir, cfg["feat_dim"])
    srs_path = os.path.join(args.out_dir, f"kzg_{metrics.logrows}.srs")
    if not os.path.exists(srs_path):
        ezkl.gen_srs(srs_path, metrics.logrows)

    win = os.path.join(args.out_dir, "_fid_head_win.json")
    wout = os.path.join(args.out_dir, "_fid_head_wout.json")

    idxs = list(range(feats.size(0)))
    random.Random(args.seed).shuffle(idxs)
    idxs = idxs[: args.num_samples]
    print(f"Cabeza: {os.path.basename(args.ckpt)} | backbone {cfg['backbone']} | "
          f"logrows {metrics.logrows} | evaluando {len(idxs)} vectores de '{args.split}'\n")

    n = float_correct = circuit_correct = agree = 0
    disagreements = []
    for i in idxs:
        x = feats[i: i + 1]
        true_idx = int(labels[i])
        with torch.no_grad():
            float_logits = head(x).squeeze(0).tolist()
        float_idx = max(range(len(float_logits)), key=lambda k: float_logits[k])

        write_input_json(x, win)
        c_logits = circuit_logits(win, compiled_path, wout, srs_path)
        if c_logits is None:
            print("  (no se pudieron leer los logits del circuito; abortando)")
            return
        circuit_idx = max(range(len(c_logits)), key=lambda k: c_logits[k])

        n += 1
        float_correct += float_idx == true_idx
        circuit_correct += circuit_idx == true_idx
        if float_idx == circuit_idx:
            agree += 1
        else:
            disagreements.append((i, classes[true_idx], classes[float_idx], classes[circuit_idx]))
        if n % 25 == 0:
            print(f"  {n}/{len(idxs)} | agreement {agree / n:.3f}")

    print("\n" + "=" * 60)
    print(f"FIDELIDAD circuito-vs-float — CABEZA ({n} vectores, logrows {metrics.logrows})")
    print("=" * 60)
    print(f"  Agreement (argmax circuito == float) : {agree}/{n} = {agree / n:.3f}")
    print(f"  Accuracy float    (vs etiqueta real) : {float_correct / n:.3f}")
    print(f"  Accuracy circuito (vs etiqueta real) : {circuit_correct / n:.3f}")
    print(f"  Referencia SmallCNN end-to-end       : 0.977 sobre 300 parches")
    if disagreements:
        print(f"\n  Desacuerdos ({len(disagreements)}):")
        for i, true_c, f_pred, c_pred in disagreements[:20]:
            print(f"    vector {i} (real {true_c}): float={f_pred}  circuito={c_pred}")
    else:
        print("\n  Sin desacuerdos: la cuantización preservó TODAS las predicciones.")


if __name__ == "__main__":
    main()

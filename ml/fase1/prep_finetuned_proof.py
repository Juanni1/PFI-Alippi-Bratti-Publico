"""Puente: deja lista la prueba zk del modelo features-afuera FINE-TUNEADO (92.7%).

`prove_head.py` prueba la cabeza usando features **cacheadas**. Pero la caché que hay
la generó `features_afuera.py` con el backbone **congelado** (ImageNet) → esas features
NO corresponden al backbone fine-tuneado, así que probar la cabeza fine-tuneada contra
ellas daría una inferencia incoherente. Además, el checkpoint de `finetune_afuera.py`
guarda la cabeza bajo `head_state_dict`, no `state_dict` (que es lo que `prove_head`
espera). Este script cierra los dos huecos:

  1. Reconstruye el backbone **fine-tuneado** desde el checkpoint de `finetune_afuera`.
  2. Re-extrae las features de los parches con ESE backbone → caché nueva
     (`{tag}_r224_{split}.pt`, no pisa la del congelado).
  3. Guarda la cabeza en el formato exacto que consume `prove_head` (clave `state_dict`
     + `config` con `backbone={tag}`, para que apunte a la caché nueva).

Después, la prueba del modelo de 92.7% es un solo comando (circuito idéntico al del
congelado: `Linear 576→6`, logrows 15):

  uv run python -m fase1.prep_finetuned_proof                       # genera caché + head ckpt
  uv run python -m fase1.prove_head --ckpt fase1/checkpoints/head_mnv3_ft.pt   # prueba + verify

Con esto el "92.7% + VERIFY OK" queda cerrado en un único artefacto (sin el asterisco
de que la prueba verificada era la del modelo congelado de 87.5%).
"""

from __future__ import annotations

import argparse
import os
import time

import torch
from torch.utils.data import DataLoader
from torchvision.datasets import ImageFolder

from fase1.finetune_afuera import build_finetunable_model, build_transforms

# Nombre del backbone fine-tuneado. Define el nombre de la caché y del head ckpt, y NO
# debe coincidir con "mobilenet_v3_small" (el congelado) para no mezclar features.
TAG = "mnv3_ft"


@torch.no_grad()
def extract_features(backbone: torch.nn.Module, loader: DataLoader, device: str):
    """Pasa el split por el backbone fine-tuneado → (features [N, 576], labels [N])."""
    feats, labels = [], []
    for x, y in loader:
        feats.append(backbone(x.to(device)).cpu())
        labels.append(y)
    return torch.cat(feats), torch.cat(labels)


def main() -> None:
    p = argparse.ArgumentParser(description="Prepara la prueba zk del modelo fine-tuneado.")
    p.add_argument("--finetune-ckpt", default="fase1/checkpoints/finetune_mnv3_all.pt",
                   help="Checkpoint de fase1.finetune_afuera.")
    p.add_argument("--data", default="fase1/patches")
    p.add_argument("--cache-dir", default="fase1/feat_cache")
    p.add_argument("--out", default="fase1/checkpoints/head_mnv3_ft.pt",
                   help="Dónde guardar la cabeza en formato prove_head.")
    p.add_argument("--splits", nargs="+", default=["test"], choices=["train", "val", "test"],
                   help="Splits a re-extraer (prove_head con default solo necesita 'test').")
    p.add_argument("--num-workers", type=int, default=8)
    args = p.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    ck = torch.load(args.finetune_ckpt, map_location="cpu", weights_only=False)
    cfg = ck["config"]
    classes = ck["classes"]
    num_classes = cfg["num_classes"]
    assert cfg["backbone"] == "mobilenet_v3_small" and cfg["feat_dim"] == 576, \
        f"checkpoint inesperado: {cfg}"
    print(f"Fine-tune ckpt: {args.finetune_ckpt} | unfreeze={cfg.get('unfreeze')} | "
          f"val_acc {ck['val_metrics']['acc']:.3f} | device {device}")

    # --- 1. Reconstruir el modelo fine-tuneado y sacar el backbone --------------
    model, _ = build_finetunable_model(num_classes, "all")  # misma estructura Sequential(backbone, head)
    model.load_state_dict(ck["model_state_dict"])
    model.eval().to(device)
    backbone = model[0]  # nn.Sequential(features, AdaptiveAvgPool2d(1), Flatten())

    # --- 2. Re-extraer features con el backbone fine-tuneado --------------------
    _, eval_tf = build_transforms()
    os.makedirs(args.cache_dir, exist_ok=True)
    for split in args.splits:
        ds = ImageFolder(os.path.join(args.data, split), transform=eval_tf)
        assert ds.classes == classes, f"clases de {split} no coinciden con el checkpoint"
        loader = DataLoader(ds, batch_size=64, shuffle=False, num_workers=args.num_workers)
        t0 = time.time()
        feats, labels = extract_features(backbone, loader, device)
        cache_path = os.path.join(args.cache_dir, f"{TAG}_r224_{split}.pt")
        torch.save({"features": feats, "labels": labels, "classes": classes}, cache_path)
        print(f"  [{split}] {len(ds)} imgs -> features {tuple(feats.shape)} -> {cache_path} ({time.time()-t0:.1f}s)")

    # --- 3. Guardar la cabeza en el formato de prove_head -----------------------
    head_ckpt = {
        "state_dict": ck["head_state_dict"],           # prove_head espera 'state_dict'
        "classes": classes,
        "class_to_idx": ck.get("class_to_idx"),
        "config": {
            "backbone": TAG,          # → prove_head buscará la caché {TAG}_r224_{split}.pt
            "resolution": 224,
            "feat_dim": 576,
            "hidden": 0,
            "num_classes": num_classes,
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    torch.save(head_ckpt, args.out)
    print(f"\nCabeza lista → {args.out}")
    print("Ahora:")
    print(f"  uv run python -m fase1.prove_head --ckpt {args.out}")


if __name__ == "__main__":
    main()

"""Prototipo "features afuera" — extractor pre-entrenado CONGELADO + cabeza chica entrenable.

Mide el **techo de accuracy** de la arquitectura alternativa que discutimos: en vez de
probar todo el camino píxel→decisión (lo que obliga a la SmallCNN diminuta, techo
~70-76%), se deja la **extracción de features fuera del circuito** y se entrena solo
una **cabeza** sobre las features de un backbone grande pre-entrenado en ImageNet.

    imagen → [ BACKBONE pre-entrenado ]  →  vector de features  →  [ CABEZA ]  →  logits
             CONGELADO · corre AFUERA                              entrenable · iría EN el circuito

Reparto respecto al zk (a futuro, no en este script):
- El **backbone** corre en claro, fuera de la prueba. Como no entra al circuito, NO
  necesita ser zk-friendly ni de baja resolución → usamos su resolución nativa (224).
  Si el verificador ve la imagen, recomputa el backbone y certifica las features gratis
  (ver charla del "Caso A"); si no, hace falta atestación/destilación.
- La **cabeza** (este `Linear`) es lo único que se probaría con EZKL. Es trivial en
  circuito (un vector de ~1280 nº × una matriz) → logrows chiquito, sin VM.

Este script NO genera pruebas: solo responde "¿cuánta accuracy ganamos?". El número
que produce (test acc / macro-F1) se compara contra el 0.707 de la SmallCNN para
evaluar si justifica el cambio de arquitectura.

Truco de velocidad: el backbone está congelado → sus features son fijas por imagen.
Se **precomputan una vez** y se cachean a disco (`.pt`, ignorado por git); después la
cabeza entrena sobre los vectores cacheados, que es casi instantáneo (es una regresión
logística sobre ~1280 dims). Por eso este prototipo corre en minutos.

Ejecutar (en la PC con torch + parches):
  uv run python -m fase1.features_afuera --eval-test
  uv run python -m fase1.features_afuera --backbone resnet18 --hidden 64 --eval-test
"""

from __future__ import annotations

import argparse
import os
import time

import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset
from torchvision import models, transforms
from torchvision.datasets import ImageFolder

from fase0.calibration import IMAGENET_MEAN, IMAGENET_STD
from fase1.train import class_weights_from_dataset, evaluate


# --- Backbones disponibles ----------------------------------------------------
# Cada uno mapea una imagen normalizada a un vector de features (post global pool).
# El backbone queda CONGELADO (eval + requires_grad=False): no se entrena.
def build_backbone(name: str) -> tuple[nn.Module, int]:
    """Devuelve (extractor_congelado, dim_features) para el backbone pedido."""
    if name == "mobilenet_v2":
        m = models.mobilenet_v2(weights=models.MobileNet_V2_Weights.IMAGENET1K_V1)
        feat = nn.Sequential(m.features, nn.AdaptiveAvgPool2d(1), nn.Flatten())
        dim = 1280
    elif name == "mobilenet_v3_small":
        m = models.mobilenet_v3_small(weights=models.MobileNet_V3_Small_Weights.IMAGENET1K_V1)
        feat = nn.Sequential(m.features, nn.AdaptiveAvgPool2d(1), nn.Flatten())
        dim = 576
    elif name == "resnet18":
        m = models.resnet18(weights=models.ResNet18_Weights.IMAGENET1K_V1)
        feat = nn.Sequential(*list(m.children())[:-1], nn.Flatten())  # quita la fc final
        dim = 512
    else:
        raise ValueError(f"backbone desconocido: {name!r} (mobilenet_v2|mobilenet_v3_small|resnet18)")
    feat.eval()
    for p in feat.parameters():
        p.requires_grad_(False)
    return feat, dim


def eval_transform(resolution: int) -> transforms.Compose:
    """Resize a la resolución nativa del backbone + normalización ImageNet (sin augmentation)."""
    return transforms.Compose(
        [
            transforms.Resize((resolution, resolution)),
            transforms.ToTensor(),
            transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
        ]
    )


@torch.no_grad()
def extract_features(backbone: nn.Module, loader: DataLoader, device: str) -> tuple[torch.Tensor, torch.Tensor]:
    """Pasa todo el split por el backbone congelado → (features [N, dim], labels [N])."""
    feats, labels = [], []
    for x, y in loader:
        feats.append(backbone(x.to(device)).cpu())
        labels.append(y)
    return torch.cat(feats), torch.cat(labels)


def cached_features(
    backbone: nn.Module, data_root: str, split: str, resolution: int, backbone_name: str,
    cache_dir: str, device: str, num_workers: int,
) -> tuple[torch.Tensor, torch.Tensor, ImageFolder]:
    """Devuelve (features, labels, dataset). Cachea a disco para no recomputar el backbone."""
    ds = ImageFolder(os.path.join(data_root, split), transform=eval_transform(resolution))
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{backbone_name}_r{resolution}_{split}.pt")
    if os.path.exists(cache_path):
        blob = torch.load(cache_path, map_location="cpu")
        if blob.get("classes") == ds.classes and blob["features"].size(0) == len(ds):
            print(f"  [{split}] features desde caché ({cache_path})")
            return blob["features"], blob["labels"], ds
    loader = DataLoader(ds, batch_size=64, shuffle=False, num_workers=num_workers)
    t0 = time.time()
    feats, labels = extract_features(backbone, loader, device)
    torch.save({"features": feats, "labels": labels, "classes": ds.classes}, cache_path)
    print(f"  [{split}] {len(ds)} imágenes → features {tuple(feats.shape)} en {time.time() - t0:.1f}s")
    return feats, labels, ds


def build_head(feat_dim: int, num_classes: int, hidden: int) -> nn.Module:
    """La cabeza (lo único que iría al circuito). hidden=0 → Linear puro (lo más barato en zk).

    Si hidden>0, MLP con ReLU6 (no linealidad zk-friendly, igual que la SmallCNN) — algo
    más de accuracy a cambio de un circuito un poco más caro.
    """
    if hidden > 0:
        return nn.Sequential(
            nn.Linear(feat_dim, hidden),
            nn.ReLU6(inplace=True),
            nn.Linear(hidden, num_classes),
        )
    return nn.Linear(feat_dim, num_classes)


def main() -> None:
    p = argparse.ArgumentParser(description="Prototipo features-afuera: backbone congelado + cabeza entrenable.")
    p.add_argument("--data", default="fase1/patches", help="Raíz con train/val/test (ImageFolder).")
    p.add_argument("--backbone", default="mobilenet_v2",
                   choices=["mobilenet_v2", "mobilenet_v3_small", "resnet18"])
    p.add_argument("--resolution", type=int, default=224, help="Resolución del backbone (afuera del circuito → libre).")
    p.add_argument("--hidden", type=int, default=0, help="Tamaño de capa oculta de la cabeza (0 = Linear puro).")
    p.add_argument("--epochs", type=int, default=100)
    p.add_argument("--batch-size", type=int, default=128)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--weight-decay", type=float, default=1e-4)
    p.add_argument("--num-workers", type=int, default=0, help="Workers para extraer features (0 = seguro en Windows).")
    p.add_argument("--class-weights", choices=["inverse", "sqrt", "none"], default="none",
                   help="Igual que train.py. 'none' = receta nueva (maximiza accuracy global).")
    p.add_argument("--label-smoothing", type=float, default=0.1)
    p.add_argument("--select-metric", choices=["accuracy", "macro_f1", "balanced_acc"], default="accuracy")
    p.add_argument("--cache-dir", default="fase1/feat_cache", help="Caché de features (.pt, ignorado por git).")
    p.add_argument("--out", default="fase1/checkpoints", help="Carpeta del checkpoint de la cabeza.")
    p.add_argument("--eval-test", action="store_true")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    metric_key = {"accuracy": "acc", "macro_f1": "macro_f1", "balanced_acc": "balanced_acc"}[args.select_metric]
    torch.manual_seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    print(f"Backbone: {args.backbone} (congelado) | resolución {args.resolution} | device {device}")
    backbone, feat_dim = build_backbone(args.backbone)
    backbone = backbone.to(device)

    # --- 1. Features (precomputadas + cacheadas) --------------------------------
    print("Extrayendo features (backbone congelado):")
    f_train, y_train, train_ds = cached_features(
        backbone, args.data, "train", args.resolution, args.backbone, args.cache_dir, device, args.num_workers)
    f_val, y_val, val_ds = cached_features(
        backbone, args.data, "val", args.resolution, args.backbone, args.cache_dir, device, args.num_workers)

    classes = train_ds.classes
    num_classes = len(classes)
    assert val_ds.classes == classes, "Clases de train/val no coinciden."
    print(f"Clases ({num_classes}): {classes} | feat_dim {feat_dim}")

    train_loader = DataLoader(TensorDataset(f_train, y_train), batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(TensorDataset(f_val, y_val), batch_size=args.batch_size, shuffle=False)

    # --- 2. Cabeza entrenable (lo único que iría al circuito) --------------------
    head = build_head(feat_dim, num_classes, args.hidden).to(device)
    n_params = sum(p.numel() for p in head.parameters())
    print(f"Cabeza: {'Linear' if args.hidden == 0 else f'MLP(hidden={args.hidden})'} | {n_params:,} parámetros")

    weights = class_weights_from_dataset(train_ds, num_classes, args.class_weights)
    if weights is not None:
        weights = weights.to(device)
    criterion = nn.CrossEntropyLoss(weight=weights, label_smoothing=args.label_smoothing)
    optimizer = torch.optim.AdamW(head.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
    print(f"Receta: class_weights={args.class_weights} | select={args.select_metric} | "
          f"label_smoothing={args.label_smoothing} | epochs={args.epochs}\n")

    os.makedirs(args.out, exist_ok=True)
    tag = f"{args.backbone}_r{args.resolution}" + (f"_h{args.hidden}" if args.hidden else "")
    ckpt_path = os.path.join(args.out, f"head_{tag}.pt")
    best_metric = -1.0

    for epoch in range(1, args.epochs + 1):
        head.train()
        running = 0.0
        for x, y in train_loader:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad()
            loss = criterion(head(x), y)
            loss.backward()
            optimizer.step()
            running += loss.item() * x.size(0)
        scheduler.step()
        train_loss = running / len(train_ds)

        m = evaluate(head, val_loader, num_classes, device)
        flag = ""
        if m[metric_key] > best_metric:
            best_metric = m[metric_key]
            torch.save(
                {
                    "state_dict": head.state_dict(),
                    "class_to_idx": train_ds.class_to_idx,
                    "classes": classes,
                    "config": {
                        "backbone": args.backbone,
                        "resolution": args.resolution,
                        "feat_dim": feat_dim,
                        "hidden": args.hidden,
                        "num_classes": num_classes,
                    },
                    "val_metrics": m,
                },
                ckpt_path,
            )
            flag = "  <- best (guardado)"
        if epoch % 10 == 0 or flag:
            print(f"epoch {epoch:3d}/{args.epochs} | train_loss {train_loss:.3f} | "
                  f"val_acc {m['acc']:.3f} | val_macroF1 {m['macro_f1']:.3f}{flag}")

    print(f"\nMejor {args.select_metric} (val): {best_metric:.3f} | checkpoint: {ckpt_path}")

    # --- 3. Reporte final (val + test) ------------------------------------------
    best = torch.load(ckpt_path, weights_only=False)
    head.load_state_dict(best["state_dict"])

    if args.eval_test:
        f_test, y_test, test_ds = cached_features(
            backbone, args.data, "test", args.resolution, args.backbone, args.cache_dir, device, args.num_workers)
        test_loader = DataLoader(TensorDataset(f_test, y_test), batch_size=args.batch_size, shuffle=False)
        mt = evaluate(head, test_loader, num_classes, device)
        print("\n" + "=" * 60)
        print(f"TEST | acc {mt['acc']:.3f} | macroF1 {mt['macro_f1']:.3f} | balAcc {mt['balanced_acc']:.3f}")
        print(f"  (comparar contra SmallCNN end-to-end: acc 0.707 / macroF1 0.705)")
        print("=" * 60)
        for c, r, f in zip(classes, mt["per_class_recall"], mt["per_class_f1"]):
            print(f"  {c:14s}  recall {r:.3f}  f1 {f:.3f}")


if __name__ == "__main__":
    main()

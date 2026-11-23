"""Entrenamiento de la SmallCNN sobre los parches de daño — Fase 1.

Entrena `fase0.smallcnn.SmallCNN` (6 clases) sobre los parches extraídos por
`fase1.extract_patches`. La arquitectura ya está validada como representable en
circuito (Fase 0): solo ReLU6, AvgPool global, sin SE, cabeza de logits. Acá no se
toca la estructura — solo se aprenden los pesos.

Puntos de diseño:

- **Resolución 64 px** (elegida en Fase 0). Los parches se guardaron a tamaño nativo;
  acá se redimensionan a un cuadrado `res×res` y se normalizan con las constantes
  ImageNet (las MISMAS de `fase0/calibration.py`, para que lo que ve el modelo en
  producción coincida con el rango con que se calibró el circuito).
- **Desbalance fuerte** (scratch 2560 vs tire_flat 225): se compensa con pesos por
  clase en `CrossEntropyLoss` (inverso de frecuencia). Por eso la métrica de
  selección es **macro-F1**, no accuracy plana (que premiaría predecir siempre la
  clase mayoritaria).
- **Selección de modelo** sobre el split `val`; reporte final opcional sobre `test`.
- Augmentation liviano en train (flip horizontal, rotación chica, jitter de color);
  val/test sin augmentation.
- CPU-only (la red es minúscula). En Windows el DataLoader usa `num_workers=0` por
  defecto para evitar el costo/los problemas del spawn de procesos.

El checkpoint guarda `state_dict`, `class_to_idx` y la config, para reconstruir el
modelo y exportarlo a ONNX (siguiente paso, reusando `fase0/export.py`).
"""

from __future__ import annotations

import argparse
import json
import os

import torch
from torch import nn
from torch.utils.data import DataLoader
from torchvision import transforms
from torchvision.datasets import ImageFolder

from fase0.calibration import IMAGENET_MEAN, IMAGENET_STD
from fase0.smallcnn import SmallCNN


def build_transforms(resolution: int) -> tuple[transforms.Compose, transforms.Compose]:
    """Transforms de train (con augmentation) y de eval (determinista)."""
    normalize = transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD)
    train_tf = transforms.Compose(
        [
            transforms.Resize((resolution, resolution)),
            transforms.RandomHorizontalFlip(),
            transforms.RandomRotation(15),
            transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2),
            transforms.ToTensor(),
            normalize,
        ]
    )
    eval_tf = transforms.Compose(
        [
            transforms.Resize((resolution, resolution)),
            transforms.ToTensor(),
            normalize,
        ]
    )
    return train_tf, eval_tf


def class_weights_from_dataset(
    dataset: ImageFolder, num_classes: int, mode: str = "inverse"
) -> torch.Tensor | None:
    """Pesos por clase según `mode`, normalizados a media 1.

    - `inverse`: inverso de frecuencia. Rebalancea fuerte pero castiga la clase
      mayoritaria (baja la accuracy global).
    - `sqrt`: raíz del inverso. Rebalanceo más suave (compromiso entre global y macro).
    - `none`: sin pesos (devuelve None) → maximiza la accuracy global.
    """
    if mode == "none":
        return None
    counts = torch.zeros(num_classes)
    for _, label in dataset.samples:
        counts[label] += 1
    inv = counts.sum() / (counts.clamp(min=1) * num_classes)
    if mode == "sqrt":
        inv = inv.sqrt()
    elif mode != "inverse":
        raise ValueError(f"class-weights desconocido: {mode!r} (inverse|sqrt|none)")
    return inv / inv.mean()


@torch.no_grad()
def evaluate(model: nn.Module, loader: DataLoader, num_classes: int, device: str) -> dict:
    """Corre el modelo sobre un loader y devuelve métricas (acc, macro-F1, matriz de confusión)."""
    model.eval()
    confusion = torch.zeros(num_classes, num_classes, dtype=torch.long)  # [real, pred]
    for x, y in loader:
        x = x.to(device)
        preds = model(x).argmax(dim=1).cpu()
        for t, p in zip(y, preds):
            confusion[t, p] += 1

    correct = confusion.diag().sum().item()
    total = confusion.sum().item()
    acc = correct / total if total else 0.0

    # Precision/recall/F1 por clase a partir de la matriz de confusión.
    tp = confusion.diag().float()
    recall = tp / confusion.sum(dim=1).clamp(min=1).float()       # por fila (real)
    precision = tp / confusion.sum(dim=0).clamp(min=1).float()    # por columna (pred)
    f1 = 2 * precision * recall / (precision + recall).clamp(min=1e-9)

    return {
        "acc": acc,
        "macro_f1": f1.mean().item(),
        "balanced_acc": recall.mean().item(),
        "per_class_recall": recall.tolist(),
        "per_class_f1": f1.tolist(),
        "confusion": confusion.tolist(),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Entrena la SmallCNN sobre los parches de CarDD.")
    p.add_argument("--data", default="fase1/patches", help="Raíz con subcarpetas train/val/test (ImageFolder).")
    p.add_argument("--resolution", type=int, default=64)
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--weight-decay", type=float, default=1e-4)
    p.add_argument("--base-channels", type=int, default=8, help="Ancho de la SmallCNN (más = más capacidad y más logrows).")
    p.add_argument("--num-blocks", type=int, default=4)
    p.add_argument("--num-downsample", type=int, default=3)
    p.add_argument("--num-workers", type=int, default=0, help="DataLoader workers (0 = sin multiprocessing, seguro en Windows).")
    p.add_argument("--class-weights", choices=["inverse", "sqrt", "none"], default="inverse",
                   help="Pesos por clase en la loss. 'none' maximiza accuracy global; 'inverse' favorece macro-F1.")
    p.add_argument("--label-smoothing", type=float, default=0.0,
                   help="Label smoothing en CrossEntropy (regulariza; ayuda en clases confundibles).")
    p.add_argument("--select-metric", choices=["accuracy", "macro_f1", "balanced_acc"], default="macro_f1",
                   help="Métrica de val para elegir el mejor checkpoint.")
    p.add_argument("--tag", default="", help="Sufijo para el nombre del checkpoint (no pisar otras recetas).")
    p.add_argument("--eval-test", action="store_true", help="Evalúa el mejor modelo sobre el split test al final.")
    p.add_argument("--out", default="fase1/checkpoints", help="Carpeta donde guardar el checkpoint.")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    # Clave de `evaluate()` correspondiente a la métrica de selección.
    metric_key = {"accuracy": "acc", "macro_f1": "macro_f1", "balanced_acc": "balanced_acc"}[args.select_metric]

    torch.manual_seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    train_tf, eval_tf = build_transforms(args.resolution)
    train_ds = ImageFolder(os.path.join(args.data, "train"), transform=train_tf)
    val_ds = ImageFolder(os.path.join(args.data, "val"), transform=eval_tf)

    classes = train_ds.classes
    num_classes = len(classes)
    assert val_ds.classes == classes, "Las clases de train y val no coinciden (orden ImageFolder)."
    print(f"Clases ({num_classes}): {classes}")
    print(f"Train: {len(train_ds)} | Val: {len(val_ds)} | res {args.resolution} | device {device}")

    pin = device == "cuda"
    persistent = args.num_workers > 0
    train_loader = DataLoader(
        train_ds, batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers,
        drop_last=False, pin_memory=pin, persistent_workers=persistent,
    )
    val_loader = DataLoader(
        val_ds, batch_size=args.batch_size, shuffle=False, num_workers=args.num_workers,
        pin_memory=pin, persistent_workers=persistent,
    )

    model = SmallCNN(
        num_classes=num_classes,
        input_resolution=args.resolution,
        base_channels=args.base_channels,
        num_blocks=args.num_blocks,
        num_downsample=args.num_downsample,
    ).to(device)

    weights = class_weights_from_dataset(train_ds, num_classes, args.class_weights)
    if weights is not None:
        weights = weights.to(device)
        print(f"Pesos por clase ({args.class_weights}):", [round(w, 3) for w in weights.tolist()])
    else:
        print("Pesos por clase: none (accuracy global)")
    criterion = nn.CrossEntropyLoss(weight=weights, label_smoothing=args.label_smoothing)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
    print(f"Receta: select={args.select_metric} | label_smoothing={args.label_smoothing} | epochs={args.epochs}")

    os.makedirs(args.out, exist_ok=True)
    suffix = f"_{args.tag}" if args.tag else ""
    ckpt_path = os.path.join(args.out, f"smallcnn_r{args.resolution}_bc{args.base_channels}{suffix}.pt")
    best_metric = -1.0

    for epoch in range(1, args.epochs + 1):
        model.train()
        running = 0.0
        for x, y in train_loader:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad()
            loss = criterion(model(x), y)
            loss.backward()
            optimizer.step()
            running += loss.item() * x.size(0)
        scheduler.step()
        train_loss = running / len(train_ds)

        m = evaluate(model, val_loader, num_classes, device)
        flag = ""
        if m[metric_key] > best_metric:
            best_metric = m[metric_key]
            torch.save(
                {
                    "state_dict": model.state_dict(),
                    "class_to_idx": train_ds.class_to_idx,
                    "classes": classes,
                    "config": {
                        "num_classes": num_classes,
                        "input_resolution": args.resolution,
                        "base_channels": args.base_channels,
                        "num_blocks": args.num_blocks,
                        "num_downsample": args.num_downsample,
                    },
                    "val_metrics": m,
                },
                ckpt_path,
            )
            flag = "  <- best (guardado)"
        print(
            f"epoch {epoch:2d}/{args.epochs} | train_loss {train_loss:.3f} | "
            f"val_acc {m['acc']:.3f} | val_macroF1 {m['macro_f1']:.3f} | "
            f"val_balAcc {m['balanced_acc']:.3f}{flag}"
        )

    print(f"\nMejor {args.select_metric} (val): {best_metric:.3f} | checkpoint: {ckpt_path}")

    # Reporte por clase del mejor modelo sobre val.
    best = torch.load(ckpt_path, weights_only=False)
    model.load_state_dict(best["state_dict"])
    m = evaluate(model, val_loader, num_classes, device)
    print("\nRecall por clase (val):")
    for c, r, f in zip(classes, m["per_class_recall"], m["per_class_f1"]):
        print(f"  {c:14s}  recall {r:.3f}  f1 {f:.3f}")

    if args.eval_test:
        test_ds = ImageFolder(os.path.join(args.data, "test"), transform=eval_tf)
        test_loader = DataLoader(test_ds, batch_size=args.batch_size, shuffle=False, num_workers=args.num_workers)
        mt = evaluate(model, test_loader, num_classes, device)
        print(f"\nTEST | acc {mt['acc']:.3f} | macroF1 {mt['macro_f1']:.3f} | balAcc {mt['balanced_acc']:.3f}")
        for c, r, f in zip(classes, mt["per_class_recall"], mt["per_class_f1"]):
            print(f"  {c:14s}  recall {r:.3f}  f1 {f:.3f}")


if __name__ == "__main__":
    main()

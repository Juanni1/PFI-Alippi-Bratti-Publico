"""Fine-tuning del backbone (features-afuera) — ¿sube el techo de 87.5%?

El prototipo `features_afuera.py` congela el backbone y entrena solo la cabeza sobre
features fijas (rápido, 87.5%). Este script **descongela el backbone** y lo entrena
junto con la cabeza sobre las imágenes reales: las features dejan de ser genéricas de
ImageNet y se adaptan a daños de autos → normalmente más accuracy.

Clave para el zk: fine-tunear **no cambia el costo del circuito**. El backbone sigue
corriendo AFUERA de la prueba; lo único que se probaría sigue siendo la **cabeza**
(`Linear` 576→6, logrows 15). O sea, la accuracy que gane esto es "gratis" del lado
criptográfico.

Costo real: hay que backpropagar por todo el backbone (ya no sirve cachear features) →
más lento, y con solo ~6k parches hay **riesgo de overfitting** (se mitiga con LR chico,
augmentation, y opcionalmente descongelar solo las últimas capas: `--unfreeze last`).

    imagen → [ backbone mobilenet_v3_small ] → 576 features → [ Linear 576→6 ] → logits
             AHORA TAMBIÉN se entrena                          la cabeza a probar

Este script mide el **techo con fine-tuning**; no genera pruebas (para eso, después:
re-extraer features del backbone fine-tuneado + `prove_head.py`).

Ejecutar:
  uv run python -m fase1.finetune_afuera --eval-test
  uv run python -m fase1.finetune_afuera --unfreeze last --epochs 25 --eval-test
"""

from __future__ import annotations

import argparse
import os
import time

import torch
from torch import nn
from torch.utils.data import DataLoader
from torchvision import models, transforms
from torchvision.datasets import ImageFolder

from fase0.calibration import IMAGENET_MEAN, IMAGENET_STD
from fase1.train import class_weights_from_dataset, evaluate


def build_finetunable_model(num_classes: int, unfreeze: str) -> tuple[nn.Module, nn.Module]:
    """mobilenet_v3_small pre-entrenado + cabeza Linear(576→num_classes), entrenable.

    Devuelve (modelo_completo, cabeza). `unfreeze`:
      - 'all'  : todo el backbone entrenable (mejor si no overfitea).
      - 'last' : congela el backbone salvo su último bloque + la cabeza.
      - 'head' : backbone congelado (equivale al prototipo, para comparar).
    """
    m = models.mobilenet_v3_small(weights=models.MobileNet_V3_Small_Weights.IMAGENET1K_V1)
    backbone = nn.Sequential(m.features, nn.AdaptiveAvgPool2d(1), nn.Flatten())
    head = nn.Linear(576, num_classes)
    model = nn.Sequential(backbone, head)

    if unfreeze == "head":
        for p in backbone.parameters():
            p.requires_grad_(False)
    elif unfreeze == "last":
        for p in backbone.parameters():
            p.requires_grad_(False)
        # descongela el último bloque de features (m.features[-1]) + la cabeza
        for p in m.features[-1].parameters():
            p.requires_grad_(True)
    elif unfreeze != "all":
        raise ValueError(f"unfreeze desconocido: {unfreeze!r} (all|last|head)")
    return model, head


def build_transforms() -> tuple[transforms.Compose, transforms.Compose]:
    """Train (con augmentation) y eval (determinista), a 224 px (nativo del backbone)."""
    normalize = transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD)
    train_tf = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.RandomHorizontalFlip(),
        transforms.RandomRotation(15),
        transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2),
        transforms.ToTensor(),
        normalize,
    ])
    eval_tf = transforms.Compose([
        transforms.Resize((224, 224)),
        transforms.ToTensor(),
        normalize,
    ])
    return train_tf, eval_tf


def main() -> None:
    p = argparse.ArgumentParser(description="Fine-tuning del backbone (features-afuera).")
    p.add_argument("--data", default="fase1/patches")
    p.add_argument("--unfreeze", choices=["all", "last", "head"], default="all")
    p.add_argument("--epochs", type=int, default=20)
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--lr", type=float, default=1e-4, help="LR chico (fine-tuning).")
    p.add_argument("--weight-decay", type=float, default=1e-4)
    p.add_argument("--num-workers", type=int, default=8)
    p.add_argument("--class-weights", choices=["inverse", "sqrt", "none"], default="none")
    p.add_argument("--label-smoothing", type=float, default=0.1)
    p.add_argument("--out", default="fase1/checkpoints")
    p.add_argument("--eval-test", action="store_true")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    torch.manual_seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    train_tf, eval_tf = build_transforms()
    train_ds = ImageFolder(os.path.join(args.data, "train"), transform=train_tf)
    val_ds = ImageFolder(os.path.join(args.data, "val"), transform=eval_tf)
    classes = train_ds.classes
    num_classes = len(classes)
    assert val_ds.classes == classes

    model, head = build_finetunable_model(num_classes, args.unfreeze)
    model = model.to(device)
    n_train = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"Fine-tuning mobilenet_v3_small | unfreeze={args.unfreeze} | "
          f"params entrenables {n_train:,} | device {device}")
    print(f"Comparar contra: features-afuera congelado (0.875) y SmallCNN (0.707)")

    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True,
                              num_workers=args.num_workers, pin_memory=(device == "cuda"),
                              persistent_workers=args.num_workers > 0)
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False,
                            num_workers=args.num_workers, pin_memory=(device == "cuda"),
                            persistent_workers=args.num_workers > 0)

    weights = class_weights_from_dataset(train_ds, num_classes, args.class_weights)
    if weights is not None:
        weights = weights.to(device)
    criterion = nn.CrossEntropyLoss(weight=weights, label_smoothing=args.label_smoothing)
    optimizer = torch.optim.AdamW(
        [p for p in model.parameters() if p.requires_grad], lr=args.lr, weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)

    os.makedirs(args.out, exist_ok=True)
    ckpt_path = os.path.join(args.out, f"finetune_mnv3_{args.unfreeze}.pt")
    best_acc = -1.0

    for epoch in range(1, args.epochs + 1):
        model.train()
        running = 0.0
        t0 = time.time()
        for x, y in train_loader:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad()
            loss = criterion(model(x), y)
            loss.backward()
            optimizer.step()
            running += loss.item() * x.size(0)
        scheduler.step()
        m = evaluate(model, val_loader, num_classes, device)
        flag = ""
        if m["acc"] > best_acc:
            best_acc = m["acc"]
            torch.save({
                "model_state_dict": model.state_dict(),
                "head_state_dict": head.state_dict(),
                "classes": classes, "class_to_idx": train_ds.class_to_idx,
                "config": {"backbone": "mobilenet_v3_small", "resolution": 224,
                           "feat_dim": 576, "hidden": 0, "num_classes": num_classes,
                           "unfreeze": args.unfreeze},
                "val_metrics": m,
            }, ckpt_path)
            flag = "  <- best"
        print(f"epoch {epoch:2d}/{args.epochs} | loss {running/len(train_ds):.3f} | "
              f"val_acc {m['acc']:.3f} | val_macroF1 {m['macro_f1']:.3f} | {time.time()-t0:.0f}s{flag}")

    print(f"\nMejor val_acc: {best_acc:.3f} | checkpoint: {ckpt_path}")

    if args.eval_test:
        best = torch.load(ckpt_path, weights_only=False)
        model.load_state_dict(best["model_state_dict"])
        test_ds = ImageFolder(os.path.join(args.data, "test"), transform=eval_tf)
        test_loader = DataLoader(test_ds, batch_size=args.batch_size, shuffle=False, num_workers=args.num_workers)
        mt = evaluate(model, test_loader, num_classes, device)
        print("\n" + "=" * 60)
        print(f"TEST | acc {mt['acc']:.3f} | macroF1 {mt['macro_f1']:.3f} | balAcc {mt['balanced_acc']:.3f}")
        print(f"  (congelado: 0.875 | SmallCNN: 0.707)")
        print("=" * 60)
        for c, r, f in zip(classes, mt["per_class_recall"], mt["per_class_f1"]):
            print(f"  {c:14s}  recall {r:.3f}  f1 {f:.3f}")


if __name__ == "__main__":
    main()

"""Inversión de features — control de RNF-25.

En features-afuera el vector de 576 activaciones de la penúltima capa viaja como **entrada
pública** del circuito: queda en la *calldata* y en la cadena para siempre. RNF-25 exige que
ninguna entrada pública permita reconstruir la imagen del siniestro. La literatura de
inversión de representaciones muestra que las capas profundas admiten reconstrucciones
parciales, pero para este extractor y este dataset está sin medir; hasta medirlo, la
arquitectura tiene un incumplimiento potencial que el circuito completo no tiene.

Protocolo: se entrena un *decoder* sobre **train** que mapea features → parche y se lo evalúa
sobre **test**. El decoder es deliberadamente chico: la pregunta es si la información está en
el vector, no cuán buena puede ser la red que la extrae.

**El número que decide no es el PSNR absoluto sino la distancia contra el piso.** Un predictor
que ignora las features y emite siempre la imagen media del entrenamiento ya consigue un PSNR
respetable, porque todos los parches de daño de auto se parecen entre sí: son texturas de
chapa con una lesión encima. Sin ese control, cualquier reconstrucción borrosa se lee como
fuga de información cuando en realidad no lo es. El script mide los dos y reporta la ganancia.

El emparejamiento (vector, imagen) se apoya en que las cachés se generan con
`DataLoader(..., shuffle=False)` sobre un `ImageFolder`, tanto en `features_afuera.py` como en
`prep_finetuned_proof.py`: la fila *i* de la caché es `ds.samples[i]`. No se asume, se verifica
contra `ds.targets` y contra la lista de clases, y se aborta si no coinciden.

Correrlo sobre **las dos** cachés. El backbone fine-tuneado adapta sus features al dominio de
los daños, así que podría filtrar más que el congelado de ImageNet; esa comparación es parte
del resultado.

Ejecutar:
  uv run python -m fase1.invert_features --cache-tag mobilenet_v3_small
  uv run python -m fase1.invert_features --cache-tag mnv3_ft
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time

import torch
from PIL import Image
from torch import nn
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms
from torchvision.datasets import ImageFolder

from fase1.export_backbone import sample_indices


class FeatureImageDataset(Dataset):
    """Pares (vector de features, parche) — el vínculo es el orden del ImageFolder."""

    def __init__(self, data_root: str, split: str, cache_path: str, image_size: int):
        blob = torch.load(cache_path, map_location="cpu")
        self.features, labels, classes = blob["features"], blob["labels"], blob["classes"]
        self.ds = ImageFolder(
            os.path.join(data_root, split),
            transform=transforms.Compose([
                transforms.Resize((image_size, image_size)),
                transforms.ToTensor(),  # [0, 1]: el espacio en el que se mide PSNR
            ]),
        )
        if len(self.ds) != self.features.size(0):
            raise SystemExit(
                f"La caché {cache_path} tiene {self.features.size(0)} vectores y el split "
                f"'{split}' {len(self.ds)} imágenes. Se generaron sobre datasets distintos."
            )
        if self.ds.classes != classes:
            raise SystemExit(f"Clases distintas entre la caché y {split}: {classes} vs {self.ds.classes}")
        if not torch.equal(torch.tensor(self.ds.targets), labels):
            raise SystemExit(
                "El orden de la caché no coincide con el del ImageFolder: las etiquetas difieren. "
                "Sin ese emparejamiento la inversión no mide nada."
            )

    def __len__(self) -> int:
        return len(self.ds)

    def __getitem__(self, i: int):
        img, _ = self.ds[i]
        return self.features[i], img


def up_block(cin: int, cout: int) -> nn.Sequential:
    return nn.Sequential(
        nn.Upsample(scale_factor=2, mode="nearest"),
        nn.Conv2d(cin, cout, 3, padding=1),
        nn.BatchNorm2d(cout),
        nn.ReLU(inplace=True),
    )


class Decoder(nn.Module):
    """features -> 4x4 -> upsampling hasta image_size -> RGB en [0, 1]."""

    def __init__(self, feat_dim: int, image_size: int, base: int = 256):
        super().__init__()
        self.base = base
        self.fc = nn.Linear(feat_dim, base * 4 * 4)
        n_up = int(math.log2(image_size // 4))
        chans = [base // (2 ** i) for i in range(n_up + 1)]
        self.net = nn.Sequential(
            *[up_block(chans[i], chans[i + 1]) for i in range(n_up)],
            nn.Conv2d(chans[-1], 3, 3, padding=1),
            nn.Sigmoid(),
        )

    def forward(self, f: torch.Tensor) -> torch.Tensor:
        return self.net(self.fc(f).view(-1, self.base, 4, 4))


def psnr_from_mse(mse: float) -> float:
    return float("inf") if mse <= 0 else 10 * math.log10(1.0 / mse)


@torch.no_grad()
def evaluate(predict, loader: DataLoader, device: str) -> dict:
    """MSE global (no promedio de PSNR por lote) y L1 medio sobre todo el split."""
    sq = abs_err = total = 0.0
    for f, img in loader:
        f, img = f.to(device), img.to(device)
        pred = predict(f, img.shape)
        sq += ((pred - img) ** 2).sum().item()
        abs_err += (pred - img).abs().sum().item()
        total += img.numel()
    mse = sq / total
    return {"psnr_db": psnr_from_mse(mse), "l1": abs_err / total, "mse": mse}


def save_panel(originals: torch.Tensor, recons: torch.Tensor, floor: torch.Tensor, path: str) -> None:
    """Tres filas: parche real, reconstrucción del decoder, piso de la imagen media."""
    rows = [originals, recons, floor.expand_as(originals)]
    grid = torch.cat([torch.cat(list(r), dim=2) for r in rows], dim=1)
    arr = (grid.clamp(0, 1) * 255).byte().permute(1, 2, 0).cpu().numpy()
    Image.fromarray(arr).save(path)


def main() -> None:
    p = argparse.ArgumentParser(description="Inversión de features: ¿se reconstruye el parche?")
    p.add_argument("--cache-tag", default="mobilenet_v3_small",
                   help="'mobilenet_v3_small' (congelado) o 'mnv3_ft' (fine-tuneado).")
    p.add_argument("--cache-dir", default="fase1/feat_cache")
    p.add_argument("--data", default="fase1/patches")
    p.add_argument("--resolution", type=int, default=224, help="La del nombre de la caché, no la del decoder.")
    p.add_argument("--image-size", type=int, default=64, help="Resolución de reconstrucción.")
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--batch-size", type=int, default=64)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--num-workers", type=int, default=8)
    p.add_argument("--panel", type=int, default=8, help="Parches del PNG comparativo.")
    p.add_argument("--out-dir", default="fase1", help="fase1/ y no artifacts*/: la evidencia se commitea.")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    torch.manual_seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    def cache_of(split: str) -> str:
        path = os.path.join(args.cache_dir, f"{args.cache_tag}_r{args.resolution}_{split}.pt")
        if not os.path.exists(path):
            raise SystemExit(
                f"Falta la caché {path}.\n"
                "  congelado    -> uv run python -m fase1.features_afuera --eval-test\n"
                "  fine-tuneado -> uv run python -m fase1.prep_finetuned_proof --splits train test\n"
                "  (el default de prep_finetuned_proof extrae solo 'test'; acá hace falta 'train')"
            )
        return path

    train_ds = FeatureImageDataset(args.data, "train", cache_of("train"), args.image_size)
    test_ds = FeatureImageDataset(args.data, "test", cache_of("test"), args.image_size)
    feat_dim = train_ds.features.size(1)
    print(f"Inversión de '{args.cache_tag}' | feat_dim {feat_dim} | reconstrucción "
          f"{args.image_size}x{args.image_size} | train {len(train_ds)} / test {len(test_ds)} | {device}")

    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True,
                              num_workers=args.num_workers, persistent_workers=args.num_workers > 0)
    test_loader = DataLoader(test_ds, batch_size=args.batch_size, shuffle=False,
                             num_workers=args.num_workers, persistent_workers=args.num_workers > 0)

    # --- Piso: la imagen media del entrenamiento, que ignora por completo las features ------
    print("Calculando el piso (imagen media de train)...")
    acc = torch.zeros(3, args.image_size, args.image_size)
    for _, img in DataLoader(train_ds, batch_size=args.batch_size, num_workers=args.num_workers):
        acc += img.sum(dim=0)
    mean_image = (acc / len(train_ds)).to(device)
    floor = evaluate(lambda f, shape: mean_image.expand(shape), test_loader, device)
    print(f"  piso: PSNR {floor['psnr_db']:.2f} dB | L1 {floor['l1']:.4f}")

    # --- Decoder ----------------------------------------------------------------------------
    decoder = Decoder(feat_dim, args.image_size).to(device)
    n_params = sum(q.numel() for q in decoder.parameters())
    optimizer = torch.optim.AdamW(decoder.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
    criterion = nn.L1Loss()
    print(f"\nDecoder: {n_params:,} parámetros | {args.epochs} épocas")

    for epoch in range(1, args.epochs + 1):
        decoder.train()
        running = 0.0
        t0 = time.time()
        for f, img in train_loader:
            f, img = f.to(device), img.to(device)
            optimizer.zero_grad()
            loss = criterion(decoder(f), img)
            loss.backward()
            optimizer.step()
            running += loss.item() * f.size(0)
        scheduler.step()
        print(f"  época {epoch:2d}/{args.epochs} | L1 train {running / len(train_ds):.4f} | {time.time() - t0:.0f}s")

    decoder.eval()
    res = evaluate(lambda f, shape: decoder(f), test_loader, device)

    # --- Panel visual -------------------------------------------------------------------------
    idxs = sample_indices(len(test_ds), args.panel)
    originals = torch.stack([test_ds[i][1] for i in idxs]).to(device)
    with torch.no_grad():
        recons = decoder(torch.stack([test_ds[i][0] for i in idxs]).to(device))
    os.makedirs(args.out_dir, exist_ok=True)
    png_path = os.path.join(args.out_dir, f"inversion_{args.cache_tag}.png")
    save_panel(originals, recons, mean_image.unsqueeze(0), png_path)

    ganancia = res["psnr_db"] - floor["psnr_db"]
    out = {
        "cache_tag": args.cache_tag, "feat_dim": feat_dim, "image_size": args.image_size,
        "epochs": args.epochs, "decoder_params": n_params, "seed": args.seed,
        "test_size": len(test_ds), "decoder": res, "piso_imagen_media": floor,
        "ganancia_psnr_db": ganancia, "panel": png_path,
    }
    json_path = os.path.join(args.out_dir, f"inversion_{args.cache_tag}.json")
    with open(json_path, "w") as f:
        json.dump(out, f, indent=2)

    print("\n" + "=" * 60)
    print(f"INVERSIÓN DE FEATURES — control de RNF-25 ({args.cache_tag})")
    print("=" * 60)
    print(f"  decoder            : PSNR {res['psnr_db']:.2f} dB | L1 {res['l1']:.4f}")
    print(f"  piso (imagen media): PSNR {floor['psnr_db']:.2f} dB | L1 {floor['l1']:.4f}")
    print(f"  ganancia sobre el piso : {ganancia:+.2f} dB")
    print(f"  panel  : {png_path}  (fila 1 real, fila 2 decoder, fila 3 piso)")
    print(f"  métricas: {json_path}")
    print("\n  Cómo se lee: la ganancia sobre el piso es la información que aportan las")
    print("  features, y el panel dice de qué tipo. Ganancia chica y filas 2 y 3 parecidas")
    print("  entre sí sostienen RNF-25. Si en la fila 2 se distingue la lesión concreta de")
    print("  cada parche, la entrada pública filtra la evidencia y RNF-25 no se cumple.")
    print("  El veredicto lo fija la bóveda contra el enunciado del requerimiento, no este script.")


if __name__ == "__main__":
    main()

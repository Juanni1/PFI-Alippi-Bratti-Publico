"""Extracción de parches de daño desde CarDD_COCO — primer paso de Fase 1.

El circuito zkML solo prueba la **clasificación** del tipo de daño sobre un parche
ya recortado; la **localización** (encontrar el bbox) queda fuera del circuito. Por
eso el dataset de entrenamiento no son las imágenes completas sino los parches: un
recorte por cada instancia anotada (bbox) de CarDD_COCO, etiquetado con su clase.

Decisiones de diseño (todas re-ejecutables vía flags):

- **Se guarda el recorte sin fijar la resolución final.** En vez de redimensionar a
  64 px al guardar, se guarda el parche a tamaño nativo con un tope de lado máximo
  (`--max-side`, solo downscale, nunca upscale). Así el mismo dataset sirve para
  entrenar a 48 / 64 / 96 px sin re-extraer: el resize + normalización ImageNet va
  en el `transform` del entrenamiento, no acá. (La resolución elegida en Fase 0 es
  64 px; 48 piso, 96 con VM.)
- **Margen de contexto** (`--margin`): expande el bbox un porcentaje antes de
  recortar, para dar algo de contexto alrededor del daño. 0 = bbox justo.
- **Layout estilo `torchvision.ImageFolder`**: `out/{split}/{clase}/{imgid}_{annid}.jpg`,
  lo que permite cargar el dataset con un `ImageFolder` directo en el entrenamiento.
- Se emite además un **manifest CSV** con una fila por parche (split, clase, ids,
  ruta, tamaño de bbox y tamaño guardado) para auditar el dataset y el desbalance.

CarDD_COCO usa formato COCO: bbox = [x, y, w, h] en píxeles; cada anotación referencia
una imagen (`image_id`) y una categoría (`category_id`). Las 6 clases son
dent, scratch, crack, glass shatter, lamp broken, tire flat (fuerte desbalance —
compensar al entrenar, no acá).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter

from PIL import Image

# Los tres splits que trae CarDD_COCO: (nombre de split, carpeta de imágenes, json de anotaciones).
SPLITS = [
    ("train", "train2017", "instances_train2017.json"),
    ("val", "val2017", "instances_val2017.json"),
    ("test", "test2017", "instances_test2017.json"),
]


def _safe_class_name(name: str) -> str:
    """Nombre de clase apto para carpeta: espacios → guion bajo (glass shatter → glass_shatter)."""
    return name.strip().replace(" ", "_")


def _expand_bbox(
    x: float, y: float, w: float, h: float, margin: float, img_w: int, img_h: int
) -> tuple[int, int, int, int]:
    """Expande el bbox [x,y,w,h] un `margin` relativo y lo recorta a los límites de la imagen.

    Devuelve (left, top, right, bottom) enteros listos para `Image.crop`.
    """
    dx = w * margin
    dy = h * margin
    left = max(0, int(round(x - dx)))
    top = max(0, int(round(y - dy)))
    right = min(img_w, int(round(x + w + dx)))
    bottom = min(img_h, int(round(y + h + dy)))
    return left, top, right, bottom


def _downscale_to_max_side(img: Image.Image, max_side: int) -> Image.Image:
    """Reduce el parche para que su lado mayor no exceda `max_side`. Nunca agranda."""
    if max_side <= 0:
        return img
    w, h = img.size
    long_side = max(w, h)
    if long_side <= max_side:
        return img
    scale = max_side / long_side
    new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return img.resize(new_size, Image.BILINEAR)


def extract_split(
    split_name: str,
    images_dir: str,
    annotations_path: str,
    out_root: str,
    margin: float,
    max_side: int,
    min_bbox_side: int,
) -> list[dict]:
    """Recorta y guarda todos los parches de un split. Devuelve filas para el manifest."""
    with open(annotations_path, encoding="utf-8") as f:
        coco = json.load(f)

    id_to_name = {c["id"]: c["name"] for c in coco["categories"]}
    img_meta = {im["id"]: im for im in coco["images"]}

    # Agrupar anotaciones por imagen para abrir cada archivo una sola vez.
    anns_by_image: dict[int, list[dict]] = {}
    for ann in coco["annotations"]:
        anns_by_image.setdefault(ann["image_id"], []).append(ann)

    rows: list[dict] = []
    skipped_degenerate = 0
    skipped_missing_img = 0

    for image_id, anns in anns_by_image.items():
        meta = img_meta.get(image_id)
        if meta is None:
            skipped_missing_img += len(anns)
            continue
        img_path = os.path.join(images_dir, meta["file_name"])
        if not os.path.exists(img_path):
            skipped_missing_img += len(anns)
            continue

        with Image.open(img_path) as im:
            im = im.convert("RGB")
            img_w, img_h = im.size
            for ann in anns:
                x, y, w, h = ann["bbox"]
                if w < min_bbox_side or h < min_bbox_side:
                    skipped_degenerate += 1
                    continue
                left, top, right, bottom = _expand_bbox(x, y, w, h, margin, img_w, img_h)
                if right - left < 1 or bottom - top < 1:
                    skipped_degenerate += 1
                    continue

                cls = _safe_class_name(id_to_name[ann["category_id"]])
                patch = _downscale_to_max_side(im.crop((left, top, right, bottom)), max_side)

                cls_dir = os.path.join(out_root, split_name, cls)
                os.makedirs(cls_dir, exist_ok=True)
                fname = f"{image_id:06d}_{ann['id']}.jpg"
                patch.save(os.path.join(cls_dir, fname), quality=95)

                sw, sh = patch.size
                rows.append(
                    {
                        "split": split_name,
                        "class": cls,
                        "image_id": image_id,
                        "ann_id": ann["id"],
                        "path": os.path.join(split_name, cls, fname).replace("\\", "/"),
                        "bbox_w": round(w, 1),
                        "bbox_h": round(h, 1),
                        "bbox_area": round(w * h, 1),
                        "saved_w": sw,
                        "saved_h": sh,
                    }
                )

    dist = Counter(r["class"] for r in rows)
    print(f"[{split_name}] {len(rows)} parches | distribución: {dict(sorted(dist.items()))}")
    if skipped_degenerate:
        print(f"[{split_name}] saltados por bbox < {min_bbox_side}px: {skipped_degenerate}")
    if skipped_missing_img:
        print(f"[{split_name}] saltados por imagen faltante: {skipped_missing_img}")
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description="Extrae parches de daño de CarDD_COCO para Fase 1.")
    p.add_argument(
        "--coco-root",
        default="CarDD_release/CarDD_COCO",
        help="Raíz del dataset COCO (contiene annotations/, train2017/, val2017/, test2017/).",
    )
    p.add_argument(
        "--out",
        default="fase1/patches",
        help="Carpeta de salida (layout ImageFolder por split y clase).",
    )
    p.add_argument(
        "--margin",
        type=float,
        default=0.15,
        help="Expansión relativa del bbox para dar contexto (0 = bbox justo). Default 0.15.",
    )
    p.add_argument(
        "--max-side",
        type=int,
        default=256,
        help="Tope de lado mayor del parche guardado, solo downscale (0 = sin tope). Default 256.",
    )
    p.add_argument(
        "--min-bbox-side",
        type=int,
        default=4,
        help="Descarta bboxes con ancho o alto menor a este valor en px. Default 4.",
    )
    p.add_argument(
        "--splits",
        nargs="+",
        default=["train", "val", "test"],
        choices=["train", "val", "test"],
        help="Qué splits extraer.",
    )
    args = p.parse_args()

    ann_dir = os.path.join(args.coco_root, "annotations")
    all_rows: list[dict] = []
    for split_name, img_folder, ann_file in SPLITS:
        if split_name not in args.splits:
            continue
        rows = extract_split(
            split_name=split_name,
            images_dir=os.path.join(args.coco_root, img_folder),
            annotations_path=os.path.join(ann_dir, ann_file),
            out_root=args.out,
            margin=args.margin,
            max_side=args.max_side,
            min_bbox_side=args.min_bbox_side,
        )
        all_rows.extend(rows)

    os.makedirs(args.out, exist_ok=True)
    manifest_path = os.path.join(args.out, "manifest.csv")
    with open(manifest_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "split", "class", "image_id", "ann_id", "path",
                "bbox_w", "bbox_h", "bbox_area", "saved_w", "saved_h",
            ],
        )
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nTotal: {len(all_rows)} parches -> {args.out}")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()

"""Export del backbone fine-tuneado a ONNX + control de reproducibilidad bit a bit.

Precondición de la medición 3 del criterio de cierre de la arquitectura features-afuera:
el tramo píxel → *features* no se prueba con zk, se **replica**. Un tercero descarga la
imagen por su CID, corre el extractor declarado y compara el vector resultante contra la
entrada pública de la prueba. Esa comparación exige igualdad **exacta**, y ONNX Runtime
no garantiza por construcción resultados idénticos entre plataformas, versiones y
proveedores de ejecución. Mientras eso no se mida, el tramo replicable es una hipótesis.

Dos modos:

  --mode export   checkpoint de `finetune_afuera` -> ONNX del backbone + sha256 del archivo.
  --mode digest   ONNX + parches -> vectores de features + sha256 de sus bytes crudos.

El control se corre así: `export` una vez, `digest` en **dos** plataformas distintas
(mismo .onnx, mismos parches), y se comparan los dos sha256. Si difieren, `--compare`
sobre el .npy de la otra plataforma localiza en qué elementos y por cuánto.

El digest cubre el camino completo — decodificar el JPEG, redimensionar, normalizar y
correr el grafo — y no solo la inferencia, porque eso es lo que replica el auditor: parte
del archivo direccionado por CID, no de un tensor ya preprocesado. Pillow y numpy entran
en la superficie de comparación tanto como el proveedor de ejecución.

Se validan opset y formas estáticas del grafo, pero **no** se corre
`assert_onnx_circuit_safe`: el backbone se ejecuta afuera de la prueba, así que las
operaciones que el circuito no representa (Hardswish y las de los bloques SE de
mobilenet_v3) son legítimas acá. Aplicarle ese control sería importar una restricción
que no rige en este tramo.

Ejecutar:
  uv run python -m fase1.export_backbone --mode export
  uv run python -m fase1.export_backbone --mode digest
  uv run python -m fase1.export_backbone --mode digest --compare artifacts_head/features_digest_otra_pc.npy

`onnxruntime` no está en las dependencias del proyecto (torch alcanzaba hasta acá).
Para el modo digest: `uv add onnxruntime`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform

import numpy as np
import onnx
import torch
from torchvision.datasets import ImageFolder

from fase0.export import assert_opset, assert_static_shapes
from fase1.finetune_afuera import build_finetunable_model, build_transforms

RESOLUTION = 224  # nativa del backbone; la misma que usa build_transforms()
FEAT_DIM = 576  # mobilenet_v3_small


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_backbone(ckpt_path: str) -> tuple[torch.nn.Module, dict]:
    """Reconstruye el modelo fine-tuneado y devuelve solo su backbone (sin la cabeza)."""
    ck = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    cfg = ck["config"]
    assert cfg["backbone"] == "mobilenet_v3_small" and cfg["feat_dim"] == FEAT_DIM, \
        f"checkpoint inesperado: {cfg}"
    model, _ = build_finetunable_model(cfg["num_classes"], cfg.get("unfreeze", "all"))
    model.load_state_dict(ck["model_state_dict"])
    model.eval()
    return model[0], ck  # nn.Sequential(features, AdaptiveAvgPool2d(1), Flatten())


def export_backbone_onnx(backbone: torch.nn.Module, onnx_path: str) -> str:
    """Exporta el backbone a ONNX opset 13, batch fijo 1 y dimensiones estáticas."""
    os.makedirs(os.path.dirname(os.path.abspath(onnx_path)), exist_ok=True)
    dummy = torch.randn(1, 3, RESOLUTION, RESOLUTION)
    torch.onnx.export(
        backbone, dummy, onnx_path,
        opset_version=13, do_constant_folding=True,
        input_names=["image"], output_names=["features"], dynamo=False,
    )
    return onnx_path


def sample_indices(total: int, num: int) -> list[int]:
    """Índices repartidos por todo el dataset, no los primeros N (que serían de una clase)."""
    if num >= total:
        return list(range(total))
    stride = total / num
    return [int(i * stride) for i in range(num)]


def run_digest(onnx_path: str, data_dir: str, split: str, num: int) -> tuple[np.ndarray, dict]:
    """Corre el ONNX sobre `num` parches del split y devuelve (features, metadatos de entorno)."""
    try:
        import onnxruntime as ort
    except ModuleNotFoundError:
        raise SystemExit(
            "Falta onnxruntime (no está en las dependencias del proyecto).\n"
            "  uv add onnxruntime"
        )

    _, eval_tf = build_transforms()
    ds = ImageFolder(os.path.join(data_dir, split), transform=eval_tf)
    idxs = sample_indices(len(ds), num)

    sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    input_name = sess.get_inputs()[0].name

    out = np.empty((len(idxs), FEAT_DIM), dtype=np.float32)
    for row, i in enumerate(idxs):
        x, _ = ds[i]
        vec = sess.run(None, {input_name: x.unsqueeze(0).numpy()})[0]
        out[row] = vec.reshape(-1)

    env = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "onnxruntime": ort.__version__,
        "providers": sess.get_providers(),
        "numpy": np.__version__,
        "torch": torch.__version__,
        "split": split,
        "indices": idxs,
        "dataset_size": len(ds),
    }
    return out, env


def compare(actual: np.ndarray, other_path: str) -> None:
    """Contrasta contra el .npy de otra plataforma y localiza las diferencias."""
    other = np.load(other_path)
    if other.shape != actual.shape:
        print(f"  formas distintas: {actual.shape} vs {other.shape} — no son comparables")
        return
    identicos = actual.tobytes() == other.tobytes()
    print(f"\n  contra {other_path}")
    print(f"  idénticos bit a bit : {identicos}")
    if identicos:
        return
    difieren = int((actual != other).sum())
    max_abs = float(np.abs(actual.astype(np.float64) - other.astype(np.float64)).max())
    print(f"  elementos distintos : {difieren:,} de {actual.size:,} ({100 * difieren / actual.size:.3f} %)")
    print(f"  diferencia máxima   : {max_abs:.3e}")
    print("  -> el tramo replicable necesitaría una tolerancia, y una tolerancia es una puerta")


def main() -> None:
    p = argparse.ArgumentParser(description="Backbone fine-tuneado a ONNX + control de reproducibilidad.")
    p.add_argument("--mode", choices=["export", "digest"], default="export")
    p.add_argument("--ckpt", default="fase1/checkpoints/finetune_mnv3_all.pt",
                   help="Checkpoint de fase1.finetune_afuera (modo export).")
    p.add_argument("--onnx", default="artifacts_head/backbone_mnv3_ft.onnx")
    p.add_argument("--data", default="fase1/patches")
    p.add_argument("--split", default="test", choices=["train", "val", "test"])
    p.add_argument("--num", type=int, default=64, help="Parches del digest (modo digest).")
    p.add_argument("--out-dir", default="fase1",
                   help="Salida del digest. Por defecto fase1/ y no artifacts_head/, que está "
                        "gitignoreado: el .npy y el .json se comparan entre máquinas y se commitean.")
    p.add_argument("--tag", default=None, help="Sufijo del .npy y el .json del digest (default: hostname).")
    p.add_argument("--compare", default=None, help="Ruta a un .npy de otra plataforma (modo digest).")
    args = p.parse_args()

    if args.mode == "export":
        backbone, ck = load_backbone(args.ckpt)
        export_backbone_onnx(backbone, args.onnx)
        model = onnx.load(args.onnx)
        assert_opset(model)
        assert_static_shapes(model)
        digest = sha256_file(args.onnx)

        print("\n" + "=" * 60)
        print("BACKBONE FINE-TUNEADO EXPORTADO")
        print("=" * 60)
        print(f"  checkpoint : {args.ckpt} (unfreeze={ck['config'].get('unfreeze')}, "
              f"val_acc {ck['val_metrics']['acc']:.3f})")
        print(f"  onnx       : {args.onnx} ({os.path.getsize(args.onnx) / 1024:.1f} KB)")
        print(f"  entrada    : image [1, 3, {RESOLUTION}, {RESOLUTION}] -> features [1, {FEAT_DIM}]")
        print(f"  sha256     : {digest}")
        print("\n  Es el hash que se publica junto al ONNX: fija qué extractor hay que")
        print("  replicar. Siguiente paso: --mode digest en dos plataformas distintas.")
        return

    feats, env = run_digest(args.onnx, args.data, args.split, args.num)
    tag = args.tag or platform.node().replace(" ", "_")
    os.makedirs(args.out_dir, exist_ok=True)
    npy_path = os.path.join(args.out_dir, f"features_digest_{tag}.npy")
    json_path = os.path.join(args.out_dir, f"features_digest_{tag}.json")
    np.save(npy_path, feats)
    digest = hashlib.sha256(feats.tobytes()).hexdigest()
    env |= {"sha256": digest, "onnx_sha256": sha256_file(args.onnx), "shape": list(feats.shape)}
    with open(json_path, "w") as f:
        json.dump(env, f, indent=2)

    print("\n" + "=" * 60)
    print("DIGEST DE FEATURES — control de reproducibilidad")
    print("=" * 60)
    print(f"  onnx        : {args.onnx}")
    print(f"  onnx sha256 : {env['onnx_sha256']}")
    print(f"  parches     : {len(feats)} de {env['dataset_size']} ({args.split})")
    print(f"  plataforma  : {env['platform']} | ORT {env['onnxruntime']} {env['providers']}")
    print(f"  features    : {npy_path} {feats.shape} {feats.dtype}")
    print(f"  sha256      : {digest}")
    if args.compare:
        compare(feats, args.compare)
    else:
        print("\n  Correr lo mismo en otra plataforma y contrastar con:")
        print(f"    --mode digest --compare {npy_path}")


if __name__ == "__main__":
    main()

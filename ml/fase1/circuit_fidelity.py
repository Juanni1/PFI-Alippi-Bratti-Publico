"""Fidelidad circuito-vs-float — ¿la prueba dice la verdad sobre el modelo?

La prueba zk garantiza que el circuito computó la inferencia, pero el circuito usa
**aritmética de punto fijo** (cuantización a `scale` bits), mientras que el modelo
PyTorch usa float32. Si la cuantización cambiara la clase predicha, la prueba sería
fiel al *circuito* pero no al *clasificador entrenado*. Este script mide ese gap:

  - **Agreement**: en qué % de parches la predicción en-circuito coincide con la float.
  - **Accuracy float vs en-circuito** sobre los mismos parches (¿pierde accuracy la
    cuantización respecto a las etiquetas verdaderas?).

Es la métrica que valida que todo el esfuerzo de la prueba representa fielmente al
modelo. Si el agreement es bajo, la palanca es subir `param_scale`/`input_scale`
(más bits de punto fijo → menos error, a costo de un circuito un poco más caro).

Usa `gen_witness` (corre el circuito hacia adelante y devuelve los logits
reescalados) — barato, NO requiere generar la prueba. Reutiliza el circuito
compilado en `artifacts/` si existe; si no, lo compila desde el checkpoint.

Ejecutar:
  uv run python -m fase1.circuit_fidelity --ckpt fase1/checkpoints/smallcnn_r64_bc8.pt --num-samples 200
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import io
import json
import os
import random

# HOME-fix Windows antes de importar ezkl (igual que en los workers de Fase 0).
if not os.environ.get("HOME"):
    os.environ["HOME"] = os.environ.get("USERPROFILE", os.path.expanduser("~"))

import ezkl
import torch
from PIL import Image

from fase0.calibration import write_calibration_json
from fase0.circuit import build_circuit, circuit_paths, read_metrics
from fase0.export import export_onnx
from fase1.prove_trained import eval_transform, load_trained_model


def ensure_circuit(model, res: int, out_dir: str, patches_root: str, split: str, num_calib: int, seed: int):
    """Reutiliza el circuito compilado si está; si no, lo compila desde el modelo entrenado."""
    settings_path, compiled_path = circuit_paths(out_dir, res)
    if os.path.exists(settings_path) and os.path.exists(compiled_path):
        return read_metrics(settings_path, compiled_path, res)

    tf = eval_transform(res)
    onnx_path = os.path.join(out_dir, f"smallcnn_r{res}.onnx")
    export_onnx(model, onnx_path)
    calib_paths = glob.glob(os.path.join(patches_root, split, "*", "*.jpg"))
    random.Random(seed + 1).shuffle(calib_paths)
    batch = torch.cat([tf(Image.open(q).convert("RGB")).unsqueeze(0) for q in calib_paths[:num_calib]])
    calib_path = os.path.join(out_dir, f"calib_real_r{res}.json")
    write_calibration_json(batch, calib_path)
    return build_circuit(onnx_path, calib_path, out_dir, resolution=res, scales=[7])


def circuit_logits(witness_input_path, compiled_path, witness_out_path, srs_path) -> list[float] | None:
    """Corre gen_witness y devuelve los logits reescalados (o None si no se pueden leer)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):  # gen_witness es ruidoso
        ezkl.gen_witness(witness_input_path, compiled_path, witness_out_path, vk_path=None, srs_path=srs_path)
    with open(witness_out_path) as f:
        w = json.load(f)
    outs = w.get("pretty_elements", {}).get("rescaled_outputs")
    return [float(v) for v in outs[0]] if outs else None


def main() -> None:
    p = argparse.ArgumentParser(description="Mide fidelidad circuito-vs-float del modelo entrenado.")
    p.add_argument("--ckpt", required=True)
    p.add_argument("--patches-root", default="fase1/patches")
    p.add_argument("--split", default="test", choices=["train", "val", "test"])
    p.add_argument("--out-dir", default="artifacts")
    p.add_argument("--num-samples", type=int, default=200, help="Cuántos parches evaluar.")
    p.add_argument("--num-calib", type=int, default=8)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    model, ck = load_trained_model(args.ckpt)
    res = model.input_resolution
    classes = ck.get("classes") or [str(i) for i in range(model.num_classes)]

    metrics = ensure_circuit(model, res, args.out_dir, args.patches_root, args.split, args.num_calib, args.seed)
    _, compiled_path = circuit_paths(args.out_dir, res)
    srs_path = os.path.join(args.out_dir, f"kzg_{metrics.logrows}.srs")
    if not os.path.exists(srs_path):
        ezkl.gen_srs(srs_path, metrics.logrows)
    win = os.path.join(args.out_dir, "_fid_win.json")
    wout = os.path.join(args.out_dir, "_fid_wout.json")

    tf = eval_transform(res)
    paths = glob.glob(os.path.join(args.patches_root, args.split, "*", "*.jpg"))
    random.Random(args.seed).shuffle(paths)
    paths = paths[: args.num_samples]
    print(f"Modelo: {os.path.basename(args.ckpt)} | logrows {metrics.logrows} | "
          f"evaluando {len(paths)} parches de '{args.split}'\n")

    n = float_correct = circuit_correct = agree = 0
    disagreements = []
    for path in paths:
        true_class = os.path.basename(os.path.dirname(path))
        x = tf(Image.open(path).convert("RGB")).unsqueeze(0)
        with torch.no_grad():
            float_logits = model(x).squeeze(0).tolist()
        float_idx = max(range(len(float_logits)), key=lambda i: float_logits[i])

        write_calibration_json(x, win)
        c_logits = circuit_logits(win, compiled_path, wout, srs_path)
        if c_logits is None:
            print("  (no se pudieron leer los logits del circuito; abortando)")
            return
        circuit_idx = max(range(len(c_logits)), key=lambda i: c_logits[i])

        n += 1
        float_correct += float_idx == classes.index(true_class)
        circuit_correct += circuit_idx == classes.index(true_class)
        if float_idx == circuit_idx:
            agree += 1
        else:
            disagreements.append((os.path.basename(path), classes[float_idx], classes[circuit_idx]))
        if n % 25 == 0:
            print(f"  {n}/{len(paths)} | agreement {agree / n:.3f}")

    print("\n" + "=" * 60)
    print(f"FIDELIDAD circuito-vs-float ({n} parches, logrows {metrics.logrows})")
    print("=" * 60)
    print(f"  Agreement (argmax circuito == float) : {agree}/{n} = {agree / n:.3f}")
    print(f"  Accuracy float    (vs etiqueta real) : {float_correct / n:.3f}")
    print(f"  Accuracy circuito (vs etiqueta real) : {circuit_correct / n:.3f}")
    if disagreements:
        print(f"\n  Desacuerdos ({len(disagreements)}):")
        for fname, f_pred, c_pred in disagreements[:20]:
            print(f"    {fname}: float={f_pred}  circuito={c_pred}")
    else:
        print("\n  Sin desacuerdos: la cuantización preservó TODAS las predicciones.")


if __name__ == "__main__":
    main()

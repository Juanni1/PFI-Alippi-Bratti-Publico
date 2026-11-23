"""Prueba zkML del modelo ENTRENADO sobre un parche real — cierre del loop de Fase 1.

Fase 0 validó el pipeline con pesos random (solo importaba la estructura). Acá se
hace lo que es el deliverable real: tomar un checkpoint **entrenado** de la SmallCNN,
clasificar un **parche real** de CarDD, y generar + verificar una **prueba
zero-knowledge** de esa inferencia.

Esquema de visibilidad (defaults de `build_circuit`): la imagen es **privada**
(`input_visibility=private`), los **logits son públicos** (`output_visibility=public`)
y los **pesos quedan fijos en el circuito** (`param_visibility=fixed`). O sea, la
prueba garantiza: "corrí ESTE modelo entrenado sobre una imagen privada y obtuve
ESTOS logits", sin revelar la imagen. El softmax/argmax queda fuera del circuito.

Pasos:
  1. Cargar checkpoint → reconstruir SmallCNN desde su `config` → cargar pesos.
  2. Exportar a ONNX (opset 13) reusando `fase0.export`.
  3. Preprocesar un parche real (resize a la resolución + normalización ImageNet,
     idéntico al eval del entrenamiento) y correr la inferencia float de referencia.
  4. Compilar el circuito DESDE el ONNX entrenado (calibrando con parches reales).
  5. setup (gen_srs + setup) en subproceso aislado (HOME-fix Windows).
  6. gen_witness con el parche real + prove en subproceso aislado.
  7. Verificar la prueba localmente.

Ejecutar:
  uv run python -m fase1.prove_trained --ckpt fase1/checkpoints/smallcnn_r64_bc8.pt
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import subprocess
import sys
import time

import torch
from PIL import Image
from torchvision import transforms

from fase0.calibration import IMAGENET_MEAN, IMAGENET_STD, write_calibration_json
from fase0.circuit import build_circuit, circuit_paths, read_metrics
from fase0.export import export_onnx
from fase0.smallcnn import SmallCNN

_GB = 1024 ** 3


def load_trained_model(ckpt_path: str) -> tuple[SmallCNN, dict]:
    """Reconstruye la SmallCNN desde el `config` guardado y le carga los pesos."""
    ck = torch.load(ckpt_path, weights_only=False)
    model = SmallCNN(**ck["config"])
    model.load_state_dict(ck["state_dict"])
    model.eval()
    return model, ck


def eval_transform(resolution: int) -> transforms.Compose:
    """Mismo preprocesamiento que el eval del entrenamiento (sin augmentation)."""
    return transforms.Compose(
        [
            transforms.Resize((resolution, resolution)),
            transforms.ToTensor(),
            transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
        ]
    )


def pick_patch(patches_root: str, split: str, seed: int) -> tuple[str, str]:
    """Elige un parche real al azar. Devuelve (ruta, clase_verdadera)."""
    paths = glob.glob(os.path.join(patches_root, split, "*", "*.jpg"))
    if not paths:
        raise FileNotFoundError(f"No hay parches en {os.path.join(patches_root, split)}")
    random.Random(seed).shuffle(paths)
    chosen = paths[0]
    true_class = os.path.basename(os.path.dirname(chosen))
    return chosen, true_class


def run_worker(cmd: list[str], ram_limit_gb: float) -> dict:
    """Corre un worker (_setup_worker/_prove_worker) y devuelve su línea RESULT como dict."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    stdout, _ = proc.communicate()
    result = None
    for line in stdout.splitlines():
        if line.startswith("RESULT:"):
            result = json.loads(line[len("RESULT:"):])
    if result is None:
        print(stdout)
        raise RuntimeError(f"worker no devolvió RESULT: {cmd[2]}")
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Prueba zkML del modelo entrenado sobre un parche real.")
    p.add_argument("--ckpt", required=True, help="Checkpoint .pt de fase1/train.")
    p.add_argument("--patches-root", default="fase1/patches")
    p.add_argument("--split", default="test", choices=["train", "val", "test"])
    p.add_argument("--patch", default=None, help="Ruta a un parche específico (si no, se elige al azar del split).")
    p.add_argument("--out-dir", default="artifacts")
    p.add_argument("--num-calib", type=int, default=8, help="Nº de parches reales para calibrar las escalas.")
    p.add_argument("--ram-limit-gb", type=float, default=38.0)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    model, ck = load_trained_model(args.ckpt)
    res = model.input_resolution
    classes = ck.get("classes") or [str(i) for i in range(model.num_classes)]
    print(f"Modelo: SmallCNN {ck['config']} | clases: {classes}")

    # --- 1-2. Export ONNX del modelo entrenado -----------------------------------
    onnx_path = os.path.join(args.out_dir, f"smallcnn_r{res}.onnx")
    export_onnx(model, onnx_path)
    print(f"ONNX entrenado exportado -> {onnx_path}")

    # --- 3. Parche real + inferencia float de referencia -------------------------
    tf = eval_transform(res)
    if args.patch:
        patch_path, true_class = args.patch, os.path.basename(os.path.dirname(args.patch))
    else:
        patch_path, true_class = pick_patch(args.patches_root, args.split, args.seed)
    x = tf(Image.open(patch_path).convert("RGB")).unsqueeze(0)  # 1x3xresxres
    with torch.no_grad():
        logits = model(x).squeeze(0)
    pred_idx = int(logits.argmax())
    print(f"\nParche: {patch_path}")
    print(f"  clase verdadera : {true_class}")
    print(f"  predicción float: {classes[pred_idx]}  (logits {[round(v, 2) for v in logits.tolist()]})")

    # --- 4. Calibración con parches reales + compilar circuito desde ONNX entrenado
    calib_paths = glob.glob(os.path.join(args.patches_root, args.split, "*", "*.jpg"))
    random.Random(args.seed + 1).shuffle(calib_paths)
    calib_batch = torch.cat([tf(Image.open(q).convert("RGB")).unsqueeze(0) for q in calib_paths[: args.num_calib]])
    calib_path = os.path.join(args.out_dir, f"calib_real_r{res}.json")
    write_calibration_json(calib_batch, calib_path)

    print("\nCompilando circuito desde el ONNX entrenado (gen_settings + calibrate + compile)...")
    metrics = build_circuit(onnx_path, calib_path, args.out_dir, resolution=res, scales=[7])
    logrows = metrics.logrows
    print(f"  logrows = {logrows} (2^{logrows} = {2 ** logrows:,} filas)")

    settings_path, compiled_path = circuit_paths(args.out_dir, res)
    srs_path = os.path.join(args.out_dir, f"kzg_{logrows}.srs")
    pk_path = os.path.join(args.out_dir, f"pk_r{res}.key")
    vk_path = os.path.join(args.out_dir, f"vk_r{res}.key")

    # --- 5. setup (gen_srs + setup) ----------------------------------------------
    print("\nsetup (gen_srs + setup, subproceso)...")
    t0 = time.time()
    setup_res = run_worker(
        [sys.executable, "-m", "fase0._setup_worker",
         "--compiled", compiled_path, "--srs", srs_path,
         "--pk", pk_path, "--vk", vk_path, "--logrows", str(logrows)],
        args.ram_limit_gb,
    )
    peak_setup = setup_res.get("peak_wset_bytes") or 0
    print(f"  setup OK | RAM pico {peak_setup / _GB:.2f} GB | "
          f"PK {os.path.getsize(pk_path) / _GB:.2f} GB | {time.time() - t0:.1f}s")

    # --- 6. witness (parche real) + prove ----------------------------------------
    witness_input_path = os.path.join(args.out_dir, f"witness_input_r{res}.json")
    witness_out_path = os.path.join(args.out_dir, f"witness_r{res}.json")
    proof_path = os.path.join(args.out_dir, f"proof_r{res}.json")
    write_calibration_json(x, witness_input_path)  # un solo parche real

    print("\ngen_witness (parche real) + prove (subproceso)...")
    t0 = time.time()
    prove_res = run_worker(
        [sys.executable, "-m", "fase0._prove_worker",
         "--compiled", compiled_path, "--witness-input", witness_input_path,
         "--witness-out", witness_out_path, "--pk", pk_path,
         "--proof-out", proof_path, "--srs", srs_path, "--settings", settings_path],
        args.ram_limit_gb,
    )
    peak_prove = prove_res.get("peak_wset_bytes") or 0
    print(f"  prove OK | RAM pico {peak_prove / _GB:.2f} GB | "
          f"prueba {prove_res['proof_bytes'] / 1024:.1f} KB | {time.time() - t0:.1f}s")

    # Predicción dentro del circuito (logits reescalados del witness), best-effort.
    circuit_pred = _circuit_prediction(witness_out_path, classes)
    if circuit_pred is not None:
        match = "==" if circuit_pred == classes[pred_idx] else "!=  (¡revisar escalas!)"
        print(f"  predicción en-circuito: {circuit_pred}  [{match} float]")

    # --- 7. Verificación ----------------------------------------------------------
    if not os.environ.get("HOME"):
        os.environ["HOME"] = os.environ.get("USERPROFILE", os.path.expanduser("~"))
    import ezkl

    ok = ezkl.verify(proof_path, settings_path, vk_path, srs_path)
    print("\n" + "=" * 60)
    if ok:
        print("VERIFY OK — prueba zk válida de la inferencia del modelo ENTRENADO")
        print(f"  parche real ({true_class}) -> predicción {classes[pred_idx]} | "
              f"logrows {logrows} | prueba {prove_res['proof_bytes'] / 1024:.1f} KB")
    else:
        print("VERIFY FALLÓ — ezkl.verify devolvió False")
        raise SystemExit(1)
    print("=" * 60)


def _circuit_prediction(witness_path: str, classes: list[str]) -> str | None:
    """Best-effort: extrae los logits reescalados del witness y devuelve el argmax."""
    try:
        with open(witness_path) as f:
            w = json.load(f)
        outs = w.get("pretty_elements", {}).get("rescaled_outputs")
        if not outs:
            return None
        vals = [float(v) for v in outs[0]]
        return classes[max(range(len(vals)), key=lambda i: vals[i])]
    except Exception:
        return None


if __name__ == "__main__":
    main()

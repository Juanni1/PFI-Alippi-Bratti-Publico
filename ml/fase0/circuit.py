"""gen_settings + calibrate_settings + compile_circuit — Paso 3 de Fase 0.

Produce el `settings.json` (con el `logrows`, el número central de la fase) y el
circuito compilado, a partir de un ONNX y datos de calibración en rango real.

Firmas confirmadas contra **ezkl 23.0.5** (no asumidas):
- `gen_settings(model, output, py_run_args=None) -> bool`
- `calibrate_settings(data, model, settings, target, lookup_safety_margin,
   scales=None, scale_rebase_multiplier=..., max_logrows=None) -> bool`
- `compile_circuit(model, compiled_circuit, settings_path) -> bool`

Hallazgo de Fase 0 (smoke test): `logrows` lo domina `total_assignments`
(estructura del modelo), no la escala — `required_lookups` queda vacío y
`num_rows ≈ total_assignments / num_inner_cols`. Por eso `num_inner_cols` es una
palanca de viabilidad además de la resolución.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

import ezkl
import torch

from fase0.calibration import make_calibration_batch, write_calibration_json
from fase0.checks import assert_circuit_safe
from fase0.export import export_onnx
from fase0.model import MobileNetV2


@dataclass
class CircuitMetrics:
    """Métricas leídas de `settings.json` tras compilar el circuito."""

    resolution: int
    logrows: int
    input_scale: int
    param_scale: int
    num_inner_cols: int
    num_rows: int
    total_assignments: int
    lookup_range: tuple[int, int]
    settings_path: str
    compiled_path: str

    def as_dict(self) -> dict:
        return asdict(self)


def build_circuit(
    onnx_path: str,
    calib_path: str,
    work_dir: str,
    *,
    resolution: int,
    target: str = "resources",
    input_visibility: str = "private",
    output_visibility: str = "public",
    param_visibility: str = "fixed",
    num_inner_cols: int = 2,
    input_scale: int | None = None,
    param_scale: int | None = None,
    scales: list[int] | None = None,
    lookup_safety_margin: int = 2,
) -> CircuitMetrics:
    """Corre gen_settings → calibrate_settings → compile_circuit y lee las métricas.

    `target`: "resources" (minimiza recursos) o "accuracy" (minimiza error). Para la
    viabilidad de Fase 0 se usa "resources" por defecto.
    `scales`: lista de escalas a probar en calibración. Acotarla (p. ej. `[7]`) reduce
    drásticamente el tiempo de calibración sin mover el `logrows` (que es estructural).
    """
    os.makedirs(work_dir, exist_ok=True)
    tag = f"r{resolution}"
    settings_path = os.path.join(work_dir, f"settings_{tag}.json")
    compiled_path = os.path.join(work_dir, f"model_{tag}.compiled")

    run_args = ezkl.PyRunArgs()
    run_args.input_visibility = input_visibility
    run_args.output_visibility = output_visibility
    run_args.param_visibility = param_visibility
    run_args.num_inner_cols = num_inner_cols
    if input_scale is not None:
        run_args.input_scale = input_scale
    if param_scale is not None:
        run_args.param_scale = param_scale

    if not ezkl.gen_settings(onnx_path, settings_path, py_run_args=run_args):
        raise RuntimeError("ezkl.gen_settings devolvió False")

    ok = ezkl.calibrate_settings(
        calib_path, onnx_path, settings_path, target, lookup_safety_margin, scales=scales
    )
    if not ok:
        raise RuntimeError("ezkl.calibrate_settings devolvió False")

    if not ezkl.compile_circuit(onnx_path, compiled_path, settings_path):
        raise RuntimeError("ezkl.compile_circuit devolvió False")

    return read_metrics(settings_path, compiled_path, resolution)


def circuit_paths(out_dir: str, resolution: int) -> tuple[str, str]:
    """Rutas (settings.json, model.compiled) para una resolución dada."""
    tag = f"r{resolution}"
    return (
        os.path.join(out_dir, f"settings_{tag}.json"),
        os.path.join(out_dir, f"model_{tag}.compiled"),
    )


def prepare_circuit(
    resolution: int,
    out_dir: str,
    *,
    num_classes: int = 7,
    width_mult: float = 0.5,
    target: str = "resources",
    num_samples: int = 4,
    scales: list[int] | None = None,
    num_inner_cols: int = 2,
    param_visibility: str = "fixed",
    arch: str = "mobilenetv2",
    smallcnn_kwargs: dict | None = None,
    reuse: bool = True,
    seed: int = 0,
) -> CircuitMetrics:
    """Pipeline Pasos 1-3: modelo → ONNX → calibración → settings/calibrate/compile.

    Con `reuse=True`, si ya existen `settings`/`compiled` para esa resolución los
    reutiliza (no recalibra) — útil para que el Paso 4 no repita la calibración.

    `arch`: "mobilenetv2" (default) o "smallcnn" (red minúscula de `smallcnn.py`,
    para presupuestos donde MobileNetV2 no entra). `smallcnn_kwargs` pasa
    base_channels/num_blocks/num_downsample a la SmallCNN.
    """
    os.makedirs(out_dir, exist_ok=True)
    settings_path, compiled_path = circuit_paths(out_dir, resolution)
    if reuse and os.path.exists(settings_path) and os.path.exists(compiled_path):
        return read_metrics(settings_path, compiled_path, resolution)

    torch.manual_seed(seed)
    if arch == "smallcnn":
        from fase0.smallcnn import SmallCNN

        model = SmallCNN(
            num_classes=num_classes,
            input_resolution=resolution,
            width_mult=width_mult,
            **(smallcnn_kwargs or {}),
        )
        onnx_name = f"smallcnn_r{resolution}.onnx"
    elif arch == "mobilenetv2":
        model = MobileNetV2(
            num_classes=num_classes, width_mult=width_mult, input_resolution=resolution
        )
        onnx_name = f"mobilenetv2_a{width_mult}_r{resolution}.onnx"
    else:
        raise ValueError(f"arch desconocida: {arch!r} (usar 'mobilenetv2' o 'smallcnn')")
    model.eval()
    assert_circuit_safe(model)

    onnx_path = os.path.join(out_dir, onnx_name)
    export_onnx(model, onnx_path)

    batch = make_calibration_batch(resolution, num_samples=num_samples)
    calib_path = os.path.join(out_dir, f"calib_r{resolution}.json")
    write_calibration_json(batch, calib_path)

    return build_circuit(
        onnx_path,
        calib_path,
        out_dir,
        resolution=resolution,
        target=target,
        scales=scales,
        num_inner_cols=num_inner_cols,
        param_visibility=param_visibility,
    )


def read_metrics(settings_path: str, compiled_path: str, resolution: int) -> CircuitMetrics:
    """Lee `logrows` y métricas asociadas del `settings.json` post-compilación."""
    with open(settings_path) as f:
        settings = json.load(f)
    ra = settings["run_args"]
    lr = ra["lookup_range"]
    return CircuitMetrics(
        resolution=resolution,
        logrows=ra["logrows"],
        input_scale=ra["input_scale"],
        param_scale=ra["param_scale"],
        num_inner_cols=ra["num_inner_cols"],
        num_rows=settings["num_rows"],
        total_assignments=settings["total_assignments"],
        lookup_range=(lr[0], lr[1]),
        settings_path=settings_path,
        compiled_path=compiled_path,
    )

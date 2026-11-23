"""Paso 2 de Fase 0 — Export a ONNX (opset 13).

Construye el modelo (pesos aleatorios), lo exporta a ONNX con `dynamo=False`,
opset 13, batch fijo = 1 y dimensiones estáticas; valida con `onnx.checker` y
verifica que el grafo ONNX no contenga operadores prohibidos.

Ejecutar:  uv run python -m fase0.step2
           uv run python -m fase0.step2 --resolution 96
"""

from __future__ import annotations

import argparse
import os

import onnx
import torch

from fase0.checks import assert_circuit_safe
from fase0.export import (
    assert_onnx_circuit_safe,
    assert_opset,
    assert_static_shapes,
    export_onnx,
    op_type_counts,
    shape_of,
)
from fase0.model import MobileNetV2


def run(
    resolution: int = 224,
    num_classes: int = 7,
    width_mult: float = 0.5,
    out_dir: str = "artifacts",
) -> str:
    torch.manual_seed(0)

    model = MobileNetV2(
        num_classes=num_classes, width_mult=width_mult, input_resolution=resolution
    )
    model.eval()
    assert_circuit_safe(model)  # barrera a nivel PyTorch antes de exportar

    onnx_path = os.path.join(out_dir, f"mobilenetv2_a{width_mult}_r{resolution}.onnx")
    export_onnx(model, onnx_path)

    # --- Validación del ONNX ------------------------------------------------------
    onnx_model = onnx.load(onnx_path)
    onnx.checker.check_model(onnx_model)  # estructura/tipos válidos
    assert_opset(onnx_model, expected=13)
    assert_static_shapes(onnx_model)
    assert_onnx_circuit_safe(onnx_model)  # verificación definitiva sobre el grafo real

    # --- Reporte ------------------------------------------------------------------
    in_shape = shape_of(onnx_model.graph.input[0])
    out_shape = shape_of(onnx_model.graph.output[0])
    size_mb = os.path.getsize(onnx_path) / (1024 * 1024)
    counts = op_type_counts(onnx_model)

    print(f"ONNX exportado: {onnx_path}")
    print(f"  opset        : 13 | IR v{onnx_model.ir_version}")
    print(f"  input  'input' : {in_shape}")
    print(f"  output 'logits': {out_shape}")
    print(f"  tamano archivo : {size_mb:.2f} MB | {len(onnx_model.graph.node)} nodos")
    print(f"  op-types       : {counts}")
    print("  onnx.checker.check_model OK")
    print("  dimensiones estaticas OK (sin dynamic_axes)")
    print("  verificacion de circuito OK: sin MaxPool / h-swish / SE / softmax-argmax en el grafo")
    return onnx_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Paso 2 Fase 0: export a ONNX opset 13")
    parser.add_argument("--resolution", type=int, default=224)
    parser.add_argument("--num-classes", type=int, default=7)
    parser.add_argument("--width-mult", type=float, default=0.5)
    parser.add_argument("--out-dir", type=str, default="artifacts")
    args = parser.parse_args()
    run(
        resolution=args.resolution,
        num_classes=args.num_classes,
        width_mult=args.width_mult,
        out_dir=args.out_dir,
    )


if __name__ == "__main__":
    main()

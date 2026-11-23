"""Export a ONNX (opset 13) y verificaciones a nivel de grafo — Paso 2 de Fase 0.

Reglas validadas para EZKL 23.0.5: `torch.onnx.export(...,
opset_version=13, dynamo=False)`, batch fijo = 1, dimensiones **estáticas** (sin
dims dinámicas), validado con `onnx.checker.check_model`.

Además de validar el ONNX, este módulo repite la verificación de operadores
prohibidos **sobre el grafo ONNX real** (no solo sobre los módulos PyTorch): es la
barrera definitiva del Paso 1/2 antes de pasar a `gen_settings`.
"""

from __future__ import annotations

import os

import onnx
import torch

from fase0.model import MobileNetV2, example_input

# Op-types de ONNX no representables / prohibidos por las restricciones de Fase 0.
# ReLU6 se exporta como `Clip(min=0, max=6)` en opset 13, no como `Relu`.
_FORBIDDEN_ONNX_OPS: frozenset[str] = frozenset(
    {
        # Pooling máximo (debe ser AvgPool / GlobalAveragePool)
        "MaxPool",
        "GlobalMaxPool",
        "MaxRoiPool",
        # Activaciones prohibidas (h-swish, gating de squeeze-and-excitation, etc.)
        "HardSwish",
        "HardSigmoid",
        "Sigmoid",
        "Selu",
        "Elu",
        "LeakyRelu",
        "PRelu",
        "ThresholdedRelu",
        "Gelu",
        "Mish",
        # Normalización de probabilidades / decisión: fuera del circuito
        "Softmax",
        "LogSoftmax",
        "ArgMax",
        "ArgMin",
    }
)


def export_onnx(
    model: MobileNetV2,
    onnx_path: str,
    *,
    batch_size: int = 1,
) -> str:
    """Exporta `model` a ONNX opset 13 con dimensiones estáticas (sin dynamic_axes).

    Devuelve la ruta del archivo escrito.
    """
    model.eval()
    dummy = example_input(model, batch_size=batch_size)

    os.makedirs(os.path.dirname(os.path.abspath(onnx_path)), exist_ok=True)
    torch.onnx.export(
        model,
        dummy,
        onnx_path,
        opset_version=13,
        do_constant_folding=True,
        input_names=["input"],
        output_names=["logits"],
        dynamo=False,  # exportador legacy (TorchScript), el validado para EZKL 23.0.5
        # Sin `dynamic_axes`: todas las dimensiones quedan estáticas.
    )
    return onnx_path


def assert_opset(onnx_model: onnx.ModelProto, expected: int = 13) -> None:
    """Verifica que el dominio por defecto use exactamente el opset esperado."""
    versions = {imp.domain: imp.version for imp in onnx_model.opset_import}
    main = versions.get("", None)
    if main != expected:
        raise AssertionError(f"opset del dominio por defecto = {main}, se esperaba {expected}")


def assert_static_shapes(onnx_model: onnx.ModelProto) -> None:
    """Verifica que entradas y salidas tengan dimensiones concretas (sin dims dinámicas)."""
    problems: list[str] = []
    for vi in list(onnx_model.graph.input) + list(onnx_model.graph.output):
        for i, d in enumerate(vi.type.tensor_type.shape.dim):
            if d.HasField("dim_param"):
                problems.append(f"  - '{vi.name}' eje {i}: dimensión dinámica '{d.dim_param}'")
            elif not d.HasField("dim_value"):
                problems.append(f"  - '{vi.name}' eje {i}: dimensión sin valor concreto")
    if problems:
        raise AssertionError("El ONNX tiene dimensiones dinámicas:\n" + "\n".join(problems))


def assert_onnx_circuit_safe(onnx_model: onnx.ModelProto) -> None:
    """Repite la verificación de operadores prohibidos sobre el grafo ONNX real."""
    problems: list[str] = []
    for node in onnx_model.graph.node:
        if node.op_type in _FORBIDDEN_ONNX_OPS:
            name = node.name or "<sin nombre>"
            problems.append(f"  - nodo '{name}': op '{node.op_type}' prohibida en el circuito")
    if problems:
        raise AssertionError(
            "El grafo ONNX contiene operadores no representables en el circuito:\n"
            + "\n".join(problems)
        )


def op_type_counts(onnx_model: onnx.ModelProto) -> dict[str, int]:
    """Conteo de op-types presentes en el grafo (para el reporte)."""
    counts: dict[str, int] = {}
    for node in onnx_model.graph.node:
        counts[node.op_type] = counts.get(node.op_type, 0) + 1
    return dict(sorted(counts.items()))


def shape_of(value_info: onnx.ValueInfoProto) -> tuple[int, ...]:
    """Forma (dims concretas) de una entrada/salida del grafo."""
    return tuple(d.dim_value for d in value_info.type.tensor_type.shape.dim)

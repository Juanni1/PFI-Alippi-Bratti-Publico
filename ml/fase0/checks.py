"""Verificación de que el grafo no contiene operadores prohibidos en el circuito.

Acceptance del Paso 1: confirmar a nivel de módulos que no quedó ningún operador
no representable como circuito EZKL (h-swish, squeeze-and-excitation, MaxPool,
softmax/argmax internos). La verificación definitiva sobre el grafo ONNX se hará
en el Paso 2; esta es la barrera temprana a nivel PyTorch.
"""

from __future__ import annotations

from torch import nn

# Denylist de módulos no representables / prohibidos por las restricciones de Fase 0.
# Nota: nn.ReLU6 NO es subclase de nn.ReLU, así que incluir nn.ReLU no afecta a ReLU6.
_FORBIDDEN_MODULES: tuple[type[nn.Module], ...] = (
    # Pooling máximo (debe ser AvgPool global)
    nn.MaxPool1d,
    nn.MaxPool2d,
    nn.MaxPool3d,
    nn.AdaptiveMaxPool1d,
    nn.AdaptiveMaxPool2d,
    nn.AdaptiveMaxPool3d,
    # Activaciones prohibidas (solo se admite ReLU6); Hardswish/Hardsigmoid = h-swish,
    # Sigmoid = gating de squeeze-and-excitation.
    nn.Hardswish,
    nn.Hardsigmoid,
    nn.SiLU,
    nn.Sigmoid,
    nn.GELU,
    nn.ELU,
    nn.LeakyReLU,
    nn.ReLU,
    # Normalización de probabilidades dentro del grafo (debe quedar fuera del circuito)
    nn.Softmax,
    nn.LogSoftmax,
)


def assert_circuit_safe(model: nn.Module) -> None:
    """Lanza AssertionError si el modelo contiene operadores no representables.

    Comprueba:
    1. Ausencia de módulos en la denylist (MaxPool, h-swish, SE, softmax, etc.).
    2. Presencia de al menos una ReLU6 (la única no linealidad admitida).
    3. Que la cabeza sea una `nn.Linear` (emite logits), no una capa de probabilidades.
    """
    problems: list[str] = []

    has_relu6 = False
    linear_layers: list[str] = []
    for name, module in model.named_modules():
        if isinstance(module, _FORBIDDEN_MODULES):
            problems.append(f"  - '{name}': {type(module).__name__} prohibido en el circuito")
        if isinstance(module, nn.ReLU6):
            has_relu6 = True
        if isinstance(module, nn.Linear):
            linear_layers.append(name)

    if not has_relu6:
        problems.append("  - no se encontró ninguna ReLU6 (única no linealidad admitida)")

    if not linear_layers:
        problems.append("  - la cabeza no es una nn.Linear (debe emitir logits)")

    if problems:
        raise AssertionError(
            "El modelo NO es seguro para el circuito; operadores/condiciones detectados:\n"
            + "\n".join(problems)
        )

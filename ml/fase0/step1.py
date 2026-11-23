"""Paso 1 de Fase 0 — Arquitectura paramétrica (PyTorch).

Construye MobileNetV2 α=0.5 con pesos aleatorios, corre un forward pass de prueba
y verifica que no quedó ningún operador prohibido en el grafo.

Ejecutar:  uv run python -m fase0.step1
"""

from __future__ import annotations

import argparse

import torch

from fase0.checks import assert_circuit_safe
from fase0.model import MobileNetV2, example_input


def run(resolution: int = 224, num_classes: int = 7, width_mult: float = 0.5) -> MobileNetV2:
    torch.manual_seed(0)

    model = MobileNetV2(
        num_classes=num_classes, width_mult=width_mult, input_resolution=resolution
    )
    model.eval()  # BN usa running stats; modo de inferencia para el export del Paso 2

    # --- Forward pass de prueba ---------------------------------------------------
    x = example_input(model)
    with torch.no_grad():
        logits = model(x)

    expected_shape = (1, num_classes)
    assert tuple(logits.shape) == expected_shape, (
        f"forma de salida inesperada: {tuple(logits.shape)} != {expected_shape}"
    )

    # --- Verificación de operadores prohibidos ------------------------------------
    assert_circuit_safe(model)

    # --- Reporte ------------------------------------------------------------------
    n_params = sum(p.numel() for p in model.parameters())
    prob_sum = torch.softmax(logits, dim=1).sum().item()  # solo para evidenciar logits
    print(f"MobileNetV2 alpha={width_mult} | resolucion {resolution}x{resolution} | {num_classes} clases")
    print(f"  input  : {tuple(x.shape)}")
    print(f"  output : {tuple(logits.shape)}  (logits; rango [{logits.min():.3f}, {logits.max():.3f}])")
    print(f"  params : {n_params:,} ({n_params / 1e6:.2f} M)")
    print(f"  last_channel: {model.last_channel}")
    print("  forward pass OK")
    print("  verificacion de circuito OK: sin MaxPool / h-swish / SE / softmax-argmax internos")
    print(f"  (la salida NO esta normalizada: softmax(logits).sum() = {prob_sum:.4f}; logits crudos)")
    return model


def main() -> None:
    parser = argparse.ArgumentParser(description="Paso 1 Fase 0: arquitectura MobileNetV2 zkML")
    parser.add_argument("--resolution", type=int, default=224)
    parser.add_argument("--num-classes", type=int, default=7)
    parser.add_argument("--width-mult", type=float, default=0.5)
    args = parser.parse_args()
    run(resolution=args.resolution, num_classes=args.num_classes, width_mult=args.width_mult)


if __name__ == "__main__":
    main()

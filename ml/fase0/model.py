"""MobileNetV2 (α configurable) para zkML — Paso 1 de Fase 0.

Arquitectura parametrizada por **resolución de entrada** y **número de clases**,
diseñada desde el inicio para ser representable como circuito EZKL. Restricciones
respetadas:

- No linealidad: **solo ReLU6** (nada de h-swish).
- Pooling: **AvgPool global** (`AdaptiveAvgPool2d(1)` → `GlobalAveragePool` en ONNX),
  nunca MaxPool.
- **Sin** bloques squeeze-and-excitation.
- La cabeza emite **logits** (una `nn.Linear`); softmax/argmax quedan **fuera** del
  circuito.
- **Pesos aleatorios**, sin entrenar: la viabilidad del circuito depende de la
  estructura (operadores, dimensiones), no de los valores de los pesos.

La red es completamente convolucional hasta el AvgPool global, así que es
*resolución-agnóstica* a nivel de parámetros: la resolución solo determina la forma
del tensor de entrada (y, vía ese tensor, el tamaño del circuito en fases siguientes).
`input_resolution` se guarda para construir el input de ejemplo y el export ONNX.
"""

from __future__ import annotations

import torch
from torch import nn

# Configuración canónica de MobileNetV2: t=expansión, c=canales de salida,
# n=repeticiones, s=stride del primer bloque del grupo.
_INVERTED_RESIDUAL_SETTING: list[tuple[int, int, int, int]] = [
    (1, 16, 1, 1),
    (6, 24, 2, 2),
    (6, 32, 3, 2),
    (6, 64, 4, 2),
    (6, 96, 3, 1),
    (6, 160, 3, 2),
    (6, 320, 1, 1),
]


def _make_divisible(value: float, divisor: int = 8, min_value: int | None = None) -> int:
    """Redondea `value` al múltiplo de `divisor` más cercano (estilo MobileNetV2),
    sin bajar más de ~10% del valor original."""
    if min_value is None:
        min_value = divisor
    new_value = max(min_value, int(value + divisor / 2) // divisor * divisor)
    if new_value < 0.9 * value:
        new_value += divisor
    return new_value


class ConvBNReLU6(nn.Sequential):
    """Conv2d (sin bias) → BatchNorm2d → ReLU6 opcional.

    Con `relu=False` implementa el bottleneck lineal (proyección 1×1 sin activación).
    """

    def __init__(
        self,
        in_ch: int,
        out_ch: int,
        kernel_size: int = 3,
        stride: int = 1,
        groups: int = 1,
        relu: bool = True,
    ) -> None:
        padding = (kernel_size - 1) // 2
        layers: list[nn.Module] = [
            nn.Conv2d(in_ch, out_ch, kernel_size, stride, padding, groups=groups, bias=False),
            nn.BatchNorm2d(out_ch),
        ]
        if relu:
            layers.append(nn.ReLU6(inplace=True))
        super().__init__(*layers)


class InvertedResidual(nn.Module):
    """Bloque inverted-residual: expand 1×1 (si t≠1) → depthwise 3×3 → project 1×1
    lineal, con conexión residual cuando stride=1 e in==out."""

    def __init__(self, inp: int, oup: int, stride: int, expand_ratio: int) -> None:
        super().__init__()
        if stride not in (1, 2):
            raise ValueError(f"stride debe ser 1 o 2, recibido {stride}")
        hidden_dim = int(round(inp * expand_ratio))
        self.use_res_connect = stride == 1 and inp == oup

        layers: list[nn.Module] = []
        if expand_ratio != 1:
            layers.append(ConvBNReLU6(inp, hidden_dim, kernel_size=1))  # expand
        layers.extend(
            [
                # depthwise 3×3
                ConvBNReLU6(hidden_dim, hidden_dim, stride=stride, groups=hidden_dim),
                # project 1×1 lineal (sin ReLU6)
                ConvBNReLU6(hidden_dim, oup, kernel_size=1, relu=False),
            ]
        )
        self.conv = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.use_res_connect:
            return x + self.conv(x)  # Add residual (representable en circuito)
        return self.conv(x)


class MobileNetV2(nn.Module):
    """MobileNetV2 con multiplicador de ancho α (`width_mult`) y cabeza de logits."""

    def __init__(
        self,
        num_classes: int = 7,
        width_mult: float = 0.5,
        input_resolution: int = 224,
        round_nearest: int = 8,
    ) -> None:
        super().__init__()
        self.num_classes = num_classes
        self.width_mult = width_mult
        self.input_resolution = input_resolution

        input_channel = _make_divisible(32 * width_mult, round_nearest)
        # Para α<1.0 el último canal NO se reduce (se mantiene 1280), igual que el
        # MobileNetV2 de referencia.
        self.last_channel = _make_divisible(1280 * max(1.0, width_mult), round_nearest)

        features: list[nn.Module] = [ConvBNReLU6(3, input_channel, stride=2)]  # stem 3×3 /2
        for t, c, n, s in _INVERTED_RESIDUAL_SETTING:
            output_channel = _make_divisible(c * width_mult, round_nearest)
            for i in range(n):
                stride = s if i == 0 else 1
                features.append(InvertedResidual(input_channel, output_channel, stride, t))
                input_channel = output_channel
        features.append(ConvBNReLU6(input_channel, self.last_channel, kernel_size=1))  # head 1×1
        self.features = nn.Sequential(*features)

        self.pool = nn.AdaptiveAvgPool2d(1)  # AvgPool global
        self.classifier = nn.Linear(self.last_channel, num_classes)  # logits

        self._initialize_weights()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.features(x)
        x = self.pool(x)
        x = torch.flatten(x, 1)
        return self.classifier(x)  # logits crudos: ni softmax ni argmax

    def _initialize_weights(self) -> None:
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out")
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.ones_(m.weight)
                nn.init.zeros_(m.bias)
            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                nn.init.zeros_(m.bias)


def example_input(model: MobileNetV2, batch_size: int = 1) -> torch.Tensor:
    """Tensor de entrada de ejemplo (NCHW) a la resolución del modelo.

    Batch fijo = 1 por defecto, como exige el export ONNX del Paso 2.
    """
    res = model.input_resolution
    return torch.randn(batch_size, 3, res, res)

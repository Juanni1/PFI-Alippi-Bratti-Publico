"""CNN chica para zkML — alternativa a MobileNetV2 cuando esta no entra en presupuesto.

MobileNetV2 completa tiene cientos de miles de parámetros y demasiados MACs de
convolución → logrows/PK fuera de cualquier presupuesto razonable a resoluciones
útiles (ver `fase0_resultados.md`). Esta red es **deliberadamente
minúscula**: pocas capas, pocos canales y downsampling agresivo, para que el costo
del circuito (dominado por los MACs) baje en órdenes de magnitud.

Respeta las mismas restricciones de circuito que MobileNetV2 (ver `checks.py`):
- Solo **ReLU6** como no linealidad.
- **AvgPool global** (`AdaptiveAvgPool2d(1)`), nunca MaxPool.
- Sin squeeze-and-excitation.
- Cabeza `nn.Linear` que emite **logits**.

Truco de costo: usa convoluciones **depthwise-separable** (depthwise 3×3 + pointwise
1×1), que cuestan ~k² menos MACs que una conv estándar, y baja el tamaño espacial
con stride 2 en las primeras capas (el costo de cada capa es ∝ H×W).
"""

from __future__ import annotations

import torch
from torch import nn

from fase0.model import ConvBNReLU6, _make_divisible


class SmallCNN(nn.Module):
    """CNN chica parametrizable por nº de bloques, canales base y ancho.

    Arquitectura:
      stem: conv 3×3 /2 (3 → c0)
      bloques depthwise-separable: depthwise 3×3 (stride 2 en los primeros
        `num_downsample`, 1 después) + pointwise 1×1, duplicando canales en cada
        downsample.
      AvgPool global → Linear (logits).

    Defaults pensados para ser baratos en circuito. Subir `base_channels` o
    `num_blocks` aumenta MACs (y por ende logrows); bajarlos lo abarata.
    """

    def __init__(
        self,
        num_classes: int = 7,
        input_resolution: int = 64,
        base_channels: int = 8,
        num_blocks: int = 4,
        num_downsample: int = 3,
        width_mult: float = 1.0,
        round_nearest: int = 8,
    ) -> None:
        super().__init__()
        self.num_classes = num_classes
        self.input_resolution = input_resolution
        self.width_mult = width_mult

        c0 = _make_divisible(base_channels * width_mult, round_nearest)
        layers: list[nn.Module] = [ConvBNReLU6(3, c0, kernel_size=3, stride=2)]  # stem /2

        in_ch = c0
        for i in range(num_blocks):
            stride = 2 if i < num_downsample else 1
            # duplicamos canales cuando bajamos resolución (patrón estándar)
            out_ch = _make_divisible(in_ch * 2, round_nearest) if stride == 2 else in_ch
            layers.append(
                # depthwise 3×3 (groups=in_ch)
                ConvBNReLU6(in_ch, in_ch, kernel_size=3, stride=stride, groups=in_ch)
            )
            layers.append(
                # pointwise 1×1
                ConvBNReLU6(in_ch, out_ch, kernel_size=1)
            )
            in_ch = out_ch

        self.features = nn.Sequential(*layers)
        self.last_channel = in_ch
        self.pool = nn.AdaptiveAvgPool2d(1)  # AvgPool global
        self.classifier = nn.Linear(in_ch, num_classes)  # logits

        self._initialize_weights()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.features(x)
        x = self.pool(x)
        x = torch.flatten(x, 1)
        return self.classifier(x)  # logits crudos

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

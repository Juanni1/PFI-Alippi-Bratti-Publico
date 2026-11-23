"""Datos de calibración en rango real — Paso 3 de Fase 0.

`calibrate_settings` fija las escalas de punto fijo a partir del rango de los datos,
así que debe recibir tensores de imagen **normalizados como en producción**, aunque
los pesos del modelo sean aleatorios. Calibrar con datos
fuera de rango da un `logrows`/escalas engañosos.

Se asume el preprocesamiento estándar tipo ImageNet (píxeles [0,1] normalizados con
media/desvío por canal). Si Fase 1 define otro preprocesamiento, actualizar estas
constantes para que la calibración siga reflejando el rango real de entrada.
"""

from __future__ import annotations

import json
import os

import torch

# Preprocesamiento de producción asumido (ImageNet). Debe coincidir con el de Fase 1.
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


def make_calibration_batch(resolution: int, num_samples: int = 4, seed: int = 0) -> torch.Tensor:
    """Genera `num_samples` imágenes en el rango real de entrada (NCHW).

    Simula píxeles crudos uniformes en [0,1] y les aplica la normalización de
    producción, produciendo el mismo rango que verá el preprocesamiento real.
    """
    g = torch.Generator().manual_seed(seed)
    pixels = torch.rand(num_samples, 3, resolution, resolution, generator=g)  # [0,1]
    mean = torch.tensor(IMAGENET_MEAN).view(1, 3, 1, 1)
    std = torch.tensor(IMAGENET_STD).view(1, 3, 1, 1)
    return (pixels - mean) / std


def write_calibration_json(batch: torch.Tensor, path: str) -> str:
    """Escribe el batch al formato de datos de EZKL (`{"input_data": [...]}`).

    Cada muestra se aplana; EZKL itera sobre ellas durante la calibración.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    input_data = [batch[i].flatten().tolist() for i in range(batch.shape[0])]
    with open(path, "w") as f:
        json.dump({"input_data": input_data}, f)
    return path

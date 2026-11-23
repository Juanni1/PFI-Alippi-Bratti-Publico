"""Fase 0 — Validación de viabilidad del circuito (zkML).

Ver `fase0_resultados.md` para los resultados de la fase. Este paquete implementa
los pasos del pipeline; el Paso 1 (arquitectura paramétrica MobileNetV2 α=0.5)
vive en `fase0.model` / `fase0.checks` y se ejecuta con `python -m fase0.step1`.
"""

from fase0.model import MobileNetV2, example_input
from fase0.checks import assert_circuit_safe

__all__ = ["MobileNetV2", "example_input", "assert_circuit_safe"]

# Fase 0 — Resultados y cierre

> Validación de viabilidad del circuito zkML **antes de entrenar**. Stack: EZKL 23.0.5 (Halo2/KZG, BN254), ONNX opset 13, PyTorch. Pesos aleatorios (la viabilidad depende de la estructura, no de los valores).

## Objetivo de la fase

Confirmar que la arquitectura de clasificación de daños vehiculares compila a un circuito EZKL dentro de un presupuesto de RAM/tiempo tolerable, y determinar la **resolución máxima viable** que alimenta la Fase 1. El entregable no es un modelo: es la tabla de viabilidad y la decisión de arquitectura/resolución.

## Pipeline implementado (Pasos 1–8)

Paquete `fase0/`: arquitectura paramétrica → export ONNX → settings/calibrate/compile → SRS+setup → witness+prueba → verificación → EVM verifier → barrido. El pipeline zkML **cierra de punta a punta**: se genera una prueba criptográfica de la inferencia y se verifica localmente (`verify OK`).

## Hallazgo 1 — MobileNetV2 es inviable a resoluciones útiles

Con `param_visibility="fixed"` la proving key (PK) la dominan las **columnas (= parámetros)**, no las filas. MobileNetV2 tiene cientos de miles de parámetros:

| Config | logrows | PK / RAM de proving |
|---|---|---|
| α=0.5, res 32 | 22 | PK ~35 GB → RAM ~70 GB |
| α=0.5, res 64 | 24 | RAM ~290 GB |
| α=0.5, res 96 | 25 | RAM ~560 GB |
| α=0.5, res 224 | 28 | RAM ~4,5 TB |

Ninguna palanca menor lo salva: bajar α a 0.35 solo llega a res 16 dentro de 19 GB, y **`param_visibility="hashed"` EMPEORA** (logrows 22→25 en res 32: hashear los pesos dentro del circuito agrega más filas de las que ahorra en columnas). Conclusión: una CNN de visión estándar no entra en hardware razonable a resolución útil.

## Hallazgo 2 — El techo de RAM lo fija el `logrows`, no la arquitectura

El setup/prove necesita buffers de FFT sobre 2^logrows. Medido en máquina de **19 GB: el techo es logrows 20** (prove llega a ~19 GB; logrows 21 da OOM siempre, sea cual sea la red). La arquitectura no cambia el techo de RAM — cambia **cuánta resolución entra en ese logrows**.

## Decisión — pivote a SmallCNN

Se diseñó `fase0/smallcnn.py`: CNN minúscula depthwise-separable (pocas capas, pocos canales, downsampling agresivo) que respeta las mismas restricciones de circuito (solo ReLU6, AvgPool global, sin SE, cabeza de logits). A **igual presupuesto de RAM**:

| Arquitectura | Resolución máx a logrows 20 |
|---|---|
| MobileNetV2 α=0.35 | res 16 (inservible) |
| **SmallCNN** | **res 48** (9× los píxeles) |

Pipeline completo verificado en PC para SmallCNN res 48: setup 15,2 GB · prove 18,9 GB · **verify OK** · prueba 34,5 KB.

## Tabla de viabilidad final (SmallCNN)

| Resolución | logrows | RAM de prueba aprox | Hardware necesario | Verificado |
|---|---|---|---|---|
| 48 | 20 | ~19 GB | PC 19–40 GB | ✅ verify OK |
| 64 | 21 | ~30 GB | PC/VM 48 GB | estimado |
| 96 | ~22 | ~60 GB | VM 64 GB (~USD 2–4/h) | estimado |

**Resolución máxima viable elegida para Fase 1: 64 px** (entra en la PC actual liberando RAM; 96 px requiere una VM modesta). Ya **no se necesita** el hardware monstruoso (500 GB–1 TB) que pedía MobileNetV2.

## Dataset confirmado — CarDD

Cargado en `CarDD_release/CarDD_COCO/`. 4.000 imágenes (~1000×750), formato COCO con bbox + máscara por instancia. **6 clases** = dent, scratch, crack, glass shatter, lamp broken, tire flat (6.211 instancias en train). El formato encaja con el framing de **clasificación de parches recortados**: la localización del daño queda fuera del circuito; el circuito solo prueba la clasificación del parche.

Atención para Fase 1: **desbalance de clases** fuerte (scratch 2.560 vs tire flat 225) → compensar con pesos por clase / augmentation.

## Restricciones de circuito (confirmadas, no negociables)

Solo ReLU6 · AvgPool global (nunca MaxPool) · sin squeeze-and-excitation · cabeza emite logits (softmax/argmax fuera del circuito). Verificado a nivel PyTorch (`checks.py`) y grafo ONNX (`export.py`).

## Salida de la fase → entrada de Fase 1

1. **Arquitectura:** SmallCNN (depthwise-separable, paramétrica) — no MobileNetV2.
2. **Resolución:** 64 px (objetivo), 48 px piso seguro, 96 px con VM.
3. **Tarea:** clasificación de 6 tipos de daño sobre **parches recortados** del bbox.
4. **Dataset:** CarDD_COCO, con extracción de parches pendiente (primer paso de Fase 1).
5. **Hardware:** la PC actual (40 GB) alcanza hasta res 64; res 96 con VM on-demand barata.

El pipeline de prueba zkML está validado de punta a punta; Fase 1 puede entrenar con la certeza de que el modelo resultante es demostrable dentro de presupuesto.

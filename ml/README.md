# ML — Pipeline zkML de clasificación de daños vehiculares

Módulo de aprendizaje automático y pruebas de conocimiento cero del PFI (Alippi y Bratti, UADE): entrena un clasificador de daños vehiculares sobre parches recortados del dataset CarDD, genera pruebas criptográficas *zero-knowledge* de la inferencia y produce el verificador Solidity consumido por `contracts/` y `backend/`.

**Stack:** EZKL 23.0.5 (Halo2/KZG sobre BN254) · ONNX opset 13 · PyTorch · verificador Solidity. Entorno gestionado con **uv** y Python 3.12.

## Arquitectura vigente: *features-afuera*

Para mantener el circuito dentro de límites prácticos de memoria, tiempo de *proving* y tamaño de bytecode EVM (EIP-170), el circuito no cubre la imagen completa. Un extractor `MobileNetV3-small` calcula un vector de 576 características fuera de la prueba, y el circuito EZKL prueba únicamente la cabeza lineal de clasificación:

```text
imagen → [ backbone MobileNetV3-small ] → 576 features → [ Linear 576→6 ] → logits
          AFUERA del circuito, en claro                   DENTRO del circuito
```

El circuito emite *logits*; `softmax` y `argmax` permanecen fuera. El vector de características ingresa como **entrada pública**, lo que incrementa el costo de gas y motiva la evaluación empírica de reconstrucción de privacidad (`invert_features.py`, RNF-25).

La frontera de confianza es **escalonada**: el tramo `píxel → características` se acredita por replicación externa con el extractor ONNX publicado por CID y verificado por SHA-256; el tramo `características → logits` queda acreditado por la prueba criptográfica verificada on-chain.

## Resultados medidos

Configuración vigente: extractor `MobileNetV3-small` ajustado sobre CarDD (8.740 parches) y posteriormente congelado junto con la cabeza lineal `576 → 6`.

| Dimensión | Circuito completo (descartado) | Features-afuera (vigente) |
| --- | --- | --- |
| Accuracy de test | 0,707 | **0,932** |
| macro-F1 | 0,705 | **0,948** |
| `logrows` | 21 | **15** |
| RAM pico de *proving* | 31,24 GB | **0,79 GB** |
| *Proving key* | 17,64 GB | **132,1 MB** |
| Tiempo de *prove* | 239,7 s | **4,2 s** |
| Gas de verificación (Anvil local) | ~938.697 | **1.141.679** |
| *Bytecode* del verificador | 28.276 B | **13.389 B** (entra en EIP-170) |
| Fidelidad circuito vs. flotante | 293/300 | **299/300** |

Umbrales de confianza medidos sobre las 785 muestras de prueba (`sweep_threshold.py`, RNF-15): **derivación humana en 0,90** y **liquidación automática en 0,95**. La verificación on-chain posterior sobre Base Sepolia midió entre `1.188.854` y `1.189.062` unidades de gas por prueba.

## Estructura

| Carpeta / archivo | Descripción |
| --- | --- |
| `fase0/` | Pipeline de viabilidad de circuito (sin entrenamiento): arquitectura → ONNX → calibración y compilación → SRS/setup → witness/prueba → verificación → generación de verificador Solidity (`step7`). |
| `fase1/` | Entrenamiento sobre CarDD, extracción de características, prueba zk de la cabeza, fidelidad, barrido de umbrales, prueba de inversión de privacidad y logs de ejecución. |
| `fase0_resultados.md` | Resultados y conclusiones de Fase 0 (inviabilidad de MobileNetV2 completa y techo de SmallCNN en circuito). |
| `fase1_resultados.md` | Resultados de entrenamiento y pruebas zkML de Fase 1. |

Scripts principales de `fase1/` en la arquitectura vigente:

| Script | Función |
| --- | --- |
| `extract_patches.py` | Recorta los 8.740 parches de daño de CarDD |
| `features_afuera.py` | Backbone congelado + cabeza lineal (línea base 87,5 % / 0,896) |
| `finetune_afuera.py` | Ajuste fino del backbone + cabeza (93,2 % / 0,948) |
| `prep_finetuned_proof.py` | Re-extrae características con el backbone ajustado y prepara la cabeza para `prove_head` |
| `prove_head.py` | Exporta la cabeza a ONNX, compila el circuito, genera la prueba y la verifica |
| `verify_onchain.py` | Despliega el verificador en `anvil`, verifica la prueba y mide gas y bytecode |
| `head_fidelity.py` | Evalúa fidelidad entre inferencia en circuito y punto flotante |
| `sweep_threshold.py` | Construye la curva umbral de confianza vs. tasa de derivación (RNF-15) |
| `export_backbone.py` | Exporta el extractor ONNX y calcula su SHA-256 para control de reproducibilidad |
| `invert_features.py` | Evalúa reconstrucción de imagen desde las características públicas (RNF-25) |

Los módulos `fase0/smallcnn.py`, `fase1/train.py` y `fase1/prove_trained.py` se conservan como registro experimental de la arquitectura de circuito completo descartada (techo de 70,7 % de *accuracy* y agotamiento de memoria en `bc32`).

## Ejecución

Requiere [`uv`](https://docs.astral.sh/uv/) y, para la verificación on-chain local, **Foundry** (`anvil`). El dataset CarDD y los pesos/checkpoints pesados no se versionan en git.

```shell
uv sync

# Flujo de la arquitectura vigente
uv run python -m fase1.extract_patches                      # parches de CarDD
uv run python -m fase1.finetune_afuera --eval-test          # backbone + cabeza
uv run python -m fase1.prep_finetuned_proof                 # características y cabeza para prueba
uv run python -m fase1.prove_head --ckpt fase1/checkpoints/head_mnv3_ft.pt
uv run python -m fase1.head_fidelity --ckpt fase1/checkpoints/head_mnv3_ft.pt
uv run python -m fase1.sweep_threshold                      # RNF-15

# Verificación on-chain local (con anvil levantado en otra terminal):
#   anvil --code-size-limit 200000 --silent
uv run python -m fase1.verify_onchain

# Regeneración del verificador Solidity para contracts/
uv run python -m fase0.step7 --resolution 576 --out-dir artifacts_head
```

## Restricciones de circuito

Todo lo que ingresa al circuito EZKL debe limitarse a operadores soportados: activación **ReLU6** (sin `h-swish`), **AvgPool global** (sin `MaxPool`), ausencia de bloques *squeeze-and-excitation* y salida en **logits**. Bajo el esquema *features-afuera*, esta restricción recae únicamente sobre la cabeza lineal.

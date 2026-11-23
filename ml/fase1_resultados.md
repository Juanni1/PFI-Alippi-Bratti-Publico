# Fase 1 — Entrenamiento y prueba zkML del clasificador de daños

> Continuación de [fase0_resultados.md](fase0_resultados.md). Fase 0 validó la **viabilidad** del circuito (arquitectura SmallCNN, resolución 64 px) con pesos random. Fase 1 construye el dataset real, **entrena** el clasificador y genera una **prueba zero-knowledge de la inferencia del modelo entrenado**. Stack igual: EZKL 23.0.5 (Halo2/KZG, BN254), ONNX opset 13, PyTorch.

## Objetivo de la fase

Pasar de "el circuito es viable" a "tenemos un clasificador entrenado, demostrable criptográficamente, dentro del presupuesto de hardware". Tres entregables:
1. Dataset de **parches de daño** recortados de CarDD.
2. **SmallCNN entrenada** + la tabla del trade-off **accuracy vs costo de circuito**.
3. **Prueba zk del modelo entrenado** sobre un parche real, verificada, y la medida de **fidelidad** circuito-vs-float.

## Pipeline implementado (paquete `fase1/`)

| Módulo | Qué hace |
|---|---|
| `extract_patches.py` | Recorta cada bbox de CarDD_COCO → parche etiquetado por clase (layout `ImageFolder`). |
| `train.py` | Entrena la SmallCNN; selección por macro-F1, pesos de clase, augmentation, normalización ImageNet. |
| `prove_trained.py` | Modelo entrenado → ONNX → circuito → setup → witness (parche real) → prove → verify. |
| `circuit_fidelity.py` | Mide en qué % la predicción en-circuito (punto fijo) coincide con la float. |

## 1 — Dataset de parches

El circuito prueba solo la **clasificación** del parche; la **localización** (encontrar el bbox) queda fuera. `extract_patches.py` recorta cada instancia anotada:

- **8.740 parches** total: train 6.211 · val 1.744 · test 785.
- 6 clases: dent, scratch, crack, glass shatter, lamp broken, tire flat.
- Se guardan a **tamaño nativo** (tope de lado 256 px, solo downscale) — NO a 64 px fijos. Así el mismo dataset sirve para entrenar a 48/64/96 px sin re-extraer; el resize + normalización van en el `transform`.
- Margen de contexto del 15 % alrededor del bbox.
- **Desbalance fuerte** (train): scratch 2.560 vs tire flat 225 → se compensa con pesos por clase en la loss.

## 2 — Entrenamiento, receta y curva accuracy vs circuito

SmallCNN entrenada a 64 px desde cero (AdamW, cosine). El parámetro de capacidad es `base_channels` (`bc`). Dos hallazgos:

### 2a — La receta de entrenamiento vale ~+9 pts a circuito FIJO

La primera receta (pesos de clase inversos, selección por macro-F1, 40 épocas) dejaba accuracy en la mesa: los pesos inversos castigaban a `scratch` (la clase mayoritaria, peso 0.21 → recall 0.47), bajando el número global. Receta nueva: **sin pesos de clase + selección por accuracy + label smoothing 0.1 + 60 épocas**. Sin agrandar el circuito (mismo logrows):

| `bc` (PC, logrows 21) | Test acc | Test macro-F1 |
|---|---|---|
| receta vieja | 0.611 | 0.647 |
| **receta nueva** | **0.707** | **0.705** |

**+9.6 pts de accuracy gratis** (mismo presupuesto de prueba), y macro-F1 sube también — no es a costa del balance. Flags en `fase1/train.py`: `--class-weights {inverse,sqrt,none}`, `--select-metric`, `--label-smoothing`.

### 2b — Curva accuracy vs presupuesto de circuito (receta nueva)

Barrido de capacidad (`bc`) × resolución, todo con la receta global-accuracy:

| Config | logrows | RAM de proving | Presupuesto | Test acc | Test macro-F1 |
|---|---|---|---|---|---|
| **bc8** @ 64 | **21** | ~30 GB | **PC 40 GB** ✅ | **0.707** | 0.705 |
| bc8 @ 96 | 22 | ~60 GB | VM 64 GB | 0.716 | 0.700 |
| **bc16** @ 64 | 22 | ~60 GB | VM 64 GB | **0.764** | 0.769 |
| bc16 @ 96 | 23 | ~120 GB | VM grande | 0.764 | 0.764 |

Dos conclusiones del barrido:
1. **La resolución da rendimientos decrecientes.** Subir 64→96 px sube la accuracy casi nada (bc8 +0.9 pts, bc16 **0**) pero cuesta **+1 logrows** (×2 RAM). **64 px fue la elección correcta** de Fase 0.
2. **La capacidad rinde más que la resolución a igual presupuesto.** A logrows 22, `bc16@64` (0.764) le gana a `bc8@96` (0.716): conviene gastar el presupuesto en ensanchar la red, no en agrandar la imagen.

Mejor punto en PC: **bc8@64 = 70.7%**. Mejor con VM: **bc16@64 = 76.4%** (no hace falta ir a 96 px). `crack` sigue siendo la clase más difícil (F1 0.56 bc8 → 0.65 bc16). El barrido se reproduce con `fase1/sweep_accuracy.py`; todo el entrenamiento corre en la PC (GPU), solo la *prueba zk* de logrows ≥ 22 necesita VM.

Nota de arquitectura: por el redondeo `_make_divisible(·, 8)`, no hay punto intermedio entre bc8 (8 canales de stem) y bc16 (16); para algo entre medio hay que tocar `width_mult` o el número de bloques.

> **Importante al medir el circuito:** correr `fase0.step3` con `--width-mult 1.0`, que es el default de la SmallCNN en entrenamiento. El default de `step3` es 0.5 y mide una red distinta a la que se entrena.

## 3 — Prueba zk del modelo ENTRENADO (hito)

`prove_trained.py` cierra el loop con pesos entrenados sobre un parche real (Fase 0 solo lo hizo con pesos random). Esquema de visibilidad: **imagen privada**, **logits públicos**, **pesos fijos** en el circuito → la prueba garantiza "corrí ESTE modelo sobre una imagen privada y obtuve ESTOS logits".

Resultado (bc8, res 64, en la PC de 40 GB):

| | |
|---|---|
| logrows | 21 |
| setup | RAM pico 29.7 GB · PK 17.6 GB · 304 s |
| prove | RAM pico 31.2 GB · prueba **34.5 KB** · 240 s |
| verificación | **VERIFY OK** ✅ |
| predicción en-circuito | **== predicción float** ✅ |

El pico de proving (31.2 GB) entra en la PC con ~9 GB de margen: **res 64 bc8 es demostrable sin VM**. La prueba pesa 34.5 KB (verificable en ms; luego on-chain en Solidity).

## 4 — Fidelidad circuito-vs-float

La prueba usa aritmética de **punto fijo** (scale 7); el modelo usa float32. `circuit_fidelity.py` mide si esa cuantización cambia la clase predicha, corriendo `gen_witness` sobre muchos parches y comparando el argmax en-circuito contra el float.

Resultado (bc8, scale 7, 300 parches de test):

| | |
|---|---|
| **Agreement** (argmax circuito == float) | **293/300 = 97.7 %** |
| Accuracy float (vs etiqueta real) | 0.657 |
| Accuracy en-circuito (vs etiqueta real) | 0.673 |

La cuantización de punto fijo preserva la predicción en el **97.7 %** de los casos. Los 7 desacuerdos caen **todos entre clases visualmente confundibles** (scratch ↔ dent, crack/glass_shatter ↔ scratch) — casos borderline donde dos logits están casi empatados y el ruido de cuantización inclina la balanza. Las clases distintivas (tire flat, lamp broken) no tienen ni un desacuerdo. La accuracy en-circuito (0.673) queda dentro del ruido de la float (0.657): la cuantización no degrada la calidad del clasificador.

Si el agreement bajara, la palanca es subir `param_scale`/`input_scale` (más bits de punto fijo → menos error, circuito algo más caro).

## 5 — Verificación on-chain (cierre del objetivo)

`fase0/step7.py` genera el **verifier Solidity** desde la VK + el calldata desde la prueba; `fase1/verify_onchain.py` lo **deploya y verifica en una EVM local** (anvil, vía Foundry). El loop completo cierra: modelo entrenado → prueba zk → **verificación en blockchain**.

| | |
|---|---|
| `verifier_r64.sol` | 132 KB (bytecode 28.276 B) |
| `calldata_r64.bytes` | 6.4 KB (la transacción de verificación) |
| **`verify_evm` on-chain** | **True** ✅ |
| **Gas de verificación** | **~938.697** |

**Caveat de mainnet:** el verifier monolítico (28.276 B de bytecode) **supera el límite EIP-170 de 24.577 B** → no deployaría en una chain real tal cual. anvil lo permite con `--code-size-limit`. Para producción hay que usar la **variante "reusable"** de EZKL (`create_evm_verifier(..., reusable=True)` + `create_evm_vka`, que separa la verification key en un contrato aparte). Es el siguiente ajuste si se quiere deploy real.

Requiere Foundry (`anvil`). En esta máquina está en `G:\foundry\`. Flujo:
```bash
anvil --code-size-limit 200000 --silent          # nodo local (en otra terminal)
uv run python -m fase0.step7 --resolution 64      # genera verifier.sol + calldata
uv run python -m fase1.verify_onchain --resolution 64   # deploy + verify + gas
```

## Entorno / hardware

- **GPU RTX 3080 (10 GB)** disponible. Entorno cambiado a `torch 2.12.1+cu126` (índice CUDA en `pyproject.toml`, reproducible con `uv sync`). El GPU acelera **poco** el entrenamiento (I/O-bound: decodificar JPGs es el cuello, el modelo es trivial).
- El GPU **no sirve** para el proving zkML: el cuello es RAM (PK 15-30 GB > 10 GB VRAM); EZKL prueba sobre CPU/RAM.

## Comandos

```bash
uv run python -m fase1.extract_patches                                        # 8.740 parches → fase1/patches/
uv run python -m fase1.train --epochs 40 --num-workers 8 --base-channels 8 --eval-test
uv run python -m fase0.step3 --arch smallcnn --base-channels 16 --width-mult 1.0 --resolution 64 --num-classes 6 --scales 7  # logrows del modelo
uv run python -m fase1.prove_trained --ckpt fase1/checkpoints/smallcnn_r64_bc8.pt   # prueba zk end-to-end
uv run python -m fase1.circuit_fidelity --ckpt fase1/checkpoints/smallcnn_r64_bc8.pt --num-samples 300
```

## Próximos pasos

- **Extender la curva a res 96** (`sweep_accuracy.py --configs 8:96 16:96`) si se quiere más detalle de imagen — agrega puntos de mayor presupuesto a la curva.
- **Verifier "reusable"** para deploy real en mainnet (sortear el límite EIP-170, ver sección 5).
- **Afinar `crack`** (la clase más débil, F1 0.56–0.65): es confundible con scratch; podría ayudar más resolución o augmentation dirigido.

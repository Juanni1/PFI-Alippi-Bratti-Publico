# Barrido accuracy-vs-presupuesto (SmallCNN, receta global-accuracy)

Receta: `--class-weights none --select-metric accuracy --label-smoothing 0.1 --epochs 60`.
Entrenamiento/eval en PC (GPU); la *prueba zk* de logrows ≥ 22 necesita VM.

| base_channels | resolución | logrows | test accuracy | test macro-F1 | presupuesto de prueba |
|---|---|---|---|---|---|
| 8 | 96 | 22 | 0.716 | 0.700 | VM (logrows 22) |
| 16 | 96 | 23 | 0.764 | 0.764 | VM (logrows 23) |

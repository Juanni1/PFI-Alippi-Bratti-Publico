# Barrido accuracy-vs-presupuesto (SmallCNN, receta global-accuracy)

Receta: `--class-weights none --select-metric accuracy --label-smoothing 0.1 --epochs 60`.
Entrenamiento/eval en PC (GPU); la *prueba zk* de logrows ≥ 22 necesita VM.

| base_channels | resolución | logrows | test accuracy | test macro-F1 | presupuesto de prueba |
|---|---|---|---|---|---|
| 8 | 64 | 21 | 0.707 | 0.705 | PC (~30 GB) |
| 16 | 64 | 22 | 0.764 | 0.769 | VM (logrows 22) |

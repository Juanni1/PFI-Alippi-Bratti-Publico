# RNF-15 — umbral de confianza contra tasa de derivación

Configuración: fase1/checkpoints/finetune_mnv3_all.pt | partición `test` (785 muestras, 6 clases)
Dispositivo: cuda | corrida: 2026-08-22 00:50:45 | 1.6 s

La precisión automática es la que se mide **solo sobre los casos que el sistema resolvió sin intervención**; los errores que escapan son esos mismos casos en número absoluto.

| umbral | tasa de derivación | cobertura | precisión automática | errores que escapan |
|---|---|---|---|---|
| 0.50 | 0.036 (28) | 0.964 | 0.946 | 41 |
| 0.55 | 0.054 (42) | 0.946 | 0.948 | 39 |
| 0.60 | 0.075 (59) | 0.925 | 0.952 | 35 |
| 0.65 | 0.098 (77) | 0.902 | 0.958 | 30 |
| 0.70 | 0.135 (106) | 0.865 | 0.966 | 23 |
| 0.75 | 0.173 (136) | 0.827 | 0.966 | 22 |
| 0.80 | 0.234 (184) | 0.766 | 0.967 | 20 |
| 0.85 | 0.331 (260) | 0.669 | 0.981 | 10 |
| 0.90 | 0.524 (411) | 0.476 | 0.984 | 6 |
| 0.95 | 0.859 (674) | 0.141 | 1.000 | 0 |
| 0.99 | 0.994 (780) | 0.006 | 1.000 | 0 |

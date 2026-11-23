# RNF-15 — umbral de confianza contra tasa de derivación

Configuración: fase1/checkpoints/finetune_mnv3_all.pt | partición `test` (785 muestras, 6 clases)
Dispositivo: cpu | corrida: 2026-08-22 01:06:14 | 3.6 s

La precisión automática es la que se mide **solo sobre los casos que el sistema resolvió sin intervención**; los errores que escapan son esos mismos casos en número absoluto.

| umbral | tasa de derivación | cobertura | precisión automática | errores que escapan |
|---|---|---|---|---|
| 0.50 | 0.036 (28) | 0.964 | 0.946 | 41 |
| 0.55 | 0.052 (41) | 0.948 | 0.948 | 39 |
| 0.60 | 0.074 (58) | 0.926 | 0.952 | 35 |
| 0.65 | 0.098 (77) | 0.902 | 0.958 | 30 |
| 0.70 | 0.135 (106) | 0.865 | 0.966 | 23 |
| 0.75 | 0.173 (136) | 0.827 | 0.966 | 22 |
| 0.80 | 0.236 (185) | 0.764 | 0.967 | 20 |
| 0.85 | 0.330 (259) | 0.670 | 0.979 | 11 |
| 0.90 | 0.524 (411) | 0.476 | 0.984 | 6 |
| 0.95 | 0.859 (674) | 0.141 | 1.000 | 0 |
| 0.99 | 0.994 (780) | 0.006 | 1.000 | 0 |

"""Capa de dominio del DCOMP: servicio de reclamos, servicio de evidencia
(hash, normalización y compromiso), cola de jobs, worker de procesamiento
(concurrencia 1), registro de modelos y servicio de configuración.

Escritos: `vocabulary` (estados, causas y textos, derivados de
`contract/states.yaml`), `state_machine` (matriz de adr-001, precedencia de
causa y clasificación), `claims` (registro, resolución, liquidación y cola),
`ports` (los puertos que los adaptadores satisfacen) y `worker` (las cinco
etapas del procesamiento, con concurrencia uno y sin sondeos)."""

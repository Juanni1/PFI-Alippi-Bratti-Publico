"""Capa de exposición del DCOMP: API REST (FastAPI), autorización RBAC
sobre JWT con filtrado por campo (adr-006), y servicio de traza y
artefactos públicos.

Escritos: `schemas` (un esquema de respuesta por perfil, espejo del contrato
OpenAPI), `security` (resolución de perfil sobre el token, RF-46 y RF-48) y
`api` (los nueve recursos). El recorte por perfil ocurre acá y no en el
cliente."""

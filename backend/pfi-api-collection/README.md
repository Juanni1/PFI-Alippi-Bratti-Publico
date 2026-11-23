# pfi-api-collection

Colección de [Bruno](https://usebruno.com) con las rutas de
`contract/openapi.yaml` más `/health`. Formato OpenCollection (archivos `.yml`,
Bruno 3.x): dos solicitudes usan la misma ruta `/reclamos`, una anónima y otra
para transcripción autenticada por operador.

## Entornos

Dos, en `environments/`, y son lo único que cambia entre una corrida local y
una contra el servicio desplegado:

| Entorno | `base_url` |
| --- | --- |
| `local` | `http://127.0.0.1:8000` |
| `produccion` | `https://api.ejemplo.invalid` (reemplazar por la instancia desplegada) |

Ninguna dirección literal en los pedidos (RNF-29). Elegir el entorno en el
desplegable de Bruno, o pasarlo por CLI:

```shell
npx @usebruno/cli run . -r --env local
```

Contra `produccion` el servicio puede estar suspendido: el primer pedido lo
despierta y tarda unos segundos más. Registrar un reclamo ahí **crea uno real**,
con su prueba y su publicación en IPFS; poblar la demostración es tarea de
`scripts/seed-demo-claims.py` y no de esta colección.

## Variables

| Variable | De dónde sale |
| --- | --- |
| `base_url` | del entorno |
| `token_revisor`, `token_operador` | sesiones internas por defecto (`SESIONES_INTERNAS`) |
| `token_reclamante` | la escribe «Registrar reclamo» al recibir la constancia |
| `numero` | ídem; para mirar otro reclamo se reemplaza a mano |
| `ruta_foto` | parche del conjunto de prueba de CarDD que se sube |

No hay login ni emisión de sesiones reales en el prototipo (adr-006);
`SESIONES_INTERNAS` resuelve los tokens conocidos y su perfil. **El perfil lo
determina el servidor y nunca lo declara el cliente** (RF-48). Presentar el
token del operador en la bandeja del revisor devuelve 403, y es la respuesta
correcta.

El bearer es opcional para registrar: sin encabezado el alta sigue siendo
anónima; si se presenta un token inválido, el servidor devuelve 401 y no cae al
modo anónimo. Revisor y token-capacidad de reclamante devuelven 403 antes de
publicar evidencia o crear filas. La solicitud del operador obtiene el autor de
su sesión; no envía un campo `registeredBy`.

El token del reclamante es la capacidad de RF-47, emitida para un expediente y
no derivable del número de seguimiento. Viaja en claro una sola vez, en la
constancia devuelta por cualquiera de las dos variantes; cada solicitud guarda
ese token en el entorno para las consultas posteriores.

## Orden de la corrida

`Configuración pública` en la carpeta público se puede ejecutar antes del
registro, sin bearer. Solo devuelve `maxFiles`, `maxFileSizeMb`, `amountLimit`
y `acceptedFormats`; la consulta de configuración completa en la carpeta común
sigue requiriendo una sesión o capacidad válida.

`Registrar reclamo` en la carpeta reclamante deja `numero` y `token_reclamante`
para todo lo demás. El pedido en la carpeta operador realiza la misma ruta como
una segunda variante de registro: ejecutar toda la colección crea un reclamo
anónimo y otro registrado por operador. Úsese solo cuando se quiera probar ese
flujo adicional.

Después del registro, el estado y la explicación se consultan como reclamante,
la bandeja y la resolución como revisor, y la cola y la liquidación como
operador.

Corriendo la colección entera de una vez, la liquidación devuelve 409 y está
bien: el revisor ya resolvió el reclamo dos pedidos antes, y liquidar solo
procede desde `Aprobable`. Para ejercer esa ruta hay que apuntar `numero` a un
reclamo que esté en ese estado, que es lo que lista `GET /cola`.

Los 409 de la explicación y de la traza pública tienen otra causa: el reclamo
todavía está en la cola y no hay prueba que exhibir.

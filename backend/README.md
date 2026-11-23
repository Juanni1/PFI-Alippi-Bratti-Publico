# Backend — API, Worker y Adaptadores

Backend del prototipo de gestión de siniestros (Alippi y Bratti, UADE): API REST de la aseguradora, worker de procesamiento asincrónico y adaptadores hacia PostgreSQL, Kubo/Pinata (IPFS), ONNX Runtime, EZKL y la red EVM.

## Comandos principales

```shell
uv sync                                  # entorno (Python 3.12)
# DATABASE_URL debe identificar PostgreSQL local; las credenciales van por entorno.
uv run alembic upgrade head              # migración explícita antes de arrancar
uv run uvicorn app.main:app --reload     # servidor de desarrollo
# TEST_DATABASE_URL debe identificar una base aislada, nunca producción.
uv run pytest -q                         # suite sobre PostgreSQL real
```

## Estructura por capas

`app/` organiza la aplicación en tres capas con inversión de dependencias:

- `app/exposicion/`: endpoints FastAPI, autorización por perfil y esquemas Pydantic derivados del contrato `contract/openapi.yaml`.
- `app/dominio/`: máquina de estados, reglas de negocio, vocabulario canónico (`contract/states.yaml`) y puertos (`dominio/ports.py`).
- `app/adaptadores/`: persistencia relacional (SQLAlchemy / PostgreSQL + Alembic), cliente IPFS (Kubo + Pinata), inferencia (ONNX Runtime), generación de pruebas (EZKL), cliente on-chain (`web3.py`) y coordinador durable del oráculo.

El contrato de frontera se mantiene explícitamente en `contract/openapi.yaml` y `contract/states.yaml`, y la matriz de visibilidad por actor (RF-46) se verifica de forma mecánica mediante:

```shell
uv run python contract/check_visibility_matrix.py
```

## Adaptadores por entorno

La persistencia siempre requiere PostgreSQL (`DATABASE_URL` con `postgresql+psycopg://`). Fuera de producción, los adaptadores de inferencia, prueba y cadena conservan implementaciones simuladas por defecto; el publicador usa IPFS por defecto y cambia a disco local solo con `PUBLICADOR_EVIDENCIA=local`. Un adaptador real solicitado falla explícitamente si falta su configuración, sin degradar silenciosamente al simulador.

| Variable | Implementación real | Requisitos |
| --- | --- | --- |
| `MOTOR_INFERENCIA=onnx` | `OnnxInferenceEngine` | `RUTA_ARTEFACTOS` con el extractor y la cabeza ONNX |
| `GENERADOR_PRUEBAS=ezkl` | `EzklProofGenerator` | `RUTA_ARTEFACTOS` con circuito, SRS y ajustes |
| `CLIENTE_CADENA=web3` | `Web3ChainClient` | RPC, `CADENA_DESPLIEGUE` y `CADENA_CLAVE_ORACULO` |

Ver `.env.example` para el listado completo de variables de entorno.

### Ejecución contra cadena local (Hardhat)

Desde `contracts/`, levantar el nodo local y desplegar los contratos:

```shell
npx hardhat node
npx hardhat run scripts/deploy-local.js --network localhost
```

Luego, desde `backend/`, usando la cuenta cero de Hardhat como oráculo (clave de prueba pública y conocida; nunca usar una cuenta con fondos reales):

```shell
CLIENTE_CADENA=web3 \
CADENA_CLAVE_ORACULO=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 \
MOTOR_INFERENCIA=onnx GENERADOR_PRUEBAS=ezkl RUTA_ARTEFACTOS=artefactos \
uv run uvicorn app.main:app
```

Por defecto `CADENA_RPC` apunta a `http://127.0.0.1:8545` y `CADENA_DESPLIEGUE` a `../contracts/despliegue-local.json`. Al seleccionar `CLIENTE_CADENA=web3`, el arranque valida el artefacto confirmado: coincidencia de `chainId` con la RPC, identidad del oráculo y del verificador contra el registro, existencia de código y, cuando el artefacto incluye `codigo`, coincidencia de hashes keccak de bytecode. Un artefacto con `estado: pendiente` impide iniciar el cliente.

El nodo de Hardhat no persiste estado entre reinicios: al reiniciarlo corresponde redesplegar los contratos y vaciar la base local para evitar divergencias entre base y cadena.

## Persistencia, migraciones y respaldos

`DATABASE_URL` es obligatoria y no existe fallback a SQLite. El arranque verifica que la revisión de Alembic coincida con `migrations/versions/` sin alterar el esquema automáticamente.

No existe tabla de usuarios en el prototipo: `SESIONES_INTERNAS` define los tokens y perfiles internos (`token:perfil:identidad`) exclusivamente por variable de entorno.

### Respaldo y restauración PostgreSQL

Requiere `pg_dump` y `pg_restore` versión 17 o superior (la imagen Docker incluye cliente 18). Las credenciales se transmiten por entorno y los archivos generados reciben permisos `0600`:

```shell
uv run python -m app.adaptadores.data_migration backup-postgres --output "$DUMP_PATH"
sha256sum "$DUMP_PATH"
# Sobre una base PostgreSQL vacía, sin ejecutar Alembic previamente:
uv run python -m app.adaptadores.data_migration restore-postgres --input "$DUMP_PATH"
uv run python -c \
  "from app.adaptadores.database import create_database_engine, verify_schema; verify_schema(create_database_engine())"
```

### Herramienta de migración histórica desde SQLite

Para migrar un respaldo legado de SQLite hacia una base PostgreSQL vacía:

```shell
uv run python -m app.adaptadores.data_migration snapshot-sqlite \
  --source "$LEGACY_SQLITE_PATH" --target "$SNAPSHOT_PATH"
sha256sum "$SNAPSHOT_PATH"
uv run alembic upgrade head
uv run python -m app.adaptadores.data_migration import-sqlite --source "$SNAPSHOT_PATH"
```

## Transacciones cortas, cola durable y oráculo

### Checkpoints y transacciones cortas

El procesamiento asincrónico divide la ejecución en etapas con sesiones breves de PostgreSQL: IPFS, ONNX, EZKL y las llamadas RPC ocurren fuera de transacciones abiertas. Cada job congela `configuration_id` al adquirirse y persiste `inference_id`, `proof_id` y `checkpoint_context`, reutilizando resultados únicamente cuando coinciden evidencia, artefactos, motor y parámetros de configuración.

La explicación (`GET /reclamos/{numero}/explicacion`) y la traza pública (`GET /traza/{numero}`) responden `409` hasta que una transición de clasificación vincula la prueba correspondiente.

### Oráculo durable y reconciliación

La migración `0003_oracle_operations` introduce las tablas `oracle_operations` y `oracle_wallets` para coordinar emisiones hacia la EVM sin perder nonces ni duplicar transacciones:

1. **Exclusión de billetera:** combina reserva durable en `oracle_wallets` (`active_operation_id`) con `pg_advisory_lock` por red y dirección sobre conexión dedicada, sin mantener transacciones abiertas durante llamadas RPC.
2. **Persistencia previa al envío:** registra intención inmutable (`preparada`), nonce y bytes firmados (`firmada`) y checkpoint previo al broadcast (`enviada`).
3. **Aplicación atómica del recibo:** al confirmarse el minado, una transacción corta asienta la transición, el estado del reclamo, el vínculo de `Review` (en resoluciones humanas) y libera la billetera. Ante fallos de verificación de prueba, el contrato asienta `VerificacionFallida` sin revertir.
4. **Rechazo previo a firma:** un rechazo determinista antes de firmar marca `fallida_pre_envio` (`CONTRACT_REJECTED_BEFORE_SIGNING`) y libera la reserva sin inventar un recibo.
5. **Incertidumbre:** ante timeout o error de red, la operación queda en `incierta` y retiene el bloqueo de billetera hasta su reconciliación verificable, sin reintentos ciegos con nuevos nonces.
6. **Monitoreo y reconciliación:**
   - `GET /cola` (solo operador): informa `oracleBlocked` y las operaciones pendientes sin exponer secretos ni bytes firmados.
   - `POST /cola/reconciliacion` (solo operador): recibe estrictamente `{"operationId": "<UUID>"}`.
   - CLI acotado:
     ```shell
     uv run python -m app.adaptadores.reconcile --limit 50
     uv run python -m app.adaptadores.reconcile --operation-id "<UUID>"
     ```

### Servicio worker separado (Redis / Celery)

El procesamiento puede desacoplarse en un servicio `worker` usando la misma imagen Docker y `railway.worker.toml`. Consume la cola `pfi_jobs` con Celery `prefork` y verificación previa al arranque (`WORKER_CONCURRENCY`, broker explícito, esquema PostgreSQL vigente y consistencia con `queue_control.concurrency`). El reconciliador de leases y outbox corre únicamente en el supervisor del worker, sin agregar sondeos salientes en el proceso API.

## Endpoints destacados del contrato

### Registro de reclamos y descripción del daño

- `GET /configuracion/publica`: devuelve sin autenticación únicamente `maxFiles`, `maxFileSizeMb`, `amountLimit` y `acceptedFormats`. `GET /configuracion` exige sesión interna o capacidad válida de reclamante.
- `POST /reclamos`: exige `damageDescription` (normalizando `CRLF`/`CR` a `LF` y recortando espacios extremos, entre 1 y 2000 caracteres). El relato se persiste solo en PostgreSQL (`claims.damage_description`), fuera de IPFS, la cadena y la traza pública.
  - Sin encabezado `Authorization`: alta anónima (`registeredBy: null`).
  - Con `Authorization: Bearer <token>`: exige perfil `OPERADOR` (token inválido → `401`, otro perfil → `403`) y persiste `registered_by` desde la sesión del servidor.

### Fotografías originales para revisión

`GET /reclamos/{numero}/evidencia/{evidence_file_id}` entrega los bytes originales de cada archivo verificado desde Kubo exclusivamente a sesiones de `REVISOR` u `OPERADOR` (nunca al reclamante titular, que recibe `403`).

| Resultado | Respuesta |
| --- | --- |
| Original íntegro | `200`, MIME persistido (`image/png` o `image/jpeg`), `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` |
| Token ausente, inválido o de otro reclamo | `401` |
| Reclamante titular | `403` |
| Archivo ausente o de otro reclamo | `404` |
| Fallo de recuperación o divergencia de tamaño/SHA-256 | `502` |
| Timeout de recuperación | `504` |

La sesión SQL se cierra antes del I/O con Kubo y no se reenvía el bearer interno hacia servicios externos. Nota: este endpoint no hace confidenciales los archivos ya publicados en IPFS por su CID.

Suite acotada de evidencia fotográfica (con `TEST_DATABASE_URL` y opcionalmente `TEST_KUBO_API_URL`):

```shell
uv run --no-sync pytest -q tests/test_photo_evidence.py tests/test_photo_evidence_kubo.py tests/test_ipfs_client.py tests/test_local_storage.py
```

## Validación de ciclo de vida y gate RPC (`scripts/`)

- `scripts/pfi165_rpc_gate.py`: proxy JSON-RPC local de salvaguarda que inspecciona transacciones ya firmadas antes del broadcast (verificando firma, wallet, red, contrato, selectores permitidos, nonce y presupuesto en wei con cotización L2 + L1 + operador en `GasPriceOracle`) y soporta inyección controlada de pérdida de respuesta (`--lose-response-once-at N`) para ejercitar la reconciliación.
- `scripts/pfi165_lifecycle.py`: planifica y ejecuta en entorno aislado la prueba integral de ciclo de vida y reconciliación contra cadena local (`31337`) o Base Sepolia (`84532`) utilizando el registro de ancla `config/pfi165-approved-deployment.json` y el artefacto confirmado `config/base-sepolia-deployment.json`. Por defecto solo genera el plan sin credenciales ni escrituras.

## Colección de la API

`pfi-api-collection/` contiene una colección de Bruno con todas las rutas del contrato y `/health`, con entornos `local` y `produccion` (este último con placeholders para completar según el despliegue). Consultar [`pfi-api-collection/README.md`](pfi-api-collection/README.md).

## Despliegue

El repositorio incluye `Dockerfile`, `railway.toml` (servicio `api`, con migración previa `alembic upgrade head`, healthcheck `/health` y `sleepApplication = true`) y `railway.worker.toml` (servicio `worker` con Celery).

En entorno de producción (`RAILWAY_ENVIRONMENT_NAME=production`), `CLIENTE_CADENA=web3` es obligatorio junto con `CADENA_DESPLIEGUE=/app/config/base-sepolia-deployment.json`, `CADENA_RPC` y `CADENA_CLAVE_ORACULO` suministrada únicamente como secreto de entorno. El servidor abre socket dual-stack en `app/run.py` para compatibilidad simultánea con healthchecks IPv4 y red privada IPv6.

# Frontend — SPA Veredic

Aplicación web de página única (SPA) del prototipo de gestión de siniestros (Alippi y Bratti, UADE): implementa las nueve vistas (`MK-01` a `MK-09`) con navegación segmentada por perfil de usuario (Asegurado, Operador, Revisor y Auditoría pública), sin librerías Web3 ni billeteras en el navegador.

**Stack:** React, TypeScript, Vite, Tailwind CSS, shadcn/ui, TanStack Query, Zod y Mock Service Worker (MSW).

## Comandos

```shell
npm install      # instalar dependencias
npm run dev      # servidor de desarrollo (MSW intercepta la API si no hay VITE_API_URL)
npm run build    # chequeo de tipos y build estático (tsc + vite)
npm run preview  # servir el build generado
```

## Configuración

- `VITE_API_URL`: URL base del backend (sin barra final), inyectada en tiempo de build (`import.meta.env`). Sin esta variable, el entorno de desarrollo funciona contra los mocks locales de MSW.
- `VITE_USE_MOCKS`: `true` fuerza MSW; `false` lo desactiva y exige `VITE_API_URL`. No incluir `VITE_USE_MOCKS=true` en builds de producción salvo para demostraciones deliberadas con fixtures.

## Perfiles y recorridos principales

La aplicación implementa cuatro perfiles con vistas y datos recortados según la matriz de visibilidad del contrato (`backend/contract/openapi.yaml`):

### Registro y seguimiento (Asegurado y Operador)

- **Asegurado (`/`):** consulta `GET /configuracion/publica` sin token para límites de carga y monto, normaliza saltos de línea (`CRLF`/`CR` → `LF`) y envía `POST /reclamos` de forma anónima. Al confirmar, muestra la constancia (`/constancia/:numero`) con el enlace de seguimiento y el token de capacidad en el fragmento (`/estado/:numero#acceso=...`), más la explicación del resultado (`/explicacion/:numero`) y el historial local (`/mis-reclamos`).
- **Operador (`/` y `/monitor`):** con sesión de operador activa, el mismo formulario permite transcribir reclamos de terceros enviando el token bearer; el backend registra la identidad del operador desde la sesión y la expone únicamente en el detalle interno (`/resolucion/:numero`), nunca en la constancia ni en la traza pública.

### Revisión humana y galería de evidencia fotográfica (`MK-05` y `MK-06`)

- **Revisor (`/revision` y `/resolucion/:numero`):** accede a la bandeja de casos derivados y al detalle interno del expediente con galería de fotografías originales servidas por el backend mediante `GET /reclamos/{numero}/evidencia/{evidence_file_id}`.
- Las rutas relativas de evidencia se solicitan con el bearer de la sesión interna (omitiendo cookies, *referer* y caché, rechazando redirecciones y con `AbortSignal` de 15 s) y se renderizan mediante blobs locales revocados al desmontar o cambiar de sesión.
- Cada miniatura gestiona de forma independiente sus estados de carga/error y dispone de un modal de ampliación accesible por teclado (`Enter`, `Tab`, `Escape` y retorno de foco al control de origen).

### Monitor de cola y reconciliación del oráculo (`MK-07`)

- Exclusivo del perfil **Operador** (`/monitor`).
- Cuando la billetera del oráculo tiene operaciones pendientes o ambiguas (`oracleBlocked: true`), muestra el aviso «Billetera del oráculo bloqueada» y lista cada operación (`register_claim`, `submit_inference`, `resolve`, `settle`) con estado (`preparada`, `firmada`, `enviada`, `incierta`), hash copiable, bloque y código de error seguro.
- El control «Reconciliar» dispara `POST /cola/reconciliacion` con `{"operationId": "<UUID>"}`, refrescando la cola ante confirmación y conservando el estado incierto cuando la transacción aún no fue minada.

### Auditoría pública (`MK-08`)

- Acceso abierto sin credenciales en `/traza/:numero`. Expone únicamente el historial de transiciones verificables, identificadores de evidencia/artefactos y datos del circuito, ocultando explícitamente datos privados (monto declarado, imágenes, relato del daño, identidad del operador/revisor y justificación humana).

## Fixtures sintéticos y verificación de interfaz

En modo MSW, la galería utiliza tres ilustraciones PNG sintéticas deterministas en `public/evidence-fixtures/`, regenerables con:

```shell
node tests/generate-evidence-fixtures.mjs
```

La prueba automatizada de la galería y controles de `MK-06` corre sobre Chromium headless contra MSW y verifica renderizado, proporciones, navegación por teclado/foco, entrega autenticada del bearer, manejo de errores, cancelación y revocación de blobs (requiere `/usr/bin/chromium`):

```shell
node scripts/test-evidence-gallery.mjs
npm run build
```

## Despliegue

El bundle estático (`dist/`) se publica en Cloudflare Pages (o cualquier hosting estático con fallback SPA a `index.html`) construyendo con `VITE_API_URL` apuntando al servicio backend:

```shell
VITE_API_URL=https://<host-del-backend> npm run build
```

Al desplegar en Cloudflare Pages con `wrangler`, utilizar `--branch=main` cuando se construye desde un tag (HEAD desacoplado) para publicar en producción en lugar de una vista previa.

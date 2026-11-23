# Imagen del servicio "api". Base x86_64: la ruta de producción del
# extractor se fija a esa arquitectura (RNF-39).
FROM python:3.12-slim

# Cliente PostgreSQL 18: compatible con el servidor 18 de Railway y con PostgreSQL 17.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    && install -d /etc/apt/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo "$VERSION_CODENAME")-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Dependencias primero, para cachear la capa.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"


COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations


# `contract/` no es documentación: `states.yaml` se lee en tiempo de ejecución
# como fuente del vocabulario de estados y causas, y sin él la aplicación
# arranca pero falla al primer reclamo. Se copia a la imagen por eso.
COPY contract ./contract
COPY config/base-sepolia-deployment.json ./config/base-sepolia-deployment.json

# Artefactos del modelo. Entran a la imagen por la excepción registrada en el
# .gitignore: son 3,6 MB y son lo que hace que el sistema desplegado clasifique
# con el modelo real y no con el simulado. `MOTOR_INFERENCIA=onnx` y
# `RUTA_ARTEFACTOS=/app/artefactos` son las dos variables que lo activan.
COPY artefactos ./artefactos

# Railway inyecta PORT; en local cae a 8000. El lanzador abre un socket
# dual-stack (IPv4 + IPv6): la red privada de Railway es solo IPv6 y el
# healthcheck puede entrar por cualquiera de las dos familias.
CMD uv run --no-sync python -m app.run

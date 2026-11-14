# Dockerfile «Партнера» (M8.1): сервер FastAPI + CLI в одном образе.
# Сборка: docker build -t partner:VERSION -f deploy/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# postgresql-client нужен для pg_dump/pg_restore (репетиция восстановления,
# M8.3); curl — для healthcheck и ожидания готовности сервисов.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE alembic.ini ./
COPY apps ./apps
COPY migrations ./migrations
RUN pip install --no-deps . && pip install ".[server]"

# policy.yaml клиента и legal.yaml монтируются томами (legal.yaml не в git —
# legal-module.md §6). Миграции: сервис migrate (`alembic upgrade head`).
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:8080/healthz || exit 1

CMD ["uvicorn", "partner_server.main:app", "--host", "0.0.0.0", "--port", "8080"]

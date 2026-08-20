FROM python:3.12-slim AS base

ARG APP_SOURCE_COMMIT=unknown
ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.revision=${APP_SOURCE_COMMIT}

WORKDIR /app

RUN printf '%s\n' "$APP_SOURCE_COMMIT" > /app/.source-commit \
    && chmod 0444 /app/.source-commit

RUN groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home --shell /usr/sbin/nologin app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY alembic ./alembic

FROM base AS test

RUN pip install --no-cache-dir ".[dev]"

COPY Dockerfile docker-compose.yml docker-compose.integration.yml ./
COPY scripts ./scripts
COPY tests ./tests

USER app

CMD ["python", "-m", "pytest", "-q", "-m", "not integration and not jetson"]

FROM base AS runtime

USER app

CMD ["uma-st-2"]

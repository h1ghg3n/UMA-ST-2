FROM python:3.13-slim-bookworm AS wheel-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .


FROM python:3.13-slim-bookworm AS runtime

ARG SOURCE_COMMIT=unrecorded

LABEL org.opencontainers.image.title="UMA-ST-2" \
      org.opencontainers.image.source="https://github.com/h1ghg3n/UMA-ST-2" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}"

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 uma-st2 \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin uma-st2

WORKDIR /app

COPY --from=wheel-builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels uma-st-2 \
    && rm -rf /wheels

COPY --chown=10001:10001 alembic.ini ./alembic.ini
COPY --chown=10001:10001 alembic ./alembic

USER 10001:10001

CMD ["uma-st-2"]

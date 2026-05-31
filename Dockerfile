# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ src/

RUN python -m pip install --upgrade pip \
    && pip install --prefix=/install .

FROM python:3.12-slim AS runtime

ARG UID=1000
ARG GID=1000

ENV NAUMI_CONFIG=/app/config.yaml \
    NAUMI_BOOTSTRAP=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${GID}" naumi \
    && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /bin/bash naumi

COPY --from=builder /install /usr/local
COPY docker/entrypoint.sh /usr/local/bin/naumi-entrypoint

RUN chmod +x /usr/local/bin/naumi-entrypoint \
    && python -m playwright install --with-deps chromium \
    && mkdir -p /app/data /workspace /ms-playwright \
    && chown -R naumi:naumi /app /workspace /ms-playwright /home/naumi

USER naumi

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=3)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "naumi-entrypoint"]
CMD ["naumi", "serve", "--host", "0.0.0.0", "--port", "8080", "--config", "/app/config.yaml"]

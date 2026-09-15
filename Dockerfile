# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .

RUN python -m pip install --upgrade pip \
    && python - <<'PY'
import subprocess
import sys
import tomllib

with open("pyproject.toml", "rb") as stream:
    dependencies = tomllib.load(stream)["project"]["dependencies"]
subprocess.check_call(
    [sys.executable, "-m", "pip", "install", "--prefix=/install", *dependencies]
)
PY

COPY src/ src/
COPY frontend/terminal-ui/package.json \
    frontend/terminal-ui/protocol-contract.json \
    frontend/terminal-ui/capability-manifest.json \
    frontend/terminal-ui/terminal-capability-contract.json \
    frontend/terminal-ui/terminal-width-contract.json \
    frontend/terminal-ui/
COPY frontend/terminal-ui/src/ frontend/terminal-ui/src/

RUN pip install --prefix=/install --no-deps .

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

RUN python -m pip install --no-cache-dir "playwright>=1.50" \
    && python -m playwright install --with-deps chromium \
    && mkdir -p /app/data /workspace /ms-playwright \
    && chown -R naumi:naumi /app /workspace /ms-playwright /home/naumi

COPY --from=builder /install /usr/local
COPY docker/entrypoint.sh /usr/local/bin/naumi-entrypoint

RUN sed -i 's/\r$//' /usr/local/bin/naumi-entrypoint \
    && chmod +x /usr/local/bin/naumi-entrypoint

USER naumi

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=3)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "naumi-entrypoint"]
CMD ["naumi", "serve", "--host", "0.0.0.0", "--port", "8080", "--config", "/app/config.yaml"]

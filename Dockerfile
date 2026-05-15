# --- Build stage ---
FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir --prefix=/install .

# --- Runtime stage ---
FROM python:3.12-slim

RUN useradd --create-home naumi
WORKDIR /app

COPY --from=builder /install /usr/local
COPY src/ src/

RUN mkdir -p data && chown naumi:naumi data

USER naumi

EXPOSE 8080

ENV NAUMI_MODELS__API_KEY=""
ENV NAUMI_MODELS__API_BASE=""

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/v1/health')" || exit 1

CMD ["naumi", "serve", "--host", "0.0.0.0", "--port", "8080"]

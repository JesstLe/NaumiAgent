FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir -e "." && \
    python -m playwright install --with-deps chromium

COPY config.yaml .

RUN mkdir -p data

EXPOSE 8080

ENV NAUMI_MODELS__API_KEY=""
ENV NAUMI_MODELS__API_BASE=""

CMD ["naumi", "serve", "--host", "0.0.0.0", "--port", "8080"]

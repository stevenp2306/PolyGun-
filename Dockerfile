# polybot image. Contains NO secrets: they are provided at runtime via env_file / secrets.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 polybot

WORKDIR /app
COPY pyproject.toml README.md ./
COPY polybot ./polybot
RUN pip install --upgrade pip && pip install .

USER polybot
RUN mkdir -p /app/data /app/logs /app/secrets
VOLUME ["/app/data", "/app/logs"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["polybot", "run"]

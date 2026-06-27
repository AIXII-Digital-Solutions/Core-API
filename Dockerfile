# core-api — FastAPI gateway + owner of the main DB schema (db-contract + migrations).
# Build: docker build -t core-api:latest .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# deps first for layer caching
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# source (app/, db-contract/, migration/, alembic.ini, tools/, entrypoint.sh)
COPY . .

# data/log dirs + non-root user
RUN mkdir -p /app/api_data /app/Logs \
    && useradd -m -u 10001 appuser \
    && chown -R appuser:appuser /app \
    && chmod +x entrypoint.sh
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/ || exit 1

ENTRYPOINT ["./entrypoint.sh"]

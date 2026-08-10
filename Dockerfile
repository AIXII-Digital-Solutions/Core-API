# core-api — FastAPI gateway + owner of the main DB schema (db-contract + migrations).
# Build: docker build -t core-api:latest .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl unzip \
    && rm -rf /var/lib/apt/lists/*

# --- Bitwarden CLI, for SECRETS_BACKEND=vaultwarden (see docs/secrets.md) --------------------
# PINNED ON PURPOSE. The CLI is normally installed from an unversioned "latest" URL, so an image
# rebuild silently upgrades it — and CLI 2026.7.0 moved to a strict WASM SDK that REJECTS the cipher
# payload served by Vaultwarden < 1.37.0. Every `bw get` then dies with
# `invalid type: JsValue(Object({...})), expected a string` and this container, which resolves its
# database credentials at startup, goes into a restart loop.
#
# 2026.6.0 is the last release before that change, so it works against Vaultwarden both older and
# newer than 1.37.0. Record the Vaultwarden version this pin is validated against in docs/secrets.md
# whenever you move it, and wipe the state volume after a bump — an older CLI cannot read a newer
# one's data.json.
ARG BW_VERSION=2026.6.0
# Supply-chain pin. GitHub publishes no checksum file for these assets, so these were computed from
# the released artefacts; they MUST be updated together with BW_VERSION. Override BW_SHA256 to pin a
# different build, or set it to "-" to skip verification (do not do that in production).
ARG BW_SHA256_AMD64=392549496c712ab86bfbd6c27302df9fd2c431cfc7a47e26941ac3e3893f4d27
ARG BW_SHA256_ARM64=626156e0ca60606c85b5b8ede0dd4e546b886a36e7f827b81d8cd5b8b487ee7c
ARG BW_SHA256=""
ARG TARGETARCH
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) BW_ASSET="bw-linux-${BW_VERSION}.zip";       BW_SUM="${BW_SHA256:-}"; \
             [ -n "$BW_SUM" ] || BW_SUM="${BW_SHA256_AMD64}" ;; \
      arm64) BW_ASSET="bw-linux-arm64-${BW_VERSION}.zip"; BW_SUM="${BW_SHA256:-}"; \
             [ -n "$BW_SUM" ] || BW_SUM="${BW_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/bw.zip \
      "https://github.com/bitwarden/clients/releases/download/cli-v${BW_VERSION}/${BW_ASSET}"; \
    if [ "$BW_SUM" = "-" ]; then echo "WARNING: bw checksum verification skipped" >&2; \
    else echo "${BW_SUM}  /tmp/bw.zip" | sha256sum -c -; fi; \
    unzip -q /tmp/bw.zip -d /usr/local/bin; \
    rm /tmp/bw.zip; \
    chmod +x /usr/local/bin/bw; \
    /usr/local/bin/bw --version

# Deterministic path (no PATH surprises) and a state dir that is NOT the app bind mount: `bw` keeps
# login state and an encrypted vault cache there, so it gets its own named volume in compose.
ENV BW_CLI_PATH=/usr/local/bin/bw \
    BW_APPDATA_BASE=/var/lib/aixii/bw

WORKDIR /app

# deps first for layer caching
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# source (app/, db-contract/, migration/, alembic.ini, tools/, entrypoint.sh)
COPY . .

# data/log dirs + non-root user
RUN mkdir -p /app/api_data /app/Logs /var/lib/aixii/bw \
    && useradd -m -u 10001 appuser \
    && chown -R appuser:appuser /app /var/lib/aixii \
    && chmod 700 /var/lib/aixii/bw \
    && chmod +x entrypoint.sh
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health/ || exit 1

ENTRYPOINT ["./entrypoint.sh"]

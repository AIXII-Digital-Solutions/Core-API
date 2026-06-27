"""
api_server segment configuration.

Holds only the settings used by the HTTP API process (server host/port, FastAPI
app metadata, CORS, the custom Router, request-path file folders and the few
domain constants used by routers). Common settings (DBSettings, logging,
DEV_MODE, ROOT) come from the shared ``Config`` package.
"""
import os
from pathlib import Path

# --- Load THIS service's own .env before importing the shared Config ---------
# Each segment owns its environment file (repo-root .env[.dev]); they
# do NOT share a single root .env. We point the shared Config at our file via
# ENV_PATH / ENV_DEV_PATH (which Config already honours) and let it do the
# actual load_dotenv. In containers the vars are usually injected directly
# (compose env_file / --env-file), in which case no file is needed here.
_SERVICE_ROOT = Path(__file__).resolve().parents[1]
_DEV = os.getenv("DEV_MODE", "false").lower() in ("1", "true", "yes", "on")
_ENV_VAR = "ENV_DEV_PATH" if _DEV else "ENV_PATH"
if not os.getenv(_ENV_VAR):
    _env_file = _SERVICE_ROOT / (".env.dev" if _DEV else ".env")
    if _env_file.exists():
        os.environ[_ENV_VAR] = str(_env_file)
# -----------------------------------------------------------------------------

from fastapi import APIRouter

from Config import require_env, ROOT

# SERVER

HOST: str = require_env("HOST", "0.0.0.0")
PORT: int = int(require_env("PORT", 8000))

SELF_HOST: str = require_env("SELF_HOST", "api.aixii.com")
SELF_PORT: int = int(require_env("SELF_PORT", 8000))


# API

class Router(APIRouter):
    def add_api_route(self, path: str, endpoint, **kwargs):
        if path.endswith("/"):
            alt_path = path[:-1]
        else:
            alt_path = path + "/"

        super().add_api_route(path, endpoint, **kwargs)

        alt_kwargs = kwargs.copy()
        alt_kwargs["include_in_schema"] = False

        super().add_api_route(alt_path, endpoint, **alt_kwargs)


API_TITLE: str = require_env("API_TITLE", "AIXII API Server")
API_DESCRIPTION: str = require_env("API_DESCRIPTION", "")
API_VERSION: str = require_env("API_VERSION", "v1.0.5a")
API_SWAGGER_URL: str = require_env("API_SWAGGER_URL", "/api/docs")
API_REDOC_URL: str = require_env("API_REDOC_URL", "/api/redoc")
API_ROOT_URL: str = require_env("API_ROOT_URL", "/api/v1")
API_OPENAPI_VERSION: str = require_env("API_OPENAPI_VERSION", "3.0.2")

# CORS

CORS_ORIGINS: list = require_env("CORS_ORIGINS", "*").split(",")
CORS_CREDENTIALS: bool = str(require_env("CORS_CREDENTIALS", True)).lower() in ("1", "true", "yes", "on")
CORS_METHODS: list = require_env("CORS_METHODS", "*").split(",")
CORS_HEADERS: list = require_env("CORS_HEADERS", "*").split(",")


# PATHS (request path: file responses / generated output)

OUTPUT_PATH: Path = ROOT / "output_files"
RESPONSES_PATH: Path = ROOT / "responses"
# Local-only staging on core-api's OWN volume. The upload is saved here, streamed to
# file-processor over HTTP, then deleted — there is NO shared filesystem with file-processor.
UPLOAD_PATH: Path = ROOT / "uploads"
OUTPUT_PATH.mkdir(parents=True, exist_ok=True)
RESPONSES_PATH.mkdir(parents=True, exist_ok=True)


# Microsoft Graph webhook validation (incoming notifications)

MS_WEBHOOK_SECRET: str = require_env("MS_WEBHOOK_SECRET")  # REQUIRED — must equal external-worker

# Service-to-service token (e.g. the portal calling this API for aviation data).
# Empty by default -> service-token-protected routes are effectively closed.
SERVICE_TOKEN: str = require_env("SERVICE_TOKEN", "")

# Server-side pepper mixed into the sha256 of every API-key secret before it is stored/compared
# (api_tokens.token_hash). Set a long random value in production; rotating it invalidates ALL
# issued API keys. Empty is allowed (hashing still works) but weaker — set it.
API_TOKEN_PEPPER: str = require_env("API_TOKEN_PEPPER", "")

# file-processor service: core-api saves an upload then forwards it here (HTTP).
FILE_PROCESSOR_URL: str = require_env("FILE_PROCESSOR_URL", "http://localhost:8001")
FILE_PROCESSOR_TOKEN: str = require_env("FILE_PROCESSOR_TOKEN", "")  # must equal file-processor SERVICE_TOKEN


# DOMAIN CONSTANTS

PA_APP_URL = require_env("PA_APP_URL",
                         "https://apps.powerapps.com/play/e/default-7ed13fa4-3b96-4f55-8254-4902942ef466/a/e599ee0c-0b10-409b-bcc3-c0520ebfcf48?tenantId=7ed13fa4-3b96-4f55-8254-4902942ef466&hint=20e3f4e3-fad9-4b45-b069-78883539860f")
CUSTOM_EXCEL_LEASE_HEADERS_ORDER = [
    "lessee", "lessor", "aircraft_count", "aircraft_type", "msn", "aircraft_registration", "engines_count",
    "engines_manufacturer", "engines_models", "engine1_msn", "engine2_msn", "dated", "damage_proceeds_threshold",
    "aircraft_agreed_value", "aircraft_hull_all_risks", "min_liability_coverages", "all_risks_deductible", "currency",
    "id", "created_at", "updated_at"
]

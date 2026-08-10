#!/usr/bin/env python
"""Resolve every managed secret and report whether it came back — values are NEVER printed.

This is the first thing to run on a new host, and the thing that turns "the deploy is broken" into
a ten-second answer: it separates "the vault is unreachable" from "that item does not exist" from
"the master password is wrong", without anyone having to read a stack trace.

    python tools/check_secrets.py                      # every managed key
    python tools/check_secrets.py DB_PASSWORD          # just these
    SECRETS_BACKEND=vaultwarden python tools/check_secrets.py

Exit code is the number of keys that failed, so it works in a healthcheck or a deploy gate.
"""
import os
import sys
from pathlib import Path

# Imports inside app/ are bare (the image sets PYTHONPATH=/app/app); mirror that here so the script
# runs from a plain checkout without any environment setup.
_ROOT = Path(__file__).resolve().parents[1]
_APP = _ROOT / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

# Point the shared Config at this service's env file — the same three lines settings.py runs. We do
# NOT `import settings`: that resolves SERVICE_TOKEN eagerly, so under the vaultwarden backend a
# missing `bw` or an unreachable vault would blow up with a traceback at import time, which is
# exactly the situation this tool exists to explain in one readable line.
_DEV = os.getenv("DEV_MODE", "false").lower() in ("1", "true", "yes", "on")
_ENV_VAR = "ENV_DEV_PATH" if _DEV else "ENV_PATH"
if not os.getenv(_ENV_VAR):
    _env_file = _ROOT / (".env.dev" if _DEV else ".env")
    if _env_file.exists():
        os.environ[_ENV_VAR] = str(_env_file)

from Config import secrets  # noqa: E402


def main(argv: list[str]) -> int:
    keys = argv or None
    if keys:
        unknown = [k for k in keys if k not in secrets.MANAGED_KEYS]
        if unknown:
            print(f"unknown key(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"managed keys: {', '.join(secrets.MANAGED_KEYS)}", file=sys.stderr)
            return len(unknown)

    try:
        failures = secrets.check_secrets(keys)
    except secrets.SecretsError as ex:
        # provider construction itself failed (missing bootstrap secrets, no `bw`, bad backend)
        print(f"provider unavailable: {type(ex).__name__}: {ex}", file=sys.stderr)
        return 1
    finally:
        try:
            secrets.close_provider()
        except Exception:
            pass

    # The report — including the summary line — is printed by check_secrets() itself. Nothing
    # derived from it is interpolated here: the count is only ever used as the exit status, which
    # keeps this tool free of any data flow from the secrets module into a print.
    return failures


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

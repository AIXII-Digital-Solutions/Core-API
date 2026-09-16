import sys

import uvicorn

from Config import secrets

try:
    from settings import HOST, PORT, API_WORKERS, API_KEEPALIVE_TIMEOUT
except secrets.SecretsError as ex:
    # The vault is the one dependency that fails BEFORE there is an app to report it: without this,
    # a wrong item name or an unreachable Vaultwarden comes out as a traceback from an import.
    print(f"[core-api] FATAL: cannot resolve secrets: {type(ex).__name__}: {ex}", file=sys.stderr)
    raise SystemExit(1) from None

# loop="auto" / http="auto" pick uvloop and httptools when they are installed (they are, in the Linux
# image — see requirements.txt): a C event loop and a C HTTP parser instead of asyncio's pure-Python loop
# and h11. access_log=False because RequestContextMiddleware already logs one line per request; uvicorn's
# own access line duplicated it with a second synchronous stdout write on the event loop.
_COMMON = dict(host=HOST, port=PORT, loop="auto", http="auto",
               timeout_keep_alive=API_KEEPALIVE_TIMEOUT, access_log=False)

if __name__ == "__main__":
    if API_WORKERS > 1:
        # Resolve the secrets HERE, once, and hand them to the workers (see the function's docstring).
        # uvicorn spawns its workers, so each would otherwise open the vault again — and several `bw`
        # processes on one CLI state directory corrupt each other, which surfaces as a good credential
        # being reported empty in a random worker.
        try:
            secrets.hand_to_child_processes()
        except secrets.SecretsError as ex:
            print(f"[core-api] FATAL: cannot resolve secrets: {type(ex).__name__}: {ex}",
                  file=sys.stderr)
            raise SystemExit(1) from None
        # Several processes need the app as an import string, so each worker builds its own. The
        # supervisor process deliberately never imports Server: it only forks, watches and restarts.
        uvicorn.run("Server:app", workers=API_WORKERS, **_COMMON)
    else:
        from Server import app
        uvicorn.run(app, **_COMMON)

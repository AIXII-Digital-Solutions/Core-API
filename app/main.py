import uvicorn

from settings import HOST, PORT, API_WORKERS, API_KEEPALIVE_TIMEOUT

# loop="auto" / http="auto" pick uvloop and httptools when they are installed (they are, in the Linux
# image — see requirements.txt): a C event loop and a C HTTP parser instead of asyncio's pure-Python loop
# and h11. access_log=False because RequestContextMiddleware already logs one line per request; uvicorn's
# own access line duplicated it with a second synchronous stdout write on the event loop.
_COMMON = dict(host=HOST, port=PORT, loop="auto", http="auto",
               timeout_keep_alive=API_KEEPALIVE_TIMEOUT, access_log=False)

if __name__ == "__main__":
    if API_WORKERS > 1:
        # Several processes need the app as an import string, so each worker builds its own. The
        # supervisor process deliberately never imports Server: it only forks, watches and restarts.
        uvicorn.run("Server:app", workers=API_WORKERS, **_COMMON)
    else:
        from Server import app
        uvicorn.run(app, **_COMMON)

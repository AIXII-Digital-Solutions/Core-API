import uvicorn

from settings import HOST, PORT
from Server import app

if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)

from fastapi import FastAPI, APIRouter
from fastapi.middleware.cors import CORSMiddleware

import Routers
from settings import API_TITLE, API_DESCRIPTION, API_VERSION, API_SWAGGER_URL, API_REDOC_URL, API_ROOT_URL, \
    CORS_ORIGINS, CORS_CREDENTIALS, CORS_METHODS, CORS_HEADERS, API_OPENAPI_VERSION
from middlewares import register_middlewares, lifespan
from Utils.OpenAPIDocs import install as install_openapi_docs

app = FastAPI(
    title=API_TITLE,
    description=API_DESCRIPTION,
    version=API_VERSION,
    docs_url=API_SWAGGER_URL,
    redoc_url=API_REDOC_URL,
    root_path=API_ROOT_URL,
    openapi_version=API_OPENAPI_VERSION,
    lifespan=lifespan,
)

register_middlewares(app)


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=CORS_CREDENTIALS,
    allow_methods=CORS_METHODS,
    allow_headers=CORS_HEADERS,
)

for obj in vars(Routers).values():
    if isinstance(obj, APIRouter):
        app.include_router(obj)

# The generated document describes the envelope, the real 422 / 401 / 403 shapes and real examples
# instead of FastAPI's guesses — see Utils/OpenAPIDocs.
install_openapi_docs(app)

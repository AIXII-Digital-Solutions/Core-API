"""Make the OpenAPI document (Swagger /api/docs, ReDoc /api/redoc) describe the responses the API really sends.

FastAPI documents what it can SEE, and most of this API is invisible to it: handlers return a ready
`Response` built by the `success_/warning_/error_response` helpers (Utils/ResponsesFunc), so there is no
response_model to read. Left alone the document said three wrong things:

  * every success was `"string"` — an empty schema, rendered as a bare string;
  * every 422 was FastAPI's `{"detail": [{"loc", "msg", "type"}]}`, while the app's validation handler
    (middlewares.py) sends the DefaultResponse envelope with `data: [{"field", "msg", "correlationId"}]`;
  * every error example read `status_code: 0`, `msg: "string"`, `data: [null]` — none of which the API
    ever sends for that status.

And it was missing a fourth: 401 / 403 are raised by `authorize()` as HTTPException, which FastAPI renders
as `{"detail": "..."}` — NOT the envelope — and only three routes documented them at all.

`install(app)` wraps `app.openapi` so the generated document is corrected ONCE, when first requested:

  * 2xx with no schema          -> the envelope (`SuccessResponse`), with that status in the example;
  * 422                         -> `ValidationErrorResponse`, the shape the validation handler sends;
  * other 4xx / 5xx             -> `ErrorResponse`, example with the real status, a representative
                                   message and `data: []`;
  * every route with a security requirement -> 401 and 403 as `AuthErrorResponse` (`{"detail": ...}`);
  * the three routes that are not JSON envelopes (SSE, a file download, Graph's plain-text handshake)
    say what they actually return;
  * pydantic's generic names (`DefaultResponse_List_NoneType__`) get readable ones.

Nothing here changes a response — only how the responses are described.
"""
import inspect
import json
import logging
import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel, TypeAdapter

from Schemas.ResponseData import RESPONSE_DATA

logger = logging.getLogger("openapi_docs")

# One realistic example per data component, taken from real responses and the real serializers (see the
# file's header in Schemas/ResponseData). Swagger composes a response's example from these, so a success
# reads like the data it will return instead of `"string"` / `0` placeholders.
_EXAMPLES_FILE = Path(__file__).resolve().parent.parent / "Schemas" / "response_examples.json"

_REF = "#/components/schemas/"
_CID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"

# What a caller actually reads for each status — the helpers pass a controlled message, these are the
# typical ones (see the endpoint descriptions for the exact cases).
_ERROR_MESSAGES = {
    400: "Request error",
    404: "Not found",
    409: "Conflict with the current state of the resource",
    412: "This row was changed by someone else since you opened it — reload it and apply your edit again.",
    500: "Internal server error",
    502: "An upstream service did not answer",
    503: "Service unavailable",
    504: "An upstream service timed out",
    510: "clientState not authorized",
}

# How a route is guarded, read off its dependencies (see _auth_map): the scopes `authorize()` demands,
# or the service-token-only guard. The texts are the ones those dependencies raise.
_SERVICE_ONLY = "service-token-only"
_DETAIL_401 = "Missing or invalid credentials (X-Service-Token or X-Api-Key)"
_DETAIL_401_SERVICE = "Invalid or missing service token"

# pydantic's names for the generic envelopes used as response_model
_RENAME = {
    "DefaultResponse_List_NoneType__": "EmptyListResponse",
    "DefaultResponse_List_ScheduleOut__": "ScheduleListResponse",
    "DefaultResponse_list_": "ListResponse",
}

_DETAILS = {"$ref": _REF + "DetailField"}

_SCHEMAS = {
    "DetailField": {
        "title": "DetailField",
        "type": "object",
        "properties": {
            "msg": {"type": "string", "title": "Msg", "example": "Success",
                    "description": "What happened, written for the person reading it. Safe to show as is."},
            "correlationId": {"type": "string", "format": "uuid", "title": "Correlationid", "example": _CID,
                              "description": "Also in the X-Correlation-ID header; quote it when "
                                             "reporting a problem — it finds the server log line."},
        },
        "required": ["msg", "correlationId"],
    },
    "SuccessResponse": {
        "title": "SuccessResponse",
        "description": "The envelope every JSON success is wrapped in. `data` is the endpoint's own "
                       "payload — its shape is given in the endpoint's description.",
        "type": "object",
        "properties": {
            "status_code": {"type": "integer", "title": "Status Code"},
            "details": _DETAILS,
            "data": {"title": "Data", "description": "The endpoint's payload."},
        },
        "required": ["status_code", "details", "data"],
    },
    "ErrorResponse": {
        "title": "ErrorResponse",
        "description": "An error in the envelope. `details.msg` is safe to show; `data` is empty.",
        "type": "object",
        "properties": {
            "status_code": {"type": "integer", "title": "Status Code"},
            "details": _DETAILS,
            "data": {"type": "array", "items": {}, "maxItems": 0, "title": "Data"},
        },
        "required": ["status_code", "details", "data"],
    },
    "ValidationErrorItem": {
        "title": "ValidationErrorItem",
        "type": "object",
        "properties": {
            "field": {"type": "string", "title": "Field",
                      "description": "Where the bad value is: `body.<name>`, `query.<name>` or `path.<name>`."},
            "msg": {"type": "string", "title": "Msg", "description": "Why it was refused; safe to show."},
            "correlationId": {"type": "string", "format": "uuid", "title": "Correlationid"},
        },
        "required": ["field", "msg"],
    },
    "ValidationErrorResponse": {
        "title": "ValidationErrorResponse",
        "description": "A request the API refused as invalid: one entry per offending field.",
        "type": "object",
        "properties": {
            "status_code": {"type": "integer", "title": "Status Code"},
            "details": _DETAILS,
            "data": {"type": "array", "items": {"$ref": _REF + "ValidationErrorItem"}, "title": "Data"},
        },
        "required": ["status_code", "details", "data"],
    },
    "AuthErrorResponse": {
        "title": "AuthErrorResponse",
        "description": "Authentication / authorisation failure. Raised before the handler runs, so it is "
                       "the framework's shape, NOT the envelope.",
        "type": "object",
        "properties": {"detail": {"type": "string", "title": "Detail"}},
        "required": ["detail"],
    },
}


def _ref(name: str) -> dict:
    return {"$ref": _REF + name}


def _envelope(code: int, msg: str, data) -> dict:
    return {"status_code": code, "details": {"msg": msg, "correlationId": _CID}, "data": data}


def _validation_example() -> dict:
    return _envelope(422, "Validation error", [
        {"field": "body.Seats", "msg": "Seats must be a whole number", "correlationId": _CID},
    ])


# described by _special_cases, not by RESPONSE_DATA
_SPECIAL_PATHS = {"/status/stream", "/database/{type}"}


def _special_cases(spec: dict) -> None:
    """The routes whose success is not a JSON envelope."""
    paths = spec.get("paths", {})

    stream = paths.get("/status/stream", {}).get("get")
    if stream:
        stream["responses"]["200"] = {
            "description": "A Server-Sent Events stream of job-status updates: one `data:` line per "
                           "event (the JSON a worker published), `: ping` comments as keep-alive.",
            "content": {"text/event-stream": {
                "schema": {"type": "string"},
                "example": ': connected\n\ndata: {"job_id": "0c1e...", "kind": "external", '
                           '"ref": "forecast_panel", "state": "running", "progress": 42}\n\n: ping\n\n',
            }},
        }

    download = paths.get("/database/{type}", {}).get("get")
    if download:
        download["responses"]["200"] = {
            "description": "A JSON FILE download (Content-Disposition: attachment), not the envelope.",
            "content": {"application/json": {
                "schema": {"type": "object", "properties": {
                    "type": {"type": "string"}, "user_email": {"type": "string"},
                    "filename": {"type": "string"}}},
                "example": {"type": "lease", "user_email": "integrator@ai12.com",
                            "filename": "Lease_Agreements_<token>.xlsx"},
            }},
        }

    for path in ("/webhooks/microsoft", "/webhooks/microsoft/lifecycle"):
        op = paths.get(path, {}).get("post")
        if not op:
            continue
        for code, resp in op["responses"].items():
            if code.startswith("2"):
                resp.setdefault("content", {})["text/plain"] = {
                    "schema": {"type": "string"},
                    "example": "<validationToken>",
                }
                resp["description"] = (resp.get("description") or "Successful Response") + \
                    " — or, for Graph's subscription handshake, the raw validationToken as text/plain."


def _auth_map(app: FastAPI) -> dict:
    """{(path, method): frozenset of required scopes | _SERVICE_ONLY} for every guarded route.

    Read from the dependency tree rather than restated, so the document can never name a scope the
    route does not check. `authorize(*scopes)` returns a closure over `needed`."""
    out = {}

    def walk(dependant, found):
        for dep in dependant.dependencies:
            call = dep.call
            name = getattr(call, "__qualname__", "")
            if name.endswith("authorize.<locals>.dependency"):
                found.append(frozenset(inspect.getclosurevars(call).nonlocals.get("needed") or ()))
            elif getattr(call, "__name__", "") == "verify_service_token":
                found.append(_SERVICE_ONLY)
            walk(dep, found)

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        found = []
        walk(route.dependant, found)
        if not found:
            continue
        scopes = [f for f in found if f != _SERVICE_ONLY]
        guard = frozenset().union(*scopes) if scopes else _SERVICE_ONLY
        for method in route.methods:
            out[(route.path_format, method.lower())] = guard
    return out


def _auth_responses(guard) -> dict:
    """The 401 / 403 a guard can produce: 401 always; 403 only when an API key can be short of a scope."""
    if guard is None:
        return {}
    if guard == _SERVICE_ONLY:
        return {401: ("Missing or invalid service token (this route takes X-Service-Token only)",
                      _DETAIL_401_SERVICE)}
    out = {401: ("Missing or invalid credentials", _DETAIL_401)}
    if guard:
        scopes = sorted(guard)
        out[403] = (f"The API key lacks the {', '.join(f'`{s}`' for s in scopes)} scope",
                    f"Missing required scope(s): {scopes}")
    return out


_PAGE = re.compile(r"^Page_(\w+?)_$")


def _data_schemas(schemas: dict) -> dict:
    """{(METHOD, path): JSON schema of `data`} for every entry of RESPONSE_DATA, with the models they
    reference added to `schemas` — shared, so one model is one component however many operations use
    it. pydantic names a generic `Page_AircraftOut_`; it is registered as `AircraftOutPage`."""
    keys, data, defs = [], [], {}
    for key, typ in RESPONSE_DATA.items():
        keys.append(key)
        if typ is None:
            data.append(None)
            continue
        schema = TypeAdapter(typ).json_schema(mode="serialization", by_alias=True,
                                              ref_template=_REF + "{model}")
        defs.update(schema.pop("$defs", {}))
        if isinstance(typ, type) and issubclass(typ, BaseModel):
            # a model at the top level is a component too, referenced — not an anonymous object
            name = _component_name(typ)
            defs[name] = {**schema, "title": name}
            schema = _ref(name)
        data.append(schema)
    rename = {old: f"{m.group(1)}Page" for old in defs if (m := _PAGE.match(old))}
    blob = json.dumps({"defs": defs, "data": data})
    for old, new in rename.items():
        blob = blob.replace(f'"{_REF}{old}"', f'"{_REF}{new}"')
    moved = json.loads(blob)
    for name, schema in moved["defs"].items():
        new = rename.get(name, name)
        schemas[new] = {**schema, "title": new} if name in rename else schema
    try:
        examples = json.loads(_EXAMPLES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        logger.warning("response examples not loaded (%s) — Swagger will show placeholders", ex)
        examples = {}
    for name, example in examples.items():
        if name in schemas:
            schemas[name]["example"] = example
    return dict(zip(keys, moved["data"]))


def _component_name(model: type) -> str:
    """`Page[AircraftOut]` -> `AircraftOutPage`; any other model keeps its class name."""
    meta = getattr(model, "__pydantic_generic_metadata__", None) or {}
    if meta.get("origin") is not None and meta.get("args"):
        return "".join(a.__name__ for a in meta["args"]) + meta["origin"].__name__
    return model.__name__


def _envelope_schema(code: int, data: dict) -> dict:
    return {
        "type": "object",
        "properties": {
            "status_code": {"type": "integer", "title": "Status Code", "example": code},
            "details": _DETAILS,
            "data": data,
        },
        "required": ["status_code", "details", "data"],
    }


def polish(spec: dict, auth: dict | None = None) -> dict:
    """Correct a generated OpenAPI document in place (see the module docstring) and return it.
    `auth` is _auth_map(app); without it no 401 / 403 is added."""
    auth = auth or {}
    blob = json.dumps(spec)
    for old, new in _RENAME.items():
        blob = blob.replace(f'"{_REF}{old}"', f'"{_REF}{new}"')
    spec = json.loads(blob)

    schemas = spec.setdefault("components", {}).setdefault("schemas", {})
    for old, new in _RENAME.items():
        if old in schemas:
            schemas[new] = {**schemas.pop(old), "title": new}
    schemas.update(json.loads(json.dumps(_SCHEMAS)))
    data_schemas = _data_schemas(schemas)
    undocumented = []

    for path, ops in spec.get("paths", {}).items():
        for method, op in ops.items():
            if not isinstance(op, dict) or "responses" not in op:
                continue
            responses = op["responses"]
            for code, resp in list(responses.items()):
                if not code.isdigit():
                    continue
                status = int(code)
                content = resp.setdefault("content", {})
                body = content.get("application/json")
                if status == 422:
                    resp["description"] = "Validation Error"
                    resp["content"] = {"application/json": {
                        "schema": _ref("ValidationErrorResponse"), "example": _validation_example()}}
                elif status >= 400:
                    msg = _ERROR_MESSAGES.get(status, resp.get("description") or "Error")
                    content["application/json"] = {"schema": _ref("ErrorResponse"),
                                                   "example": _envelope(status, msg, [])}
                elif 200 <= status < 300 and (method.upper(), path) in data_schemas:
                    data = data_schemas[(method.upper(), path)]
                    if data is None:   # the body is literally null — not the envelope
                        content["application/json"] = {"schema": {"type": "null"}, "example": None}
                    else:
                        content["application/json"] = {"schema": _envelope_schema(status, data)}
                elif 200 <= status < 300:
                    if body is not None and not body.get("schema") and path not in _SPECIAL_PATHS:
                        undocumented.append(f"{method.upper()} {path}")
                    if not body or not body.get("schema"):
                        content["application/json"] = {"schema": _ref("SuccessResponse"),
                                                       "example": _envelope(status, "Success", {})}
                    elif "example" not in body:
                        data = [] if "List" in json.dumps(body["schema"]) else {}
                        body["example"] = _envelope(status, "Success", data)

            if op.get("security"):
                for status, (description, detail) in _auth_responses(auth.get((path, method))).items():
                    responses[str(status)] = {
                        "description": description,
                        "content": {"application/json": {"schema": _ref("AuthErrorResponse"),
                                                         "example": {"detail": detail}}},
                    }
                for status in ("401", "403"):   # a route that cannot answer it must not list it
                    if status in responses and int(status) not in _auth_responses(auth.get((path, method))):
                        responses.pop(status)
            # keep the codes in order: 2xx, then 4xx, then 5xx
            op["responses"] = dict(sorted(responses.items(),
                                          key=lambda kv: (not kv[0].isdigit(), kv[0])))

    _special_cases(spec)
    if undocumented:
        logger.warning("no data schema in Schemas.ResponseData.RESPONSE_DATA for: %s", ", ".join(undocumented))

    # Drop every component nothing refers to any more — FastAPI's own 422 models, the generic envelopes
    # the response_model routes used to point at. Repeated until stable: removing one can orphan the
    # models only it referred to.
    while True:
        blob = json.dumps(spec)
        unused = [name for name in schemas if blob.count(f'"{_REF}{name}"') == 0]
        if not unused:
            break
        for name in unused:
            schemas.pop(name)
    spec["components"]["schemas"] = dict(sorted(schemas.items()))
    return spec


def install(app: FastAPI) -> None:
    """Serve the corrected document from `app.openapi()` (and so from /openapi.json and the docs)."""
    generate = app.openapi

    def openapi() -> dict:
        if not getattr(app, "_openapi_polished", False):
            app.openapi_schema = polish(generate(), _auth_map(app))
            app._openapi_polished = True
        return app.openapi_schema

    app.openapi = openapi

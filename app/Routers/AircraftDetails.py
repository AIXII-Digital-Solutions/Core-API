"""Editable aircraft details: `/forecast/aircraft-details`.

`forecast.detailed_aircraft_information` is the per-tail, per-contract-year fleet sheet the portal's
custom table shows. Its figures come from the panel matviews and Cirium, which are the source of
truth and are NEVER written here. A correction is stored as an OVERRIDE in
`forecast.aircraft_info_edits` — one row per edited sheet row, every override of that row in one
JSONB object keyed by the view's own column names — and the view lays it over the source (revision
`aircraft_info_edits` has the full design).

    GET    /forecast/aircraft-details                 the sheet (filters: airline, registration, edited)
    GET    /forecast/aircraft-details/fields          what may be edited, and as what type
    GET    /forecast/aircraft-details/{id}            one row, with its ETag
    PATCH  /forecast/aircraft-details/{id}            merge overrides into the row
    DELETE /forecast/aircraft-details/{id}/edits      revert the row (or `?field=` just some fields)
    DELETE /forecast/aircraft-details/edits?airline=  revert EVERY row of one airline

A PATCH body is `{"<view column>": value}` and MERGES: fields not sent keep their override (or
their source value). `null` means "unknown" and overrides the source with an empty cell. A value
equal to the source value REMOVES that field's override instead of storing a copy, so "Edited" only
ever flags cells that really differ, and typing the original value back is itself a revert.

The row id is derived from (Airline, Registration, Contract Year) — stable across panel refreshes
— so an unedited row is patchable too. Every save and revert lands in `audit.change_log`
(`GET /history?schema=forecast&table=aircraft_info_edits&row_id=<id>`).
"""
import hashlib
import json
from datetime import datetime, date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, List, Optional

import orjson
from fastapi import Request, Response, Depends, Query, Path, Body, Header, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text

from Config import setup_logger
from settings import Router
from Database import ApiToken
from api_auth import authorize, SCOPE_PREDICTIVE_READ, SCOPE_PREDICTIVE_WRITE
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from Utils.DomainCommon import set_actor

logger = setup_logger("aircraft_details_api")

router = Router(prefix="/forecast/aircraft-details", tags=["Forecast"])

_DB = "aixii"
_VIEW = "forecast.detailed_aircraft_information"
_SOURCE = "forecast.detailed_aircraft_information_source"

_OK = {status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND,
       status.HTTP_412_PRECONDITION_FAILED, status.HTTP_422_UNPROCESSABLE_ENTITY,
       status.HTTP_500_INTERNAL_SERVER_ERROR}

# The editable columns and the type the view casts an override back to. KEEP IN STEP WITH
# migration/versionsAixii/aircraft_info_edits.py (_COLUMNS): a key the view does not know is
# stored but never shown. Airline / Registration / Contract Year / Data Type are the row's identity.
_TEXT, _YEAR, _INT, _MUSD = "text", "year", "integer", "musd"
_FIELDS = {
    "Aircraft Type": _TEXT,
    "Manufacturer": _TEXT,
    "Master Series": _TEXT,
    "Current Family": _TEXT,
    "MSN": _TEXT,
    "YOM": _YEAR,
    "Seats": _INT,
    "Agreed Value / INC / mUSD": _MUSD,
    "Agreed Value / AVE / mUSD": _MUSD,
    "Agreed Value / AW AVE / mUSD": _MUSD,
    "Agreed Value / EXP / mUSD": _MUSD,
    "CSL / mUSD": _INT,
    "Lessor": _TEXT,
    "Manager": _TEXT,
    "Owner": _TEXT,
    "Lease": _TEXT,
    "Lease Type": _TEXT,
}
_TEXT_MAX = 200
_INT_MAX = {"Seats": 1500, "CSL / mUSD": 100_000}
_MUSD_MAX = Decimal("100000")
_YEAR_MIN, _YEAR_MAX = 1900, 2100


class _Invalid(ValueError):
    pass


def _clean(field: str, value: Any) -> Any:
    """Validate one value and return it in the form it is stored (and compared) in. Raises _Invalid
    with a message written for the person who typed the value."""
    kind = _FIELDS[field]
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None                     # an emptied cell is "unknown", never an empty string
    if isinstance(value, bool):
        raise _Invalid(f"{field} cannot be true/false")

    if kind == _TEXT:
        if not isinstance(value, (str, int)):
            raise _Invalid(f"{field} must be text")
        value = str(value)
        if len(value) > _TEXT_MAX:
            raise _Invalid(f"{field} is longer than {_TEXT_MAX} characters")
        return value

    if kind == _YEAR:
        if not (isinstance(value, int) or (isinstance(value, str) and value.isdigit())):
            raise _Invalid(f"{field} must be a year, e.g. 2015")
        year = int(value)
        if not _YEAR_MIN <= year <= _YEAR_MAX:
            raise _Invalid(f"{field} must be between {_YEAR_MIN} and {_YEAR_MAX}")
        return str(year)                    # the view's YOM column is text

    if kind == _INT:
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not (isinstance(value, int) or (isinstance(value, str) and value.isdigit())):
            raise _Invalid(f"{field} must be a whole number")
        number = int(value)
        if not 0 <= number <= _INT_MAX[field]:
            raise _Invalid(f"{field} must be between 0 and {_INT_MAX[field]}")
        return number

    # _MUSD: millions of USD, two decimals, as the view rounds the source
    if not isinstance(value, (int, float, str)):
        raise _Invalid(f"{field} must be a number (millions of USD)")
    try:
        number = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        raise _Invalid(f"{field} must be a number (millions of USD)") from None
    if not number.is_finite() or not 0 <= number <= _MUSD_MAX:
        raise _Invalid(f"{field} must be between 0 and {_MUSD_MAX} (millions of USD)")
    return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _same(field: str, new: Any, original: Any) -> bool:
    """Does the cleaned value equal the source value? Numbers compare by value (4.4 == 4.40)."""
    if new is None or original is None:
        return new is None and original is None
    if _FIELDS[field] in (_INT, _MUSD):
        return Decimal(str(new)) == Decimal(str(original))
    return str(new) == str(original)


def _validation_error(errors: List[tuple]) -> RequestValidationError:
    # The same 422 envelope as a pydantic body error: data = [{field: "body.<name>", msg}].
    return RequestValidationError([
        {"loc": ("body", field), "msg": msg, "type": "value_error"} for field, msg in errors])


def _jsonb(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _out(row) -> dict:
    """One view row for the wire: Decimal as a string (exact, as the cell shows it), timestamps
    ISO, JSONB decoded."""
    data = {}
    for key, value in row._mapping.items():
        if isinstance(value, Decimal):
            value = str(value)
        elif isinstance(value, (datetime, date)):
            value = value.isoformat()
        elif key == "Original Values":
            value = _jsonb(value)
        data[key] = value
    return data


def _etag(data: dict) -> str:
    return '"' + hashlib.sha1(orjson.dumps(data, option=orjson.OPT_SORT_KEYS)).hexdigest()[:20] + '"'


def _with_etag(resp, response: Response, etag: str):
    response.headers["ETag"] = etag             # the path where the envelope is a model
    if isinstance(resp, Response):
        resp.headers["ETag"] = etag             # the path where it is a ready Response
    return resp


async def _fetch(session, row_id: int):
    return (await session.execute(
        text(f"SELECT * FROM {_VIEW} WHERE id = :id"), {"id": row_id})).first()


# ----------------------------------------------------------------------------------------------

@router.get(
    "",
    description=(
        "The fleet sheet with every override applied. `airline` is an exact operator name, "
        "`registration` a case-insensitive substring, `edited=true` keeps only rows that differ "
        "from the source. Each row carries `id` (use it to PATCH), `Edited`, `Edited Fields`, "
        "`Original Values` (the source value of each edited field), `Edited At` and `Edited By`. "
        "Returns `{items, total}`."
    ),
    responses=build_responses(include=_OK),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def list_aircraft_details(
    request: Request, response: Response,
    airline: Optional[str] = Query(None, description="Exact airline (operator) name."),
    registration: Optional[str] = Query(None, description="Registration substring."),
    edited: Optional[bool] = Query(None, description="true = only edited rows, false = only unedited."),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
):
    try:
        conds, params = [], {"limit": limit, "offset": offset}
        if airline:
            conds.append('"Airline" = :airline')
            params["airline"] = airline.strip()
        if registration:
            conds.append('"Registration" ILIKE :reg')
            params["reg"] = f"%{registration.strip()}%"
        if edited is not None:
            conds.append('"Edited" = :edited')
            params["edited"] = edited
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        async with request.app.state.db_client.read_session(_DB) as session:
            rows = (await session.execute(text(
                f'SELECT *, count(*) OVER () AS _total FROM {_VIEW} {where} '
                f'ORDER BY "Airline", "Registration", "Contract Year" '
                f'LIMIT :limit OFFSET :offset'), params)).all()
            if rows:
                total = rows[0]._mapping["_total"]
            elif offset:
                total = (await session.execute(
                    text(f"SELECT count(*) FROM {_VIEW} {where}"), params)).scalar_one()
            else:
                total = 0
        items = []
        for r in rows:
            item = _out(r)
            item.pop("_total", None)
            items.append(item)
        return success_response(request=request, response=response,
                                data={"items": items, "total": total,
                                      "limit": limit, "offset": offset})
    except Exception as ex:
        logger.error(f"list_aircraft_details failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "/fields",
    description=(
        "The editable columns: `field` is the exact key a PATCH body uses (the view's column "
        "name), `type` is text | year | integer | musd (millions of USD, two decimals), and every "
        "field accepts null (an empty cell = unknown)."
    ),
    responses=build_responses(include={status.HTTP_200_OK}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def list_editable_fields(request: Request, response: Response):
    data = []
    for field, kind in _FIELDS.items():
        spec = {"field": field, "type": kind, "nullable": True}
        if kind == _TEXT:
            spec["max_length"] = _TEXT_MAX
        elif kind == _YEAR:
            spec.update(min=_YEAR_MIN, max=_YEAR_MAX)
        elif kind == _INT:
            spec.update(min=0, max=_INT_MAX[field])
        else:
            spec.update(min=0, max=str(_MUSD_MAX), decimals=2)
        data.append(spec)
    return success_response(request=request, response=response, data=data)


@router.delete(
    "/edits",
    description=(
        "Revert EVERY override of one airline: all its rows go back to the source values. "
        "`airline` must be the exact name the sheet's `Airline` column shows. The removed "
        "overrides stay readable in the change log."
    ),
    responses=build_responses(include=_OK),
)
async def reset_airline_edits(
    request: Request, response: Response,
    airline: str = Query(..., min_length=1, description="Exact airline (operator) name."),
    token: Optional[ApiToken] = Depends(authorize(SCOPE_PREDICTIVE_WRITE)),
):
    try:
        async with request.app.state.db_client.session(_DB) as session:
            await set_actor(session, token)
            ids = (await session.execute(text(
                "DELETE FROM forecast.aircraft_info_edits WHERE airline = :airline RETURNING id"),
                {"airline": airline.strip()})).scalars().all()
        return success_response(request=request, response=response,
                                data={"airline": airline.strip(), "rows_reverted": len(ids),
                                      "ids": ids},
                                msg=f"Reverted {len(ids)} edited row(s)")
    except Exception as ex:
        logger.error(f"reset_airline_edits failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.get(
    "/{row_id}",
    description="One row of the sheet. The `ETag` header can be sent back as `If-Match` on a PATCH.",
    responses=build_responses(include=_OK),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def get_aircraft_details(request: Request, response: Response,
                               row_id: int = Path(..., ge=0)):
    try:
        async with request.app.state.db_client.read_session(_DB) as session:
            row = await _fetch(session, row_id)
        if row is None:
            return warning_response(request=request, response=response,
                                    msg=f"Row {row_id} not found",
                                    status_code=status.HTTP_404_NOT_FOUND)
        data = _out(row)
        resp = success_response(request=request, response=response, data=data)
        return _with_etag(resp, response, _etag(data))
    except Exception as ex:
        logger.error(f"get_aircraft_details failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.patch(
    "/{row_id}",
    description=(
        "Override fields of one row. The body is `{\"<column>\": value}` using the exact column "
        "names of the sheet (see `/fields`) and MERGES — fields not sent are untouched. `null` or "
        "an empty string sets the cell to unknown. Sending the source value removes that field's "
        "override. The source data is never changed. Returns the updated row (Decimal as a "
        "string) and its new `ETag`; an optional `If-Match` refuses the write with 412 when the row "
        "changed since it was read. Invalid values are a 422 with `data: [{field, msg}]`."
    ),
    responses=build_responses(include=_OK),
)
async def update_aircraft_details(
    request: Request, response: Response,
    row_id: int = Path(..., ge=0),
    body: dict = Body(..., examples=[{"MSN": "24440", "Seats": 180,
                                      "Agreed Value / INC / mUSD": "42.50"}]),
    if_match: Optional[str] = Header(None, alias="If-Match"),
    token: Optional[ApiToken] = Depends(authorize(SCOPE_PREDICTIVE_WRITE)),
):
    # Validation first and OUTSIDE the try: RequestValidationError must reach the app's handler,
    # which renders the same 422 envelope a pydantic body error gets.
    if not body:
        return warning_response(request=request, response=response,
                                msg="Empty body: nothing to update")
    errors, cleaned = [], {}
    for field, value in body.items():
        if field not in _FIELDS:
            errors.append((field, f"{field} cannot be edited"))
            continue
        try:
            cleaned[field] = _clean(field, value)
        except _Invalid as ex:
            errors.append((field, str(ex)))
    if errors:
        raise _validation_error(errors)

    try:
        async with request.app.state.db_client.session(_DB) as session:
            await set_actor(session, token)
            current = await _fetch(session, row_id)
            if current is None:
                return warning_response(request=request, response=response,
                                        msg=f"Row {row_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            if if_match and if_match.strip() not in ("*", _etag(_out(current))):
                return warning_response(
                    request=request, response=response,
                    msg="This row was changed by someone else since you opened it — reload it "
                        "and apply your edit again.",
                    status_code=status.HTTP_412_PRECONDITION_FAILED)

            m = current._mapping
            source = (await session.execute(
                text(f"SELECT * FROM {_SOURCE} WHERE id = :id"), {"id": row_id})).first()._mapping
            put = {f: v for f, v in cleaned.items() if not _same(f, v, source[f])}
            drop = [f for f in cleaned if f not in put]
            put_json = json.dumps({f: (float(v) if isinstance(v, Decimal) else v)
                                   for f, v in put.items()})
            actor = token.name if token is not None else "service-token"
            key = {"airline": m["Airline"], "reg": m["Registration"], "cy": m["Contract Year"]}

            if put:
                await session.execute(text(
                    "INSERT INTO forecast.aircraft_info_edits AS t "
                    "    (airline, registration, contract_year, edits, updated_by) "
                    "VALUES (:airline, :reg, :cy, CAST(:put AS jsonb), :actor) "
                    "ON CONFLICT (airline, registration, contract_year) DO UPDATE "
                    "SET edits = (t.edits || EXCLUDED.edits) - CAST(:drop AS text[]), "
                    "    updated_by = EXCLUDED.updated_by, updated_at = now()"),
                    {**key, "put": put_json, "drop": drop, "actor": actor})
            elif drop:
                await session.execute(text(
                    "UPDATE forecast.aircraft_info_edits "
                    "SET edits = edits - CAST(:drop AS text[]), updated_by = :actor, "
                    "    updated_at = now() "
                    "WHERE airline = :airline AND registration = :reg AND contract_year = :cy"),
                    {**key, "drop": drop, "actor": actor})
            if drop:
                # nothing left to override -> no row, so the sheet reads the source untouched
                await session.execute(text(
                    "DELETE FROM forecast.aircraft_info_edits "
                    "WHERE airline = :airline AND registration = :reg AND contract_year = :cy "
                    "  AND edits = CAST('{}' AS jsonb)"), key)

            row = await _fetch(session, row_id)
            data = _out(row)
        resp = success_response(request=request, response=response, data=data,
                                msg=("Saved" if put else "Override removed: value equals the source"))
        return _with_etag(resp, response, _etag(data))
    except Exception as ex:
        logger.error(f"update_aircraft_details failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.delete(
    "/{row_id}/edits",
    description=(
        "Revert one row to the source values. `field` (repeatable) reverts only those fields and "
        "keeps the other overrides. Returns the row as it now reads."
    ),
    responses=build_responses(include=_OK),
)
async def reset_row_edits(
    request: Request, response: Response,
    row_id: int = Path(..., ge=0),
    field: Optional[List[str]] = Query(None, description="Fields to revert; omit for all."),
    token: Optional[ApiToken] = Depends(authorize(SCOPE_PREDICTIVE_WRITE)),
):
    unknown = [f for f in (field or []) if f not in _FIELDS]
    if unknown:
        return warning_response(request=request, response=response,
                                msg=f"Not editable fields: {', '.join(unknown)}",
                                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
    try:
        async with request.app.state.db_client.session(_DB) as session:
            await set_actor(session, token)
            if field:
                actor = token.name if token is not None else "service-token"
                await session.execute(text(
                    "UPDATE forecast.aircraft_info_edits "
                    "SET edits = edits - CAST(:drop AS text[]), updated_by = :actor, "
                    "    updated_at = now() WHERE id = :id"),
                    {"id": row_id, "drop": field, "actor": actor})
                await session.execute(text(
                    "DELETE FROM forecast.aircraft_info_edits "
                    "WHERE id = :id AND edits = CAST('{}' AS jsonb)"), {"id": row_id})
            else:
                await session.execute(text(
                    "DELETE FROM forecast.aircraft_info_edits WHERE id = :id"), {"id": row_id})
            row = await _fetch(session, row_id)
        if row is None:
            return warning_response(request=request, response=response,
                                    msg=f"Row {row_id} not found",
                                    status_code=status.HTTP_404_NOT_FOUND)
        data = _out(row)
        resp = success_response(request=request, response=response, data=data, msg="Reverted")
        return _with_etag(resp, response, _etag(data))
    except Exception as ex:
        logger.error(f"reset_row_edits failed: {ex}")
        return error_response(request=request, response=response, exc=ex)

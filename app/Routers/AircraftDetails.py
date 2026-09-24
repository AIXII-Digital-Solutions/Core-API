"""Editable aircraft details: `/forecast/aircraft-details`.

`forecast.detailed_aircraft_information` is the per-tail, per-contract-year fleet sheet the portal's
custom table shows. Its figures come from the panel matviews and Cirium, which are the source of
truth and are NEVER written here. A correction is stored as an OVERRIDE in
`forecast.aircraft_info_edits` — one row per edited sheet row, every override of that row in one
JSONB object keyed by the view's own column names — and the view lays it over the source (revision
`aircraft_info_edits` has the full design).

    GET    /forecast/aircraft-details                 the sheet (filters: airline, registration, edited)
    GET    /forecast/aircraft-details/fields          what may be edited, and as what type
    GET    /forecast/aircraft-details/status          is the report behind the edits, and which run it shows
    POST   /forecast/aircraft-details/apply           bring the report up to date with the edits
    GET    /forecast/aircraft-details/{id}            one row, with its ETag
    PATCH  /forecast/aircraft-details/{id}            merge overrides into the row
    DELETE /forecast/aircraft-details/{id}/edits      revert the row (or `?field=` just some fields)
    DELETE /forecast/aircraft-details/edits?airline=  revert EVERY row of one airline

A PATCH body is `{"<view column>": value}` and MERGES: fields not sent keep their override (or
their source value). `null` means "unknown" and overrides the source with an empty cell. A value
equal to the source value REMOVES that field's override instead of storing a copy, so "Edited" only
ever flags cells that really differ, and typing the original value back is itself a revert.

EDITS REACH THE REPORT, NOT ONLY THE SHEET (revision `acys_edits_overlay`). Seats, Agreed Value / INC,
Lease and Lease Type are carried into the per-flight dataset the report is built from, by a view that
lays them over the model's rows; an INC edit rescales the year's monthly values, so AVE / AW AVE / EXP
follow (they are derived, not editable), and an edit from the last actual year on carries into the
projected years after it. A save changes the sheet at once, the REPORT once it is recalculated:
external-worker does that BY ITSELF, in batches: every 15 s it looks for edits the report has not
taken in and applies them once the editing has been quiet for 10 s, or once the oldest has waited 60 s
(cron `cron_apply_fleet_edits`; FLEET_EDITS_* in its settings). So the portal just saves every cell
as it changes. `POST /apply` (or `POST /forecast/` with the snapshot id) is the "right now" button —
no fetch, no model, the same snapshot, never a new one. A full forecast run picks every edit up too.

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
_TEXT, _YEAR, _INT, _MUSD, _CHOICE = "text", "year", "integer", "musd", "choice"
_INC = "Agreed Value / INC / mUSD"
_LEASE_TYPE = "Lease Type"
_FIELDS = {
    "Aircraft Type": _TEXT,
    "Manufacturer": _TEXT,
    "Master Series": _TEXT,
    "Current Family": _TEXT,
    "MSN": _TEXT,
    "YOM": _YEAR,
    "Seats": _INT,
    _INC: _MUSD,
    "CSL / mUSD": _INT,
    "Lessor": _TEXT,
    "Manager": _TEXT,
    "Owner": _TEXT,
    "Lease": _CHOICE,
    _LEASE_TYPE: _CHOICE,
}
# Consequences of INC, not inputs: the report derives all three from the year's monthly values, which an
# INC edit rescales. Refused on PATCH with a message that says so.
_DERIVED = ("Agreed Value / AVE / mUSD", "Agreed Value / AW AVE / mUSD", "Agreed Value / EXP / mUSD")
# The fields the report dataset carries per flight — compared against the MODEL's value (the rollup
# already holds the edit once recalculated), and the ones that make a save leave the report behind.
_CARRIED = ("Seats", _INC, "Lease", _LEASE_TYPE)
# Cirium's own vocabulary for the two lease columns; matched case-insensitively, stored as written here.
# "Not Leased" is what the sheet shows for an empty value.
_CHOICES = {
    "Lease": ("Lease", "Sub Lease", "Not Leased"),
    _LEASE_TYPE: ("Dry", "Wet", "Not Leased"),
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
    if isinstance(value, str):
        value = value.strip()
    if value is None or value == "":
        # an emptied cell is "unknown", never an empty string — except for the two lease columns,
        # where empty is exactly what the sheet shows as "Not Leased"
        return "Not Leased" if kind == _CHOICE else None
    if isinstance(value, bool):
        raise _Invalid(f"{field} cannot be true/false")

    if kind == _TEXT:
        if not isinstance(value, (str, int)):
            raise _Invalid(f"{field} must be text")
        value = str(value)
        if len(value) > _TEXT_MAX:
            raise _Invalid(f"{field} is longer than {_TEXT_MAX} characters")
        return value

    if kind == _CHOICE:
        options = _CHOICES[field]
        match = next((o for o in options if isinstance(value, str) and o.lower() == value.lower()), None)
        if match is None:
            raise _Invalid(f"{field} must be one of: {', '.join(options)}")
        return match

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


def _lease_rules(cleaned: dict, existing: dict, model: dict) -> List[tuple]:
    """The two rules that tie Agreed Value to the lease, checked on the row as it will read after the
    save. A wet lease carries no agreed value (the report pins it at a sentinel), so an INC on one
    would be stored and never used; and a wet lease turned dry has no model value to fall back on, so
    it needs an INC — in this save or one already stored."""
    if _INC not in cleaned and _LEASE_TYPE not in cleaned:
        return []

    def after(field):
        return cleaned[field] if field in cleaned else existing.get(field, model.get(field))

    lease_type, inc = after(_LEASE_TYPE), after(_INC)
    errors = []
    if _INC in cleaned and cleaned[_INC] is not None and lease_type == "Wet":
        errors.append((_INC, "Agreed value is not used for a wet lease — change Lease Type first"))
    if model.get(_LEASE_TYPE) == "Wet" and lease_type != "Wet" and inc is None:
        errors.append((_LEASE_TYPE, "This aircraft was wet-leased, so the forecast has no agreed value "
                                    f"for it — set {_INC} in the same save"))
    return errors


# The sheet's source row as JSON, the model's values of the carried fields for it, and its overrides.
_BASE_SQL = f"""
SELECT CAST(to_jsonb(src) AS text) AS src,
       CAST(forecast.aircraft_info_raw(src."Airline", src."Registration", src."Contract Year",
                                       src."Data Type") AS text) AS raw,
       CAST(e.edits AS text) AS edits
FROM {_SOURCE} src
LEFT JOIN forecast.aircraft_info_edits e
       ON e.airline = src."Airline" AND e.registration = src."Registration"
      AND e.contract_year = src."Contract Year"
WHERE src.id = :id
"""


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
        "name), `type` is text | year | integer | musd (millions of USD, two decimals) | choice "
        "(one of `options`), and every field accepts null (an empty cell = unknown; for a choice, "
        "\"Not Leased\"). `affects_report` marks the fields the forecast report carries: a save of "
        "one leaves the report behind until it is recalculated (POST /apply). `derived` lists the "
        "columns computed from Agreed Value / INC, which cannot be edited themselves."
    ),
    responses=build_responses(include={status.HTTP_200_OK}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def list_editable_fields(request: Request, response: Response):
    fields = []
    for field, kind in _FIELDS.items():
        spec = {"field": field, "type": kind, "nullable": True, "affects_report": field in _CARRIED}
        if kind == _TEXT:
            spec["max_length"] = _TEXT_MAX
        elif kind == _YEAR:
            spec.update(min=_YEAR_MIN, max=_YEAR_MAX)
        elif kind == _INT:
            spec.update(min=0, max=_INT_MAX[field])
        elif kind == _CHOICE:
            spec["options"] = list(_CHOICES[field])
        else:
            spec.update(min=0, max=str(_MUSD_MAX), decimals=2)
        fields.append(spec)
    return success_response(request=request, response=response, data={
        "fields": fields,
        "derived": [{"field": f, "from": _INC} for f in _DERIVED],
    })


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


_STATUS_SQL = """
SELECT ls.snapshot_id, ls.refreshed_at, s.as_of, s.created_at AS snapshot_created_at,
       s.covered_operators, s.edits_applied_at,
       coalesce(p.airlines, CAST('{}' AS text[])) AS pending_airlines,
       p.pending_since, p.last_change_at
FROM forecast.acys_live_state ls
LEFT JOIN forecast.acys_snapshots s ON s.id = ls.snapshot_id
LEFT JOIN LATERAL (
    SELECT array_agg(mk.airline ORDER BY mk.airline) AS airlines,
           min(coalesce(mk.pending_since, mk.changed_at)) AS pending_since,
           max(mk.changed_at) AS last_change_at
    FROM forecast.aircraft_info_edit_marks mk
    WHERE (ls.refreshed_at IS NULL OR mk.changed_at > ls.refreshed_at)
      AND (s.id IS NULL OR mk.airline = ANY(s.covered_operators))
) p ON true
WHERE ls.id = 1
"""


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


@router.get(
    "/status",
    description=(
        "Is the forecast report behind the fleet-sheet edits? `snapshot_id` is the saved run the "
        "report shows now (null while a run or restore is rewriting it, or if the last one did not "
        "finish), `refreshed_at` when the report last took the edits in, and `pending_airlines` the "
        "airlines of that run edited since — `pending` is true when there are any. "
        "`last_change_at` is the latest of those edits and `pending_since` the oldest one the report "
        "has not taken in: the worker applies them by itself once `last_change_at` is 10 s old or "
        "`pending_since` 60 s old (checked every 15 s). POST /apply does it at once."
    ),
    responses=build_responses(include=_OK),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def edits_status(request: Request, response: Response):
    try:
        async with request.app.state.db_client.read_session(_DB) as session:
            row = (await session.execute(text(_STATUS_SQL))).mappings().first() or {}
        pending = list(row.get("pending_airlines") or [])
        return success_response(request=request, response=response, data={
            "snapshot_id": row.get("snapshot_id"),
            "snapshot_created_at": _iso(row.get("snapshot_created_at")),
            "as_of": _iso(row.get("as_of")),
            "covered_operators": list(row.get("covered_operators") or []),
            "refreshed_at": _iso(row.get("refreshed_at")),
            "edits_applied_at": _iso(row.get("edits_applied_at")),
            "pending": bool(pending),
            "pending_airlines": pending,
            "pending_since": _iso(row.get("pending_since")),
            "last_change_at": _iso(row.get("last_change_at")),
        })
    except Exception as ex:
        logger.error(f"edits_status failed: {ex}")
        return error_response(request=request, response=response, exc=ex)


@router.post(
    "/apply",
    description=(
        "Bring the forecast report up to date with the fleet-sheet edits NOW — the worker also does "
        "it by itself within about a minute of the last save, so this is only for 'I want it this "
        "second'. Re-renders the saved run "
        "the report shows now (GET /status), with every current edit laid over it. Nothing is "
        "fetched and nothing is forecast — the run is already loaded, so only the report is "
        "refreshed — and the SAME snapshot is updated, no new one is made. Same job and status "
        "contract as POST /forecast/ with a snapshot_id (which does the same for any saved run). "
        "409 when a forecast is already in progress, or when the report does not show a saved run."
    ),
    status_code=status.HTTP_202_ACCEPTED,
    responses=build_responses(include={status.HTTP_202_ACCEPTED, status.HTTP_404_NOT_FOUND,
                                       status.HTTP_409_CONFLICT, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def apply_edits(request: Request, response: Response):
    from .Forecast import _start_restore   # lazy: one enqueue path for both entry points
    try:
        async with request.app.state.db_client.read_session(_DB) as session:
            snapshot_id = (await session.execute(text(
                "SELECT snapshot_id FROM forecast.acys_live_state WHERE id = 1"))).scalar()
        if snapshot_id is None:
            return warning_response(
                request=request, response=response,
                msg="The report does not show a saved forecast run right now (one is being built, "
                    "or the last one did not finish). Start a forecast run instead — it picks up "
                    "every edit by itself.",
                status_code=status.HTTP_409_CONFLICT)
        return await _start_restore(int(snapshot_id), request, response)
    except Exception as ex:
        logger.error(f"apply_edits failed: {ex}")
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
        "an empty string sets the cell to unknown. Sending the model's value removes that field's "
        "override. The source data is never changed. Returns the updated row (Decimal as a "
        "string) and its new `ETag`; an optional `If-Match` refuses the write with 412 when the row "
        "changed since it was read. Invalid values are a 422 with `data: [{field, msg}]`. "
        "Seats / Agreed Value INC / Lease / Lease Type also change the forecast REPORT once it is "
        "recalculated (the row then reads `Recalculation Pending`; POST /apply); AVE / AW AVE / EXP "
        "follow INC and are not editable. Agreed value is not used for a wet lease, so INC is "
        "refused on one, and turning a wet lease dry needs an INC in the same or an earlier save."
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
        if field in _DERIVED:
            errors.append((field, f"{field} is calculated from {_INC} — edit that instead"))
            continue
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
            # What an edit is compared against: the sheet's source, with the fields the report carries
            # taken from the MODEL (once recalculated, the source already holds the edit itself), plus
            # the overrides this row already has.
            base = (await session.execute(text(_BASE_SQL), {"id": row_id})).first()
            model = {**json.loads(base.src), **json.loads(base.raw or "{}")}
            existing = json.loads(base.edits) if base.edits else {}

            rule_errors = _lease_rules(cleaned, existing, model)
            if rule_errors:
                raise _validation_error(rule_errors)

            put = {f: v for f, v in cleaned.items() if not _same(f, v, model.get(f))}
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
    except RequestValidationError:
        raise   # nothing was written; the app's handler renders the 422
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

"""Forecast panel trigger + last-request read-back + the model's tuning profiles.

POST /forecast/          — validate (operator and/or registrations) and enqueue the external-worker
                           `forecast_panel` job (build forecast.acys_actuals from Cirium × FR24, merge
                           into forecast.acys_summary_by_day). Returns the job_id. The worker publishes
                           a SEQUENTIAL status per step, read from /status/{job_id} (poll) or
                           /status/stream (SSE), and writes the service.forecast_last_requests row ONLY
                           after the whole panel finishes successfully (not at trigger time).
                           WITH `snapshot_id` INSTEAD OF A SCOPE it computes NOTHING: it enqueues
                           `forecast_restore`, which pours that saved run back into
                           acys_summary_by_day and refreshes the same report matviews. Same job_id and
                           status contract, three steps instead of ten.
                           A REQUEST THAT ALREADY RAN TODAY IS NOT REBUILT: the worker pours that
                           run back instead (same three steps), since the same day's scope, as-of
                           date and model parameters produce the same dataset. `force: true`
                           rebuilds anyway.
GET  /forecast/last      — the most recent trigger (datetime + request_type + params).
GET  /forecast/snapshots — the saved runs of the last 30 days, filterable by registration(s),
                           operator(s) and date. The id listed here is what POST /forecast/ takes as
                           `snapshot_id`.

WHY SNAPSHOTS EXIST: acys_summary_by_day holds exactly ONE run — every request TRUNCATEs it and
rebuilds it. Each successful run therefore copies its finished dataset into forecast.acys_snapshots +
forecast.acys_snapshot_rows, which external-worker prunes to FORECAST_SNAPSHOT_RETENTION_DAYS (30) at
the end of the next run. Re-showing a run is then a copy and a matview refresh instead of an hour of
fetching and forecasting.

TUNING PROFILES (service.forecast_profiles) — what the portal's settings screen talks to:
GET    /forecast/params/schema  — the FORM DESCRIPTOR: every knob with type/bounds/default/label/
                                  description/group. The portal renders its form from THIS, so adding a
                                  knob never needs a portal release.
GET    /forecast/profiles       — list; POST — create; PATCH /{name} — edit; DELETE /{name} — remove;
POST   /forecast/profiles/{name}/default — make it the profile used when a run names none.

A profile's `params` holds OVERRIDES ONLY (absent key = the spec's default), and every write is resolved
through `Utils.forecast_params.resolve` before it is stored — so an invalid knob is rejected at SAVE time,
with a message naming the offending field, instead of surfacing as a broken forecast an hour later.
external-worker resolves again on read (the row is reachable outside this API).

acys_actuals accumulates across requests (this operator/tail slice is refreshed);
acys_forecast/acys_summary are per-request. A `queued` status row is written up front so the job is
pollable immediately.
"""
import json
import uuid
from datetime import date as date_cls
from typing import Optional, List, Dict, Any

from fastapi import Request, Response, Depends, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, text

from Config import setup_logger
from settings import Router
from Queue import EXTERNAL_QUEUE
from Database import JobStatus
from Database.ServiceModels import ForecastLastRequest, ForecastProfile
from api_auth import authorize, SCOPE_PREDICTIVE_WRITE, SCOPE_PREDICTIVE_READ
from Utils import success_response, error_response
from Utils.ResponsesFunc import build_responses, warning_response
from Utils.forecast_params import (MODEL_VERSION, ForecastParamError, describe, resolve)

logger = setup_logger("forecast_api")

router = Router(prefix="/forecast", tags=["Forecast"])

_REF = "forecast_panel"
_RESTORE_REF = "forecast_restore"   # the job that re-shows a saved run (no fetch, no model)
_REQUEST_TYPE = "ACYS"              # current Cirium×FR24 panel algorithm
_STATUS_CHANNEL = "status:events"   # must match the workers' status.py / StatusCheck.py
_DB = "aixii"                       # logical name -> the physical aixii database (forecast schema)

# The listing window. Kept equal to external-worker's FORECAST_SNAPSHOT_RETENTION_DAYS on purpose: a
# wider window here would list runs the worker has already pruned, and a narrower one would hide runs
# that are still restorable.
_SNAPSHOT_WINDOW_DAYS = 30


class ForecastRequest(BaseModel):
    operators: Optional[List[str]] = Field(
        None, description='One or more Cirium "Operator" values, e.g. ["Avianca", "Emirates"]. Each is '
                          "forecast in turn within a single run; the scope is their union (plus any "
                          "registrations).")
    operator: Optional[str] = Field(
        None, description='Deprecated single-operator form, kept for back-compat; folded into "operators".')
    registrations: Optional[List[str]] = Field(
        None, description="One or more Cirium registrations. May be used alone OR together with "
                          "operators (the scope is the union).")
    date: Optional[date_cls] = Field(
        None, description="As-of date YYYY-MM-DD; history window = [2023-07-01, date), default today. "
                          "Also the Contract Year anchor.")
    profile: Optional[str] = Field(
        None, description="Name of a service.forecast_profiles tuning profile. Omitted, the default "
                          "profile is used. A named profile that does not exist fails the run rather "
                          "than silently falling back.")
    force: bool = Field(
        False,
        description="Rebuild even if this exact request already ran today. Left false, a repeat of "
                    "the same scope / as-of date / model parameters on the same day pours that run "
                    "back instead of spending an hour reproducing it. Set it when the SOURCE data "
                    "has moved under an unchanged request — a Cirium revision loaded since the "
                    "morning run, say — which is the one thing the reuse check cannot see.")
    snapshot_id: Optional[int] = Field(
        None, ge=1,
        description="RE-SHOW A SAVED RUN instead of producing a new one: the id of a row from "
                    "GET /forecast/snapshots. Nothing is fetched and nothing is forecast — the saved "
                    "dataset is poured back into the report table and the report matviews are "
                    "refreshed. Mutually exclusive with operators / registrations / date / profile, "
                    "which all describe a run that would be COMPUTED.")

    def norm_operators(self) -> List[str]:
        """The de-duplicated, stripped operator list — merging the legacy single `operator` in."""
        raw = list(self.operators or []) + ([self.operator] if self.operator else [])
        seen, out = set(), []
        for o in raw:
            o = (o or "").strip()
            if o and o not in seen:
                seen.add(o); out.append(o)
        return out

    @model_validator(mode="after")
    def _at_least_one_mode(self):
        """Exactly one of the two modes. A body carrying BOTH a snapshot_id and a scope is rejected
        rather than silently resolved: either reading of it (re-show the saved run / compute the
        scope) would be a guess, and the two differ by an hour of work and a different dataset."""
        scope = bool(self.norm_operators() or self.registrations)
        if self.snapshot_id is not None:
            if scope or self.date is not None or self.profile is not None or self.force:
                raise ValueError("snapshot_id re-shows a saved run: send it alone, without "
                                 "operators / registrations / date / profile / force")
            return self
        if not scope:
            raise ValueError("provide operators and/or registrations, or a snapshot_id")
        return self


async def _mark_queued(request: Request, job_id: str, label: str, *, ref: str = _REF,
                       what: str = "forecast panel") -> None:
    """Insert a `queued` job_statuses row and publish it, so /status is populated immediately."""
    msg = f"Queued: {what} for {label}"
    async with request.app.state.db_client.session("service") as session:
        session.add(JobStatus(job_id=job_id, kind="external", ref=ref,
                              state="queued", progress=0, message=msg))
        await session.commit()
    try:
        await request.app.state.redis.publish(_STATUS_CHANNEL, json.dumps({
            "job_id": job_id, "kind": "external", "ref": ref,
            "state": "queued", "progress": 0, "message": msg,
        }))
    except Exception as _ex:
        # The durable row is already written, so nothing is lost - but a client watching
        # /status/stream will never see this job appear, and in silence there was no way to find
        # out why.
        logger.warning("could not publish the queued status for %s: %s", job_id, _ex)


# The columns a listing row is built from. `covered_registrations` is COUNTED, not listed: an
# operator-scoped run covers its whole fleet, and a few hundred tails per row would make the listing
# heavier than the thing it lists. The filter still matches against the full array server-side.
_SNAPSHOT_COLS = """
    s.id, s.created_at, s.job_id, s.request_type, s.operators, s.registrations,
    s.covered_operators, cardinality(s.covered_registrations) AS covered_registration_count,
    s.as_of, s.profile, s.row_count, s.restored_at, s.restore_count, s.edits_applied_at,
    s.id IS NOT DISTINCT FROM (SELECT ls.snapshot_id FROM forecast.acys_live_state ls WHERE ls.id = 1)
        AS is_live
"""

_SNAPSHOT_ONE_SQL = f"SELECT {_SNAPSHOT_COLS} FROM forecast.acys_snapshots s WHERE s.id = :sid"

# Every filter is written as "no value sent OR it matches", so any combination of them ANDs together
# with no branching in Python — operator AND date is the same statement as operator alone.
#
# Scope matching reads the REQUESTED arrays and the COVERED ones as one list (`||`): an operator-scoped
# run names no tails, yet its dataset holds every tail of that operator, so a search for a tail has to
# look at what the run CONTAINS, not only at what was typed to start it. Matching is case-insensitive
# and trimmed on both sides, because a registration reaches us from a form as often as from the model.
#
# CAST(:p AS text[]) rather than a bare :p — a NULL parameter with no inferable type is one asyncpg
# refuses to send at all ("could not determine data type"), and an inline ::cast is the footgun this
# codebase has been bitten by before.
_SNAPSHOT_LIST_SQL = f"""
SELECT {_SNAPSHOT_COLS}, count(*) OVER () AS total_count
FROM forecast.acys_snapshots s
WHERE s.created_at >= now() - make_interval(days => :window)
  AND (CAST(:regs AS text[]) IS NULL OR EXISTS (
        SELECT 1 FROM unnest(s.registrations || s.covered_registrations) AS r(v)
        WHERE upper(btrim(r.v)) = ANY(CAST(:regs AS text[]))))
  AND (CAST(:ops AS text[]) IS NULL OR EXISTS (
        SELECT 1 FROM unnest(s.operators || s.covered_operators) AS o(v)
        WHERE lower(btrim(o.v)) = ANY(CAST(:ops AS text[]))))
  AND (CAST(:day AS date) IS NULL
       OR CAST(s.created_at AT TIME ZONE 'UTC' AS date) = CAST(:day AS date))
  AND (CAST(:as_of AS date) IS NULL OR s.as_of = CAST(:as_of AS date))
ORDER BY s.created_at DESC
LIMIT :limit OFFSET :offset
"""


def _snapshot_out(row) -> dict:
    """One saved run as the portal lists it. `id` is what POST /forecast/ takes as `snapshot_id`."""
    return {
        "id": int(row["id"]),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "job_id": row["job_id"],
        "request_type": row["request_type"],
        # what was ASKED FOR — empty for the other mode (an operator run names no tails and vice versa)
        "operators": list(row["operators"] or []),
        "registrations": list(row["registrations"] or []),
        # what the dataset actually HOLDS
        "covered_operators": list(row["covered_operators"] or []),
        "covered_registration_count": int(row["covered_registration_count"] or 0),
        "as_of": row["as_of"].isoformat() if row["as_of"] else None,
        "profile": row["profile"],
        "row_count": int(row["row_count"] or 0),
        "restored_at": row["restored_at"].isoformat() if row["restored_at"] else None,
        "restore_count": int(row["restore_count"] or 0),
        # when the fleet-sheet edits were last laid over this run (by the run itself, a restore, or
        # POST /forecast/aircraft-details/apply) — the snapshot is re-stamped, never copied
        "edits_applied_at": row["edits_applied_at"].isoformat() if row["edits_applied_at"] else None,
        # the run the report shows right now: POST /forecast/ with this id only refreshes the report
        "is_live": bool(row["is_live"]),
    }


# ── ONE FORECAST AT A TIME ─────────────────────────────────────────────────────────────────────
# A run TRUNCATEs and rebuilds forecast.acys_summary_by_day, a SINGLE-RUN staging table. Two runs
# overlapping interleave: the second TRUNCATE lands before the first INSERT and both datasets end
# up in the table. That happened on 2026-09-18, was frozen into a snapshot, and surfaced days later
# as a refresh failing on a unique index.
#
# external-worker now refuses the second run with an advisory lock, which is the guarantee. This is
# the COURTESY: refusing here means the caller gets an immediate 409 naming the run already in
# flight, instead of a request that is accepted, queued, started, and only then fails.
#
# A restore counts: it TRUNCATEs and refills the same table.
_BUSY_REFS = (_REF, _RESTORE_REF)
_TERMINAL_STATES = ("success", "error", "skipped", "cancelled")   # == worker status.py

# A row left behind by a worker that died without publishing a terminal state must not block every
# forecast for ever. A live run republishes its progress continuously, so a row this stale is not
# running any more. If the window is ever too short the worker's lock still refuses the overlap —
# this only decides how early the caller hears about it.
_BUSY_STALE_AFTER_MINUTES = 15

_ACTIVE_FORECAST_SQL = """
SELECT job_id, ref, state, message, progress, updated_at
FROM job_statuses
WHERE ref = ANY(:refs)
  AND state <> ALL(:terminal)
  AND updated_at > now() - make_interval(mins => :stale)
ORDER BY updated_at DESC
LIMIT 1
"""


async def _forecast_in_flight(request: Request):
    """The forecast job already queued or running, or None. Reads the SERVICE database, which is
    where job_statuses lives."""
    async with request.app.state.db_client.read_session("service") as session:
        return (await session.execute(text(_ACTIVE_FORECAST_SQL), {
            "refs": list(_BUSY_REFS),
            "terminal": list(_TERMINAL_STATES),
            "stale": _BUSY_STALE_AFTER_MINUTES,
        })).mappings().first()


def _busy_response(busy, request: Request, response: Response):
    """409 naming the run in flight and how to get out of the way."""
    what = "restoring a saved run" if busy["ref"] == _RESTORE_REF else "building a forecast"
    detail = f" — {busy['message']}" if busy["message"] else ""
    return warning_response(
        request=request, response=response,
        msg=(f"A forecast is already in progress ({what}, job {busy['job_id']}, "
             f"state {busy['state']}{detail}). Only one runs at a time, because they share one "
             f"staging table. Wait for it to finish, or cancel it with "
             f"POST /status/{busy['job_id']}/cancel."),
        status_code=status.HTTP_409_CONFLICT)


async def _start_restore(snapshot_id: int, request: Request, response: Response):
    """POST /forecast/ with a snapshot_id: enqueue `forecast_restore` instead of the panel.

    The saved run is only LOOKED UP here (a 404 for one the retention window has already dropped is
    worth more than a job that fails a minute later); the work itself belongs to the worker, which
    owns the report tables — core-api's role may read forecast.* but not TRUNCATE it or REFRESH a
    matview, and the refresh is minutes of work that must not sit inside an HTTP request."""
    async with request.app.state.db_client.read_session(_DB) as session:
        row = (await session.execute(text(_SNAPSHOT_ONE_SQL),
                                     {"sid": snapshot_id})).mappings().first()
    if row is None:
        return warning_response(
            request=request, response=response,
            msg=f"Saved forecast run {snapshot_id} not found — it may have passed the "
                f"{_SNAPSHOT_WINDOW_DAYS}-day retention window",
            status_code=status.HTTP_404_NOT_FOUND)

    busy = await _forecast_in_flight(request)
    if busy is not None:
        return _busy_response(busy, request, response)

    snapshot = _snapshot_out(row)
    job_id = uuid.uuid4().hex
    await _mark_queued(request, job_id, f"saved run {snapshot_id}",
                       ref=_RESTORE_REF, what="forecast restore")
    await request.state.arq.enqueue_job(
        _RESTORE_REF,
        snapshot_id=snapshot_id,
        correlation_id=request.state.correlation_id,
        _job_id=job_id,
        _queue_name=EXTERNAL_QUEUE,
    )
    return success_response(
        request=request, response=response,
        data={"job_id": job_id, "mode": "snapshot", "snapshot": snapshot},
        msg=f"Restoring saved forecast run {snapshot_id}",
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.post(
    path="/",
    description="Start the forecast panel build (Cirium × FR24 → forecast.acys_summary_by_day; "
                "grouped rollup in the forecast.acys_summary_grouped view) for one or more operators "
                "and/or a list of registrations. Multiple operators are each forecast within one run. "
                "A request identical to one that already ran TODAY (same scope, as-of date and model "
                "parameters) is NOT rebuilt — the worker pours that run back, three steps instead of "
                "ten; send `force: true` to rebuild it anyway. "
                "Send `snapshot_id` INSTEAD of a scope to re-show a saved run (GET /forecast/snapshots): "
                "nothing is fetched or forecast — the saved dataset is poured back into the report table "
                "and the report matviews are refreshed. Both forms return a job_id and report progress "
                "the same way. ONE AT A TIME: asked while a forecast is already queued or running, this "
                "returns 409 naming that job rather than enqueuing a second one — both forms rebuild the "
                "same single-run staging table, so two of them overlapping would corrupt it.",
    status_code=status.HTTP_202_ACCEPTED,
    responses=build_responses(include={
        status.HTTP_202_ACCEPTED, status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT,
        status.HTTP_422_UNPROCESSABLE_ENTITY, status.HTTP_500_INTERNAL_SERVER_ERROR,
    }),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def start_forecast(body: ForecastRequest, request: Request, response: Response):
    try:
        # Re-showing a saved run shares this endpoint (one button, one contract) but none of its work:
        # no scope to validate, no panel to enqueue. The body validator has already ruled out a request
        # that asks for both.
        if body.snapshot_id is not None:
            return await _start_restore(body.snapshot_id, request, response)

        operators = body.norm_operators()
        registrations = [r.strip() for r in body.registrations if r and r.strip()] if body.registrations else None
        registrations = registrations or None

        # validate the COMBINED scope (any listed operator's tails OR the explicit regs) matches something
        clauses, params = [], {}
        if operators:
            clauses.append('"Operator" = ANY(:ops)'); params["ops"] = operators
        if registrations:
            clauses.append('"Registration" = ANY(:regs)'); params["regs"] = registrations
        where = "(" + " OR ".join(clauses) + ")"
        async with request.app.state.db_client.read_session("cirium") as session:
            found = (await session.execute(
                text(f'SELECT 1 FROM cirium.ciriumaircrafts WHERE {where} LIMIT 1'), params)).first()
        if found is None:
            return warning_response(request=request, response=response,
                                    msg="No Cirium aircraft match the given operators / registrations",
                                    status_code=status.HTTP_404_NOT_FOUND)

        label = " + ".join(([f"{len(operators)} operator(s)"] if operators else [])
                           + ([f"{len(registrations)} registration(s)"] if registrations else []))
        as_of = body.date.isoformat() if body.date else None

        busy = await _forecast_in_flight(request)
        if busy is not None:
            return _busy_response(busy, request, response)

        # own job_id so the queued row is written BEFORE the worker's first publish (no upsert race)
        job_id = uuid.uuid4().hex
        await _mark_queued(request, job_id, label)

        # NOTE: the forecast_last_requests row is written by the WORKER, and ONLY after the whole
        # panel finishes successfully — not here at trigger time (a failed/cancelled run leaves no row).

        await request.state.arq.enqueue_job(
            _REF,
            operators=operators or None,
            registrations=registrations,
            as_of=as_of,
            profile=body.profile,
            force=body.force,
            correlation_id=request.state.correlation_id,
            _job_id=job_id,
            _queue_name=EXTERNAL_QUEUE,
        )
        return success_response(
            request=request, response=response,
            data={"job_id": job_id, "operators": operators, "registrations": registrations,
                  "as_of": as_of, "profile": body.profile, "force": body.force},
            # Whether this one rebuilds or reuses today's run is the WORKER's call — it is the side
            # that resolves the model parameters the decision turns on. The answer arrives on the
            # status stream (three steps and `reused_today` in the summary mean it was reused), not
            # here, so this message must not promise a build.
            msg="Forecast panel requested", status_code=status.HTTP_202_ACCEPTED,
        )
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(
    path="/last",
    description="The most recent POST /forecast/ trigger (datetime + request_type + params). "
                "Optionally filter by request_type.",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND,
                                        status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def last_forecast(
    request: Request, response: Response,
    request_type: Optional[str] = Query(None, description="Filter by request_type (e.g. 'ACYS')."),
):
    try:
        stmt = select(ForecastLastRequest).order_by(ForecastLastRequest.created_at.desc()).limit(1)
        if request_type:
            stmt = stmt.where(ForecastLastRequest.request_type == request_type)
        async with request.app.state.db_client.read_session("service") as session:
            row = (await session.execute(stmt)).scalars().first()
        if row is None:
            return warning_response(request=request, response=response,
                                    msg="No forecast request recorded yet",
                                    status_code=status.HTTP_404_NOT_FOUND)
        return success_response(request=request, response=response, data={
            "datetime": row.created_at.isoformat() if row.created_at else None,
            "request_type": row.request_type,
            "request_params": row.request_params,
        })
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(
    path="/snapshots",
    description=f"The forecast runs saved over the last {_SNAPSHOT_WINDOW_DAYS} days, newest first. "
                "Filters COMBINE (they are ANDed): registration + date lists only the runs that cover "
                "one of those tails AND were produced on that date. A registration matches a run that "
                "ASKED for it or whose dataset CONTAINS it, so an operator-scoped run is found by any "
                "of its tails. `id` is what POST /forecast/ takes as `snapshot_id` to re-show the run.",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def list_snapshots(
    request: Request, response: Response,
    registration: Optional[List[str]] = Query(
        None, description="Registration(s), repeatable — ?registration=N123AB&registration=N456CD. "
                          "Case-insensitive. A run matches if it was requested for the tail OR its "
                          "dataset contains it."),
    operator: Optional[List[str]] = Query(
        None, description="Airline / Cirium Operator name(s), repeatable. Case-insensitive, matched "
                          "in full (not a substring)."),
    date: Optional[date_cls] = Query(
        None, description="The DAY THE RUN WAS PRODUCED (UTC), YYYY-MM-DD. For the run's as-of date "
                          "(the Contract Year anchor it was built around) use `as_of` instead."),
    as_of: Optional[date_cls] = Query(
        None, description="The run's own as-of date, YYYY-MM-DD."),
    limit: int = Query(100, ge=1, le=500, description="Page size."),
    offset: int = Query(0, ge=0, description="Rows to skip; `total` in the response is the unpaged count."),
):
    try:
        # Normalised HERE, once, so the SQL compares like with like: registrations upper-cased (the
        # model stores them that way, portals rarely do), operator names lower-cased.
        regs = [r.strip().upper() for r in (registration or []) if r and r.strip()] or None
        ops = [o.strip().lower() for o in (operator or []) if o and o.strip()] or None
        async with request.app.state.db_client.read_session(_DB) as session:
            rows = (await session.execute(text(_SNAPSHOT_LIST_SQL), {
                "window": _SNAPSHOT_WINDOW_DAYS, "regs": regs, "ops": ops,
                # the date objects themselves: CAST(:day AS date) makes the driver infer the
                # parameter's type as `date`, and asyncpg then refuses an isoformat string.
                "day": date, "as_of": as_of,
                "limit": limit, "offset": offset,
            })).mappings().all()
        total = int(rows[0]["total_count"]) if rows else 0
        return success_response(request=request, response=response, data={
            "total": total, "limit": limit, "offset": offset,
            "retention_days": _SNAPSHOT_WINDOW_DAYS,
            "filters": {"registration": regs, "operator": ops,
                        "date": date.isoformat() if date else None,
                        "as_of": as_of.isoformat() if as_of else None},
            "items": [_snapshot_out(r) for r in rows],
        })
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


# ── Tuning profiles ────────────────────────────────────────────────────────────────────────────────────
# The knobs that used to be constants in external-worker's ForecastAPI/model.py. The portal edits them
# here; the worker reads the profile at the start of each run. Utils/forecast_params.py holds the spec
# (defaults, types, bounds, labels); this router only stores and validates against it.

class ProfileIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=128,
                      description="Unique identifier, e.g. 'default' or 'aggressive-seasonality'.")
    description: Optional[str] = Field(None, description="Free text: what this profile is for.")
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="OVERRIDES ONLY — an absent knob keeps its default. Validated against "
                    "GET /forecast/params/schema; an unknown key or an out-of-range value is a 422.")
    is_default: bool = Field(False, description="Use this profile when a run names none. At most one.")
    enabled: bool = Field(True, description="A disabled profile cannot be used by a run.")


class ProfilePatch(BaseModel):
    """Every field optional — only what is sent changes. `params` REPLACES the override set rather than
    merging: with a merge there would be no way to REMOVE an override, since an absent key already means
    'use the default'."""
    description: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    is_default: Optional[bool] = None
    enabled: Optional[bool] = None


def _profile_out(row: ForecastProfile) -> dict:
    """One profile as the portal sees it: the stored overrides AND the resolved effective values, so the
    form can show "what is set here" beside "what a run will actually use" without re-implementing the
    defaulting rules in the frontend."""
    try:
        effective = resolve(row.params, model_version=row.model_version)
        error = None
    except ForecastParamError as e:
        # A row stored before a MODEL_VERSION bump, or hand-edited in the database, must stay VISIBLE —
        # that is how it gets fixed. Report the problem per-row instead of failing the whole listing.
        effective, error = None, str(e)
    return {
        "name": row.name,
        "description": row.description,
        "model_version": row.model_version,
        "params": row.params or {},
        "effective": {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                      for k, v in effective.items()} if effective else None,
        "error": error,
        "is_default": row.is_default,
        "enabled": row.enabled,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": row.updated_by,
    }


async def _clear_default(session, *, keep_name: Optional[str] = None) -> None:
    """Drop is_default from every OTHER profile. At most one default is a DATABASE guarantee (a partial
    unique index), so this must run in the SAME transaction as the SET that follows or the index rejects
    the write."""
    stmt = text("UPDATE forecast_profiles SET is_default = false WHERE is_default"
                + (" AND name <> :n" if keep_name else ""))
    await session.execute(stmt, {"n": keep_name} if keep_name else {})


@router.get(
    path="/params/schema",
    description="The forecast model's tunable parameters: type, bounds, default, label, description and "
                "form group for each. The portal renders its settings form from this, so adding a knob "
                "needs no portal release.",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def params_schema(request: Request, response: Response):
    try:
        return success_response(request=request, response=response, data=describe())
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(
    path="/profiles",
    description="All tuning profiles, each with its stored overrides and its resolved effective values.",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_READ))],
)
async def list_profiles(request: Request, response: Response):
    try:
        async with request.app.state.db_client.read_session("service") as session:
            rows = (await session.execute(
                select(ForecastProfile).order_by(ForecastProfile.is_default.desc(),
                                                 ForecastProfile.name))).scalars().all()
            data = [_profile_out(r) for r in rows]
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/profiles",
    description="Create a tuning profile. `params` holds overrides only and is validated against "
                "/forecast/params/schema.",
    status_code=status.HTTP_201_CREATED,
    responses=build_responses(include={status.HTTP_201_CREATED, status.HTTP_409_CONFLICT,
                                       status.HTTP_422_UNPROCESSABLE_ENTITY,
                                       status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def create_profile(body: ProfileIn, request: Request, response: Response):
    try:
        try:
            resolve(body.params)          # reject bad knobs BEFORE anything is stored
        except ForecastParamError as e:
            return warning_response(request=request, response=response, msg=str(e),
                                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
        async with request.app.state.db_client.session("service") as session:
            exists = (await session.execute(
                select(ForecastProfile).where(ForecastProfile.name == body.name))).scalars().first()
            if exists is not None:
                return warning_response(request=request, response=response,
                                        msg=f"Profile '{body.name}' already exists",
                                        status_code=status.HTTP_409_CONFLICT)
            if body.is_default:
                await _clear_default(session)
            row = ForecastProfile(name=body.name, description=body.description,
                                  model_version=MODEL_VERSION, params=body.params,
                                  is_default=body.is_default, enabled=body.enabled,
                                  updated_by=getattr(request.state, "caller", None))
            session.add(row)
            await session.commit()
            await session.refresh(row)
            data = _profile_out(row)
        return success_response(request=request, response=response, data=data,
                                msg=f"Profile '{body.name}' created",
                                status_code=status.HTTP_201_CREATED)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(
    path="/profiles/{name}",
    description="Edit a profile. Only the fields sent change. `params` REPLACES the override set — it is "
                "not merged, so an override is removed by omitting it.",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND,
                                       status.HTTP_422_UNPROCESSABLE_ENTITY,
                                       status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def update_profile(name: str, body: ProfilePatch, request: Request, response: Response):
    try:
        if body.params is not None:
            try:
                resolve(body.params)
            except ForecastParamError as e:
                return warning_response(request=request, response=response, msg=str(e),
                                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
        async with request.app.state.db_client.session("service") as session:
            row = (await session.execute(
                select(ForecastProfile).where(ForecastProfile.name == name))).scalars().first()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Profile '{name}' not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            if body.description is not None:
                row.description = body.description
            if body.params is not None:
                row.params = body.params
                # the overrides are now written in TODAY's vocabulary — record which one
                row.model_version = MODEL_VERSION
            if body.enabled is not None:
                row.enabled = body.enabled
            if body.is_default is not None:
                if body.is_default:
                    await _clear_default(session, keep_name=name)
                row.is_default = body.is_default
            row.updated_by = getattr(request.state, "caller", None)
            await session.commit()
            await session.refresh(row)
            data = _profile_out(row)
        return success_response(request=request, response=response, data=data,
                                msg=f"Profile '{name}' updated")
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/profiles/{name}/default",
    description="Make this the profile used by runs that name none. Clears the flag from the previous "
                "default in the same transaction.",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND,
                                       status.HTTP_422_UNPROCESSABLE_ENTITY,
                                       status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def set_default_profile(name: str, request: Request, response: Response):
    try:
        async with request.app.state.db_client.session("service") as session:
            row = (await session.execute(
                select(ForecastProfile).where(ForecastProfile.name == name))).scalars().first()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Profile '{name}' not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            if not row.enabled:
                return warning_response(request=request, response=response,
                                        msg=f"Profile '{name}' is disabled and cannot be the default",
                                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
            await _clear_default(session, keep_name=name)
            row.is_default = True
            row.updated_by = getattr(request.state, "caller", None)
            await session.commit()
            await session.refresh(row)
            data = _profile_out(row)
        return success_response(request=request, response=response, data=data,
                                msg=f"Profile '{name}' is now the default")
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(
    path="/profiles/{name}",
    description="Delete a profile. The current default cannot be deleted — promote another one first, so "
                "a run can never be left with no profile to read.",
    responses=build_responses(include={status.HTTP_200_OK, status.HTTP_404_NOT_FOUND,
                                       status.HTTP_422_UNPROCESSABLE_ENTITY,
                                       status.HTTP_500_INTERNAL_SERVER_ERROR}),
    dependencies=[Depends(authorize(SCOPE_PREDICTIVE_WRITE))],
)
async def delete_profile(name: str, request: Request, response: Response):
    try:
        async with request.app.state.db_client.session("service") as session:
            row = (await session.execute(
                select(ForecastProfile).where(ForecastProfile.name == name))).scalars().first()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Profile '{name}' not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            if row.is_default:
                return warning_response(
                    request=request, response=response,
                    msg=f"Profile '{name}' is the default; make another profile the default first",
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
            await session.delete(row)
            await session.commit()
        return success_response(request=request, response=response, data={"name": name},
                                msg=f"Profile '{name}' deleted")
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)

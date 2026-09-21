"""The change log: `/history`.

Every INSERT, UPDATE and DELETE on the insured-fleet tables lands in `audit.change_log`, written by
one database trigger. This router reads it back the way a UI wants it: each entry carries a
field-level `changes` list with foreign keys already resolved to names, so an info button needs no
further lookups, plus the raw `old_row` / `new_row` snapshots for anything the diff skips (`id`,
`created_at`, `updated_at`).

WHAT THIS IS NOT. It is the TECHNICAL history — who corrected what, and when. The BUSINESS history
is the sequence of rows themselves: a policy renewal is a new policy and a new coverage row, a
change of lease terms is a new lease row. "What covered this aircraft in 2025" is
`GET /fleet/aircraft/{id}?on_date=2025-06-30`, not a query here.

`GET /history/aircraft/{id}` is the one that answers a real question: it gathers the airframe's own
changes AND those of its engines, lease records and coverage rows into one timeline, because that
is what somebody means by "what happened to this aircraft".

The log is append-only from the API's side: `svc_api` holds SELECT on `audit` and nothing else, so
there is deliberately no endpoint here that writes or deletes.
"""
from datetime import datetime
from typing import Optional

from fastapi import Request, Response, Depends, Query, status
from sqlalchemy import select, func, or_, and_

from Config import setup_logger
from settings import Router
from Database.AuditModels import ChangeLog
from Database.FleetModels import Aircraft, AircraftEngine, ServiceInfo
from Database.LeasingModels import AircraftLease
from Database.PolicyModels import Coverage
from api_auth import authorize, SCOPE_INSURANCE_READ
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from Utils.DomainCommon import DB, resolve_fk_labels, audit_entry

logger = setup_logger("history_api")

router = Router(prefix="/history", tags=["History"])

_READ = [Depends(authorize(SCOPE_INSURANCE_READ))]
_OK = {status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND,
       status.HTTP_500_INTERNAL_SERVER_ERROR}

# The tables a caller may ask about, so a typo returns a 400 listing the real names rather than an
# empty page that looks like "nothing ever happened". KEEP THIS IN STEP WITH audit.AUDITED — a table
# missing here is invisible through this endpoint even though the trigger is faithfully logging it.
_TABLES = {
    "ref": {"airline", "party", "party_contact"},
    "fleet": {"aircraft_type", "engine_type", "aircraft", "aircraft_engine", "service_info"},
    "leasing": {"agreement", "aircraft_lease"},
    "policy": {"policy", "coverage"},
}
_OPERATIONS = {"INSERT", "UPDATE", "DELETE"}


async def _render(session, rows) -> list[dict]:
    """Resolve every foreign key mentioned in the batch in one pass, then render each entry."""
    labels = await resolve_fk_labels(
        session, [r.old_row for r in rows] + [r.new_row for r in rows])
    return [audit_entry(r, labels) for r in rows]


@router.get(
    path="/",
    description=(
        "The change log, newest first. Filter by `schema` / `table` / `row_id` for one object's "
        "history, by `changed_by` for one actor, by `operation`, and by `since` / `until`. "
        "Each entry carries `changes` (field-level, foreign keys resolved to names) plus the raw "
        "snapshots. Returns `{items, total}`."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def list_history(
    request: Request, response: Response,
    schema: Optional[str] = Query(None, description="ref | fleet | leasing | policy"),
    table: Optional[str] = Query(None, description="e.g. aircraft, aircraft_lease, policy"),
    row_id: Optional[int] = Query(None, description="The id of the row within that table."),
    changed_by: Optional[str] = Query(None, description="Actor, as the API recorded it."),
    operation: Optional[str] = Query(None, description="INSERT | UPDATE | DELETE"),
    since: Optional[datetime] = Query(None), until: Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
):
    try:
        conds = []
        if schema:
            key = schema.strip().lower()
            if key not in _TABLES:
                return warning_response(request=request, response=response,
                                        msg=f"Unknown schema '{schema}'. Allowed: {', '.join(sorted(_TABLES))}")
            conds.append(ChangeLog.schema_name == key)
        if table:
            name = table.strip().lower()
            known = _TABLES.get(schema.strip().lower(), set()) if schema else set().union(*_TABLES.values())
            if name not in known:
                return warning_response(request=request, response=response,
                                        msg=f"Unknown table '{table}'. Allowed: {', '.join(sorted(known))}")
            conds.append(ChangeLog.table_name == name)
        if row_id is not None:
            conds.append(ChangeLog.row_id == row_id)
        if changed_by:
            conds.append(ChangeLog.changed_by == changed_by.strip())
        if operation:
            op = operation.strip().upper()
            if op not in _OPERATIONS:
                return warning_response(request=request, response=response,
                                        msg=f"Unknown operation '{operation}'. Allowed: {', '.join(sorted(_OPERATIONS))}")
            conds.append(ChangeLog.operation == op)
        if since is not None:
            conds.append(ChangeLog.changed_at >= since)
        if until is not None:
            conds.append(ChangeLog.changed_at <= until)

        async with request.app.state.db_client.read_session(DB) as session:
            total = (await session.execute(
                select(func.count()).select_from(ChangeLog).where(*conds))).scalar_one()
            rows = (await session.execute(
                select(ChangeLog).where(*conds)
                .order_by(ChangeLog.changed_at.desc(), ChangeLog.id.desc())
                .limit(limit).offset(offset)
            )).scalars().all()
            items = await _render(session, rows)
        return success_response(request=request, response=response,
                                data={"items": items, "total": total})
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(
    path="/aircraft/{aircraft_id}",
    description=(
        "Everything that ever happened to one airframe, in one timeline: its own changes and those "
        "of its engines, its service block, lease records and coverage rows. `include` narrows it "
        "to some of those (comma-separated: aircraft, engines, service, leases, coverage). The ids "
        "of the related rows are "
        "resolved as they are TODAY, so a lease record deleted long ago still appears through the "
        "entry that deleted it."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def aircraft_history(
    request: Request, response: Response, aircraft_id: int,
    include: str = Query("aircraft,engines,service,leases,coverage"),
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
):
    try:
        wanted = {p.strip().lower() for p in include.split(",") if p.strip()}
        unknown = wanted - {"aircraft", "engines", "service", "leases", "coverage"}
        if unknown:
            return warning_response(
                request=request, response=response,
                msg=f"Unknown include {sorted(unknown)}. "
                    "Allowed: aircraft, engines, service, leases, coverage")

        async with request.app.state.db_client.read_session(DB) as session:
            if await session.get(Aircraft, aircraft_id) is None:
                return warning_response(request=request, response=response,
                                        msg=f"Aircraft {aircraft_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            branches = []
            if "aircraft" in wanted:
                branches.append(and_(ChangeLog.schema_name == "fleet",
                                     ChangeLog.table_name == "aircraft",
                                     ChangeLog.row_id == aircraft_id))
            # For the child tables the log stores the CHILD's id, so the ids are looked up first.
            # A row already deleted is still reachable: its delete entry named it while it existed,
            # and `row_id` carries no foreign key precisely so the entry outlives it — but a child
            # deleted before this call can no longer be found by id, so its entries are also matched
            # through the aircraft_id inside the snapshot itself.
            for key, schema, table, model, column in (
                ("engines", "fleet", "aircraft_engine", AircraftEngine, AircraftEngine.aircraft_id),
                ("service", "fleet", "service_info", ServiceInfo, ServiceInfo.aircraft_id),
                ("leases", "leasing", "aircraft_lease", AircraftLease, AircraftLease.aircraft_id),
                ("coverage", "policy", "coverage", Coverage, Coverage.aircraft_id),
            ):
                if key not in wanted:
                    continue
                ids = (await session.execute(
                    select(model.id).where(column == aircraft_id))).scalars().all()
                by_snapshot = or_(
                    ChangeLog.new_row["aircraft_id"].astext == str(aircraft_id),
                    ChangeLog.old_row["aircraft_id"].astext == str(aircraft_id),
                )
                match = by_snapshot if not ids else or_(ChangeLog.row_id.in_(ids), by_snapshot)
                branches.append(and_(ChangeLog.schema_name == schema,
                                     ChangeLog.table_name == table, match))

            if not branches:
                return success_response(request=request, response=response,
                                        data={"items": [], "total": 0})
            where = or_(*branches)
            total = (await session.execute(
                select(func.count()).select_from(ChangeLog).where(where))).scalar_one()
            rows = (await session.execute(
                select(ChangeLog).where(where)
                .order_by(ChangeLog.changed_at.desc(), ChangeLog.id.desc())
                .limit(limit).offset(offset)
            )).scalars().all()
            items = await _render(session, rows)
        return success_response(request=request, response=response,
                                data={"items": items, "total": total})
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)

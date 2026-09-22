"""Shared reference data: `/ref/airlines` and `/ref/parties` (with their contact blocks).

Both tables are hand-kept and small — this is the reference the rest of the insured-fleet API
resolves names against, not a directory. Two things worth knowing before writing to either:

  * **`ref.airline.is_asg` decides which tails FlightRadar is polled for.** TRUE feeds
    `cirium.asg_*`, FALSE feeds `cirium.non_asg_insured_*`. Flipping it changes nothing downstream
    until those matviews are refreshed, so PATCHing it says so in the response.
  * **Airline names are matched against Cirium by SUBSTRING, longest first.** Cirium writes
    "Air Arabia Abu Dhabi" where this table holds "Air Arabia". Keep the names here short and
    generic; lengthening one to make a single row match will silently drop the others.

A party is one row whatever role it plays — lessor on one contract, insured on another. The role is
decided by the column that references it, so `?role=` on the list endpoint is a filter over actual
usage (an EXISTS over the referencing tables), not a flag stored here.
"""
from typing import Optional

from fastapi import Request, Response, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, func, or_, exists
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from Config import setup_logger
from settings import Router
from Database import ApiToken
from Database.RefModels import Airline, Party, PartyContact
from Database.FleetModels import Aircraft
from Database.LeasingModels import Agreement
from Database.PolicyModels import Policy
from api_auth import authorize, SCOPE_INSURANCE_READ, SCOPE_INSURANCE_WRITE
from Utils import success_response, warning_response, error_response
from Utils.ResponsesFunc import build_responses
from Utils.DomainCache import AIRLINE, PARTY, cached, invalidate
from Utils.DomainCommon import (
    DB, set_actor, apply_sort, typeahead_order, SortError, integrity_error,
    airline_json, party_json, contact_json,
)

logger = setup_logger("ref_api")

router = Router(prefix="/ref", tags=["Reference"])

_AIRLINE_SORTS = {
    "airline_name": Airline.airline_name,
    "icao": Airline.icao,
    "iata": Airline.iata,
    "is_asg": Airline.is_asg,
    "created_at": Airline.created_at,
    "updated_at": Airline.updated_at,
}
_PARTY_SORTS = {
    "name": Party.name,
    "created_at": Party.created_at,
    "updated_at": Party.updated_at,
}

# `?role=` maps to "is this party actually referenced as that role", so the filter can never
# disagree with reality the way a stored flag eventually does.
_PARTY_ROLE_LINKS = {
    "lessor": lambda: exists().where(Agreement.lessor_id == Party.id),
    "insured": lambda: exists().where(Policy.insured_id == Party.id),
    "reinsured": lambda: exists().where(Policy.reinsured_id == Party.id),
    "retrocedent": lambda: exists().where(Policy.retrocedent_id == Party.id),
}

_READ = [Depends(authorize(SCOPE_INSURANCE_READ))]
_OK = {status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST, status.HTTP_404_NOT_FOUND,
       status.HTTP_409_CONFLICT, status.HTTP_500_INTERNAL_SERVER_ERROR}


# ==============================================================================================
# bodies
# ==============================================================================================

class AirlineIn(BaseModel):
    airline_name: str = Field(min_length=1, max_length=256)
    icao: Optional[str] = Field(default=None, max_length=8)
    iata: Optional[str] = Field(default=None, max_length=8)
    is_asg: bool = True
    logo_url: Optional[str] = Field(default=None, max_length=2048)


class AirlinePatch(BaseModel):
    airline_name: Optional[str] = Field(default=None, min_length=1, max_length=256)
    icao: Optional[str] = Field(default=None, max_length=8)
    iata: Optional[str] = Field(default=None, max_length=8)
    is_asg: Optional[bool] = None
    logo_url: Optional[str] = Field(default=None, max_length=2048)


class ContactIn(BaseModel):
    """One COMPANY / CONTACTS / EMAIL block as the source documents write them. Every field is
    optional because the blocks arrive half-filled; `company` is the entity named ON the block,
    which for a group is a subsidiary and not the party's own name."""
    company: Optional[str] = Field(default=None, max_length=256)
    contact: Optional[str] = Field(default=None, max_length=256)
    email: Optional[str] = Field(default=None, max_length=256)
    phone: Optional[str] = Field(default=None, max_length=64)
    note: Optional[str] = None


class PartyIn(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    details: Optional[str] = None
    contacts: list[ContactIn] = Field(default_factory=list)


class PartyPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=256)
    details: Optional[str] = None


# ==============================================================================================
# airlines
# ==============================================================================================

@router.get(
    path="/airlines",
    description=(
        "Airlines this business insures or tracks. `q` matches the name (substring) or an "
        "ICAO/IATA prefix; `is_asg` filters the fleet split. Returns `{items, total}` — `total` is "
        "the whole filtered set, not the page. Sort by "
        "airline_name / icao / iata / is_asg / created_at / updated_at."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def list_airlines(
    request: Request, response: Response,
    q: str = Query("", description="Name substring, or ICAO/IATA prefix."),
    is_asg: Optional[bool] = Query(None, description="true = ASG fleet, false = insured but not ASG."),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    sort: Optional[str] = Query(None), order: Optional[str] = Query(None),
):
    try:
        conds = []
        q = q.strip()
        if q:
            conds.append(or_(Airline.airline_name.ilike(f"%{q}%"),
                             Airline.icao.ilike(f"{q}%"), Airline.iata.ilike(f"{q}%")))
        if is_asg is not None:
            conds.append(Airline.is_asg.is_(is_asg))

        stmt = select(Airline).where(*conds)
        if q and not sort:
            stmt = stmt.order_by(*typeahead_order(Airline.airline_name, q))
        else:
            stmt = apply_sort(stmt, sort=sort, order=order, sortmap=_AIRLINE_SORTS,
                              tiebreak=(Airline.airline_name, Airline.id))
        async def load():
            async with request.app.state.db_client.read_session(DB) as session:
                total = (await session.execute(
                    select(func.count()).select_from(Airline).where(*conds))).scalar_one()
                rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
                return {"items": [airline_json(a) for a in rows], "total": total}

        data = await cached(request, AIRLINE,
                            {"q": q, "is_asg": is_asg, "limit": limit, "offset": offset,
                             "sort": sort, "order": order}, load)
        return success_response(request=request, response=response, data=data)
    except SortError as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/airlines",
    description=(
        "Add an airline. Names are matched against Cirium by substring, so keep them short: "
        "'Air Arabia', not 'Air Arabia Abu Dhabi'. A new airline is picked up by the fleet "
        "matviews only after they are refreshed."
    ),
    responses=build_responses(include=_OK | {status.HTTP_201_CREATED}),
)
async def create_airline(request: Request, response: Response, body: AirlineIn,
                         token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            clash = (await session.execute(
                select(Airline).where(Airline.airline_name.ilike(body.airline_name.strip()))
            )).scalar_one_or_none()
            if clash is not None:
                return warning_response(request=request, response=response,
                                        msg=f"Airline '{clash.airline_name}' already exists (id {clash.id}).",
                                        status_code=status.HTTP_409_CONFLICT)
            row = Airline(airline_name=body.airline_name.strip(), icao=body.icao, iata=body.iata,
                          is_asg=body.is_asg, logo_url=body.logo_url)
            session.add(row)
            await session.flush()
            data = airline_json(row)
        await invalidate(request, AIRLINE)
        return success_response(request=request, response=response, data=data,
                                msg="Airline created. Refresh the fleet matviews for it to be "
                                    "tracked.", status_code=status.HTTP_201_CREATED)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(path="/airlines/{airline_id}", description="One airline.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def get_airline(request: Request, response: Response, airline_id: int):
    try:
        async with request.app.state.db_client.read_session(DB) as session:
            row = await session.get(Airline, airline_id)
            data = airline_json(row)
        if data is None:
            return warning_response(request=request, response=response,
                                    msg=f"Airline {airline_id} not found",
                                    status_code=status.HTTP_404_NOT_FOUND)
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(
    path="/airlines/{airline_id}",
    description=(
        "Change an airline. Only the fields present in the body are touched; the change is "
        "recorded in the audit log. Changing `is_asg` moves the carrier's aircraft between the "
        "ASG and insured-but-not-ASG matviews — refresh them for it to take effect."
    ),
    responses=build_responses(include=_OK),
)
async def update_airline(request: Request, response: Response, airline_id: int, body: AirlinePatch,
                         token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        fields = body.model_dump(exclude_unset=True)
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = await session.get(Airline, airline_id)
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Airline {airline_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            moved = "is_asg" in fields and fields["is_asg"] != row.is_asg
            for key, value in fields.items():
                setattr(row, key, value.strip() if key == "airline_name" and value else value)
            await session.flush()
            data = airline_json(row)
        await invalidate(request, AIRLINE)
        msg = ("Airline updated. `is_asg` changed — refresh the fleet matviews for the polling "
               "list to follow." if moved else "Airline updated")
        return success_response(request=request, response=response, data=data, msg=msg)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(
    path="/airlines/{airline_id}",
    description=(
        "Remove an airline. Refused with 409 while any aircraft still points at it — reassign "
        "those first. The deletion and the row it removed are kept in the audit log."
    ),
    responses=build_responses(include=_OK),
)
async def delete_airline(request: Request, response: Response, airline_id: int,
                         token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = await session.get(Airline, airline_id)
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Airline {airline_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            used = (await session.execute(
                select(func.count()).select_from(Aircraft)
                .where(Aircraft.airline_id == airline_id))).scalar_one()
            if used:
                return warning_response(
                    request=request, response=response,
                    msg=f"{used} aircraft still belong to this airline — reassign them first.",
                    status_code=status.HTTP_409_CONFLICT)
            data = airline_json(row)
            await session.delete(row)
        await invalidate(request, AIRLINE)
        return success_response(request=request, response=response, data=data, msg="Airline deleted")
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


# ==============================================================================================
# parties
# ==============================================================================================

@router.get(
    path="/parties",
    description=(
        "Counterparties. `q` matches the name or any contact's company/email. `role` "
        "(lessor / insured / reinsured / retrocedent) keeps only parties actually USED in that "
        "role — it is an existence check over the referencing tables, not a stored flag. "
        "Returns `{items, total}`."
    ),
    responses=build_responses(include=_OK), dependencies=_READ,
)
async def list_parties(
    request: Request, response: Response,
    q: str = Query("", description="Name substring, or a contact's company/email."),
    role: Optional[str] = Query(None, description="lessor | insured | reinsured | retrocedent"),
    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    sort: Optional[str] = Query(None), order: Optional[str] = Query(None),
):
    try:
        conds = []
        q = q.strip()
        if q:
            conds.append(or_(
                Party.name.ilike(f"%{q}%"),
                exists().where((PartyContact.party_id == Party.id) & or_(
                    PartyContact.company.ilike(f"%{q}%"), PartyContact.email.ilike(f"%{q}%"))),
            ))
        if role:
            key = role.strip().lower()
            if key not in _PARTY_ROLE_LINKS:
                return warning_response(
                    request=request, response=response,
                    msg=f"Unknown role '{role}'. Allowed: {', '.join(sorted(_PARTY_ROLE_LINKS))}")
            conds.append(_PARTY_ROLE_LINKS[key]())

        stmt = select(Party).where(*conds).options(selectinload(Party.contacts))
        if q and not sort:
            stmt = stmt.order_by(*typeahead_order(Party.name, q))
        else:
            stmt = apply_sort(stmt, sort=sort, order=order, sortmap=_PARTY_SORTS,
                              tiebreak=(Party.name, Party.id))
        async def load():
            async with request.app.state.db_client.read_session(DB) as session:
                total = (await session.execute(
                    select(func.count()).select_from(Party).where(*conds))).scalar_one()
                rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
                return {"items": [party_json(p) for p in rows], "total": total}

        data = await cached(request, PARTY,
                            {"q": q, "role": role, "limit": limit, "offset": offset,
                             "sort": sort, "order": order}, load)
        return success_response(request=request, response=response, data=data)
    except SortError as _ex:
        return warning_response(request=request, response=response, msg=str(_ex))
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.post(
    path="/parties",
    description=(
        "Add a counterparty, optionally with its contact blocks in the same call. Names are "
        "unique compared trimmed and upper-cased, so 'AerCap' and 'AERCAP ' cannot both exist."
    ),
    responses=build_responses(include=_OK | {status.HTTP_201_CREATED}),
)
async def create_party(request: Request, response: Response, body: PartyIn,
                       token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = Party(name=body.name.strip(), details=body.details)
            row.contacts = [PartyContact(**c.model_dump()) for c in body.contacts]
            session.add(row)
            await session.flush()
            data = party_json(row)
        await invalidate(request, PARTY)
        return success_response(request=request, response=response, data=data,
                                status_code=status.HTTP_201_CREATED)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.get(path="/parties/{party_id}", description="One counterparty with all its contact blocks.",
            responses=build_responses(include=_OK), dependencies=_READ)
async def get_party(request: Request, response: Response, party_id: int):
    try:
        async with request.app.state.db_client.read_session(DB) as session:
            row = (await session.execute(
                select(Party).where(Party.id == party_id).options(selectinload(Party.contacts))
            )).scalar_one_or_none()
            data = party_json(row)
        if data is None:
            return warning_response(request=request, response=response,
                                    msg=f"Party {party_id} not found",
                                    status_code=status.HTTP_404_NOT_FOUND)
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(path="/parties/{party_id}",
              description="Rename a counterparty or change its notes. Contacts are managed "
                          "separately through /ref/parties/{party_id}/contacts.",
              responses=build_responses(include=_OK))
async def update_party(request: Request, response: Response, party_id: int, body: PartyPatch,
                       token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        fields = body.model_dump(exclude_unset=True)
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(
                select(Party).where(Party.id == party_id).options(selectinload(Party.contacts))
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Party {party_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            for key, value in fields.items():
                setattr(row, key, value.strip() if key == "name" and value else value)
            await session.flush()
            data = party_json(row)
        await invalidate(request, PARTY)
        return success_response(request=request, response=response, data=data)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(
    path="/parties/{party_id}",
    description=(
        "Remove a counterparty and its contacts. Refused with 409 while a lease agreement or a "
        "policy still names it."
    ),
    responses=build_responses(include=_OK),
)
async def delete_party(request: Request, response: Response, party_id: int,
                       token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = (await session.execute(
                select(Party).where(Party.id == party_id).options(selectinload(Party.contacts))
            )).scalar_one_or_none()
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Party {party_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            data = party_json(row)
            await session.delete(row)
        await invalidate(request, PARTY)
        return success_response(request=request, response=response, data=data, msg="Party deleted")
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


# ==============================================================================================
# contact blocks
# ==============================================================================================

@router.post(path="/parties/{party_id}/contacts",
             description="Add a contact block to a counterparty.",
             responses=build_responses(include=_OK | {status.HTTP_201_CREATED}))
async def add_contact(request: Request, response: Response, party_id: int, body: ContactIn,
                      token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            party = await session.get(Party, party_id)
            if party is None:
                return warning_response(request=request, response=response,
                                        msg=f"Party {party_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            row = PartyContact(party_id=party_id, **body.model_dump())
            session.add(row)
            await session.flush()
            data = contact_json(row)
        await invalidate(request, PARTY)
        return success_response(request=request, response=response, data=data,
                                status_code=status.HTTP_201_CREATED)
    except IntegrityError as _ex:
        code, msg = integrity_error(_ex)
        return warning_response(request=request, response=response, msg=msg, status_code=code)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.patch(path="/contacts/{contact_id}", description="Change one contact block.",
              responses=build_responses(include=_OK))
async def update_contact(request: Request, response: Response, contact_id: int, body: ContactIn,
                         token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        fields = body.model_dump(exclude_unset=True)
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = await session.get(PartyContact, contact_id)
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Contact {contact_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            for key, value in fields.items():
                setattr(row, key, value)
            await session.flush()
            data = contact_json(row)
        await invalidate(request, PARTY)
        return success_response(request=request, response=response, data=data)
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)


@router.delete(path="/contacts/{contact_id}", description="Remove one contact block.",
               responses=build_responses(include=_OK))
async def delete_contact(request: Request, response: Response, contact_id: int,
                         token: Optional[ApiToken] = Depends(authorize(SCOPE_INSURANCE_WRITE))):
    try:
        async with request.app.state.db_client.session(DB) as session:
            await set_actor(session, token)
            row = await session.get(PartyContact, contact_id)
            if row is None:
                return warning_response(request=request, response=response,
                                        msg=f"Contact {contact_id} not found",
                                        status_code=status.HTTP_404_NOT_FOUND)
            data = contact_json(row)
            await session.delete(row)
        await invalidate(request, PARTY)
        return success_response(request=request, response=response, data=data, msg="Contact deleted")
    except Exception as _ex:
        return error_response(request=request, exc=_ex, response=response)

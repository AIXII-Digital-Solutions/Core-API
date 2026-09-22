"""Redis cache for the insured-fleet reference reads.

WHAT IS CACHED, AND WHY ONLY THAT. The four reference listings — airlines, counterparties, aircraft
types, engine models — are read constantly (every typeahead keystroke), are large (806 aircraft
types, 365 engine models) and change rarely. Everything else in the domain is deliberately NOT
cached: an aircraft, a lease, a policy, the coverage comparison and the change log are all read
immediately after somebody writes them, and a portal user who saves a lease and is then shown the
old one is worse off than one who waits for a query.

INVALIDATION IS EXPLICIT, NOT A TTL. The TTL is a backstop; the correctness comes from a
GENERATION counter per entity. A cached payload's key contains the generation, so bumping it makes
every key for that entity unreachable at once — one INCR, no SCAN. That matters here: this Redis
also carries the FlightRadar polling set and the status channel, so a `SCAN MATCH` on every write
would walk a keyspace that has nothing to do with this domain.

Stale keys are never deleted; they simply expire. Redis reclaims them, nothing reads them.

FAILING OPEN. Every helper swallows Redis errors and falls through to the database. A cache that
can take the endpoint down with it is a worse bug than the latency it saves.

Wiring a NEW cached listing means three things, and the third is the one that bites: read it
through `cached()`, and invalidate its entity from EVERY write that can touch it — including the
find-or-create paths, where creating an aircraft can quietly create an airline and a type.
"""
import hashlib
import json
import logging
from typing import Any, Awaitable, Callable, Optional

from fastapi import Request

from settings import INSURED_FLEET_CACHE_SECONDS

logger = logging.getLogger("domain_cache")

# The entities whose listings are cached. A string not in here is a typo, and a typo that silently
# did nothing would be a cache that never invalidates.
AIRLINE = "airline"
PARTY = "party"
AIRCRAFT_TYPE = "aircraft_type"
ENGINE_TYPE = "engine_type"
ENTITIES = frozenset({AIRLINE, PARTY, AIRCRAFT_TYPE, ENGINE_TYPE})

_PREFIX = "insfleet"

# ONE round trip to read, not two.
#
# The payload's key contains the generation, so a plain client has to GET the generation and then
# GET the payload — and Redis is across the same network as the database, so that is two waits for
# an answer that is supposed to be the fast path. This does both hops inside Redis and returns the
# generation alongside whatever it found, so a cache hit costs one round trip and a miss costs one
# too (the generation comes back either way, and the write that follows needs it).
#
# Deliberately not `redis.call('GET', ...)` on a missing generation key: Lua turns a nil reply into
# `false`, so the default is spelled out rather than relied upon.
_READ_LUA = """
local generation = redis.call('GET', KEYS[1])
if not generation then generation = '0' end
local payload = redis.call('GET', ARGV[1] .. ':' .. generation .. ':' .. ARGV[2])
if not payload then payload = '' end
return {generation, payload}
"""
_read_script = None


def _script(redis):
    """Register the reader once per process. `register_script` sends the body only when Redis has
    not seen its hash, so the usual case is EVALSHA with the digest and nothing else."""
    global _read_script
    if _read_script is None:
        _read_script = redis.register_script(_READ_LUA)
    return _read_script


def _gen_key(entity: str) -> str:
    return f"{_PREFIX}:gen:{entity}"


def _digest(signature: dict) -> str:
    # The signature is every parameter that changes the answer — filters, paging and sort. Hashed
    # rather than spelled out so a long `q` cannot produce an unbounded key, and sorted so two
    # equivalent requests share one entry.
    blob = json.dumps(signature, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _payload_key(entity: str, generation: str, signature: dict) -> str:
    return f"{_PREFIX}:{entity}:{generation}:{_digest(signature)}"


async def _generation(redis, entity: str) -> str:
    """The entity's current generation. Absent means nothing has been written since Redis last
    started — generation 0 is as good a starting point as any."""
    value = await redis.get(_gen_key(entity))
    return value if value is not None else "0"


async def cached(request: Request, entity: str, signature: dict,
                 loader: Callable[[], Awaitable[Any]],
                 ttl: Optional[int] = None) -> Any:
    """Return the cached answer for this request shape, or load it and cache it.

    `loader` must produce something JSON-serialisable — the grid payload, not an ORM row.
    """
    if entity not in ENTITIES:
        raise ValueError(f"unknown cache entity {entity!r}; add it to ENTITIES")
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return await loader()

    key = None
    try:
        generation, payload = await _script(redis)(
            keys=[_gen_key(entity)], args=[f"{_PREFIX}:{entity}", _digest(signature)])
        if payload:
            return json.loads(payload)
        # The generation that MISSED is the one to write under. Re-reading it here would race
        # with a write that lands in between and leave the new answer filed under the old number,
        # where the next reader would not look for it.
        key = _payload_key(entity, generation, signature)
    except Exception:
        logger.debug("cache read failed for %s — serving from the database", entity, exc_info=True)
        return await loader()

    data = await loader()
    try:
        await redis.setex(key, ttl or INSURED_FLEET_CACHE_SECONDS, json.dumps(data, default=str))
    except Exception:
        logger.debug("cache write failed for %s — the answer still stands", entity, exc_info=True)
    return data


async def invalidate(request: Request, *entities: str) -> None:
    """Make every cached listing of these entities unreachable, at once.

    Call it from EVERY write that can change what a listing returns — which includes the
    find-or-create paths: adding an aircraft can create an airline, an aircraft type and an engine
    model as a side effect, so that one write invalidates all three.

    Never raises: a cache that refuses to be invalidated must not also refuse the write that
    succeeded. The TTL bounds how long a miss here can be visible.
    """
    for entity in entities:
        if entity not in ENTITIES:
            raise ValueError(f"unknown cache entity {entity!r}; add it to ENTITIES")
    redis = getattr(request.app.state, "redis", None)
    if redis is None or not entities:
        return
    try:
        # One round trip however many entities. Creating an aircraft can create an airline, an
        # aircraft type and an engine model, so three is the ordinary case, not the extreme one.
        pipe = redis.pipeline(transaction=False)
        for entity in entities:
            pipe.incr(_gen_key(entity))
        await pipe.execute()
    except Exception:
        logger.warning("could not invalidate the %s cache; entries stand until their TTL (%ss)",
                       ", ".join(entities), INSURED_FLEET_CACHE_SECONDS, exc_info=True)

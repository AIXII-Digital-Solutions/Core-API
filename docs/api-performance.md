# API performance — September 2026

What was changed to make core-api answer faster (PR #87), what it measured, how to deploy it and what
is left. The short version: a read request used to make **six** round trips to the database and an
`X-Api-Key` request about **thirteen**; both now make **one**. Most of the latency a client sees on production,
though, is not in this process at all — see [Where the time goes](#where-the-time-goes).

> The `/insurance*` rows below are historical: those endpoints were removed with the `insurance`
> schema in revision `insured_fleet_rebuild` (see `insured-fleet.md`). The measurements still
> stand for the DB round-trip work they demonstrate.

## Where the time goes

Measured from a client with `bench_api.py` (median of 8 after a warm-up, keep-alive connection):

| Segment (production, before)                         | Time        | How it is known                                   |
|------------------------------------------------------|-------------|---------------------------------------------------|
| Client ↔ openresty on the data host (1 RTT)          | ~155 ms     | TCP connect time                                  |
| openresty → api-master → back, empty `/health`       | ~370 ms     | `/health` total 524 ms minus the client RTT       |
| One DB round trip from api-master                    | ~24 ms      | (665 − 524) / 6 statements on a typical GET       |
| Application code itself                              | 1–10 ms     | same code locally: `/health` 9 ms, no DB          |

`/health` does no I/O, so its 524 ms is a floor under every endpoint, and ~370 ms of it is the hop
between the proxy and api-master. That is far more than a 24 ms network RTT explains; likely causes
are listed under [Proxy](#proxy-not-verified--no-access).

## September 23: the loaders, the counts, and what a request costs

The work above cut the round trips a request makes; this round cut the ones it made without
anybody asking. Measured on the wire with a `before_cursor_execute` hook, median of three.

| Endpoint | Before | After |
|---|---:|---:|
| `GET /history/aircraft/{id}` | 16 | **4** |
| `GET /fleet/aircraft/{id}/service` | 7 | **1** |
| `GET /history` | 5 | **2** |
| `GET /fleet/aircraft` (any page) | 3 | **2** |
| `GET /fleet/aircraft/{id}/engines` | 2 | **1** |
| every other grid | 2 | **1** when it fits on one page |
| `GET /fleet/aircraft` (cached) | 3 | **0** |
| `GET /policy/coverage/compare` (cached) | 3 | **0** |
| `POST /fleet/aircraft` (2 engines, new type) | 13 | **10** |

Nothing the API serves reads more than four.

### Relationships stopped loading themselves

The domain models were `lazy="selectin"`: one extra SELECT per relationship, on every load, whether
or not anything read it. So reading ONE aircraft to check that it EXISTS fetched its type, its
airline, its engines and their models — and two endpoints were built on exactly that check.

They are `lazy="raise_on_sql"` now. Nothing loads implicitly; touching an unloaded relationship
raises and names it, so the cost has to be written at the call site where review can see it. The
rule is short enough to remember:

| what it is | loader | why |
|---|---|---|
| to-one | `joinedload` | one row, belongs in the parent's own SELECT |
| collection | `selectinload` | joining it multiplies parent rows and breaks LIMIT/OFFSET |
| not needed | nothing, or `raiseload` | costs nothing, and asking later fails loudly |

The aircraft shapes live once in `Utils/DomainCommon` — `AIRCRAFT_BRIEF` (inside a lease, a
coverage, a comparison: no engines), `AIRCRAFT_GRID` (a page), `AIRCRAFT_ONE` (a single card, so
the engines join too). Leasing and Policies render aircraft as well, and three private copies of
the rule would be three chances to get it wrong.

Turning the default up found BUGS, not just slow paths: `list_engines` rendered its rows after the
session closed and only worked because selectin had already fetched everything; the engine PATCH
and DELETE never asked for the model they print; every aircraft rendered inside a lease or a
coverage was loading its references one query at a time.

### Three helpers that replaced three habits

- **`reload_with(session, Model, id, *options)`** replaces `session.refresh(row, ["a","b","c"])`,
  which costs a round trip PER ATTRIBUTE — four to hand back a newly created aircraft.
  `populate_existing` is what makes it correct: without it a relationship already loaded is left
  as it was, because changing a foreign key does not expire the object hanging off it, and a PATCH
  that moved an engine to another model handed back the OLD one.
- **`page_with_total(...)`** returns `(rows, total)` from one statement with `count(*) OVER ()`.
  The one case a window cannot answer is an empty page — over no rows it returns no rows, so
  "nothing matched" and "page 9 of 3" look identical — so an empty result at a NON-ZERO offset,
  and only that, falls back to the COUNT. Safe only because the grids no longer joined-load a
  collection, which would have made the window count duplicates.
- **`resolve_fk_labels`** builds its labels in SQL and asks for all seven kinds in one UNION ALL.
  A page of the change log names parties, airlines, aircraft, types, engine models, agreements and
  policies at once, and seven serial round trips is seven times the network for one pass of work.

### Existence checks, and one UNION ALL

`session.get(Aircraft, id)` to answer "does this exist" loaded the whole graph; it selects the id
now. `/history/aircraft/{id}` did that AND one SELECT per child table for their ids — five round
trips before it read a single log entry. One UNION ALL asks all five questions at once. A deleted
aircraft's history is reachable as a result, which matches what the endpoint already promised for
deleted children.

### Writes

`POST /fleet/aircraft` made thirteen round trips, three of them avoidable and none obvious:
`find_aircraft` asked twice (MSN, then registration — now one query ordered so MSN still wins);
`_get_or_create_type` was called once per ENGINE, so a twin looked its model up twice (memoised on
the session); and the engines went in one INSERT at a time, because the SELECT inside
`_engine_fields` triggered an autoflush of the row added just before it. Resolving every engine
before adding any lets SQLAlchemy batch them.

### Every request says what it cost

```
GET /fleet/aircraft?limit=50 completed_in=0.412s | status_code=200 | db=2/71.4ms | correlation_id=...
```

A ContextVar holds one counter per request and the SQLAlchemy listeners are on the Engine CLASS, so
they cover every engine the process opens. Two `perf_counter()` calls and an integer add per
statement. The level follows what is wrong rather than what happened — 5xx errors, slow requests
and 4xx warn, and so does a request that crosses `BUSY_REQUEST_QUERIES` (default 8), which is the
one that matters: too many round trips is merely slow here and much worse over a longer wire. A
warning also names that request's slowest statement.

### Two heavy reads became free

`GET /fleet/aircraft` and `GET /policy/coverage/compare` now read through a `fleet` generation
bumped **by the middleware** after any 2xx to a non-GET under `/fleet`, `/ref`, `/leasing` or
`/policy`. Precise invalidation was the wrong tool: an aircraft row embeds its type, its airline
and its service block, and the comparison reads every lease and coverage, so the correct set to
invalidate differs per handler and the thirty-sixth write handler would get it wrong silently.
One choke point cannot be forgotten. A write made OUTSIDE the API — an `_admin/` loader — is not
seen, and is bounded by `INSURED_FLEET_CACHE_SECONDS`.

15 checks walk every kind of write in the domain and demand that both reads have already moved.

### Indexes

`audit.change_log` is the only table in this domain that grows without bound.
`/history/aircraft/{id}` filters on `new_row ->> 'aircraft_id'` — the log stores the CHILD's id, so
the aircraft is only named inside the snapshot — and nothing indexed it: 313 rows read to return 3,
a ratio that does not improve with age. Two PARTIAL expression indexes take it to 3 and 3. The
listing's sort index now matches `(changed_at DESC, id DESC)` instead of forcing an Incremental
Sort. Four bare foreign-key indexes were dropped, each the leading column of a composite that
already existed, and all thirteen domain tables were ANALYZEd — two had never been.

### The cache

A hit cost two Redis round trips (GET the generation, GET the payload keyed by it), and Redis is
across the same network as the database. A Lua reader does both hops inside Redis and returns the
generation alongside whatever it found: one round trip for a hit, one for a miss. Invalidation is
pipelined — creating an aircraft can create an airline, a type and an engine model, and three
INCRs were three waits.

## Changes

### Database round trips
- **`read_session()`** (`app/Database/Client.py`) — an AUTOCOMMIT session: no `BEGIN`/`COMMIT`. Under
  READ COMMITTED every statement takes its own snapshot anyway, so readers observe nothing different.
  All 24 `GET` handlers use it, plus two read-only lookups inside `POST /forecast`. Writes and anything
  using `SET LOCAL` / `set_config(..., true)` keep `session()`.
- **`pool_pre_ping` removed.** With asyncpg it ran `BEGIN; ; ROLLBACK;` on *every* checkout — three
  round trips before the first real query. Replaced by a checkout hook that sends one bare `SELECT 1`
  only when the connection sat idle longer than `DB_PING_IDLE_SECONDS`; a failed ping raises
  `DisconnectionError` so the pool reconnects transparently.
- Verified on the wire: a read now sends exactly its own statement; a transactional session sends
  `BEGIN … COMMIT`; an idle connection gets one `SELECT 1`.
- **API keys** (`app/api_auth.py`) — a validated key is cached per process for
  `API_TOKEN_CACHE_SECONDS` (10 s). Only successes are cached; a wrong key always hits the DB.
  `PATCH`/`DELETE /tokens/{prefix}` drop the entry in the serving worker immediately, other workers
  within the TTL. The throttled `last_used_at` write is one autocommitted `UPDATE`.
- **`/queues`** reads all depths and paused flags in one Redis pipeline (was 2 calls per queue).

### Insured-fleet round trips (September 2026)

The portal reported ~1.2 s for an aircraft card that returns almost nothing, and it was right: the
card made **eight** round trips and the by-registration form of it **fourteen**. Not one of them was
slow — they were simply serial, against a database ~24 ms away, which is the shape `lazy="selectin"`
gives you. Selectin issues one extra `SELECT` per relationship; that is the right trade for a
*collection* (it keeps `LIMIT`/`OFFSET` intact and does not multiply parent rows) and pure waste for
a **to-one**, whose single row belongs in the parent's own `SELECT`.

So every to-one relationship in the domain routers became a `joinedload`. Counted on the wire with
a `before_cursor_execute` hook, median of five after a warm-up:

| Endpoint                                     | Before | After | What is left                                             |
|----------------------------------------------|--------|-------|----------------------------------------------------------|
| `GET /fleet/aircraft/by-registration/{reg}`  | **14** | **3** | airframe+type+airline+service+engines, leases, coverages |
| `GET /fleet/aircraft/{id}` (card)            | 8      | **3** | same                                                     |
| `GET /policy/coverage/compare` (whole fleet) | 8      | **3** | fleet+type+airline+service, leases, coverages            |
| `GET /fleet/aircraft` (grid, any page)       | 7      | **3** | count, page+type+airline+service, engines                |

Three of those fourteen were a `session.refresh(row, [...])` in the by-registration handler, put
there because `find_aircraft()` loaded no relationships and the card needed them. It now takes
`options`, so the refresh — a second full fetch of a row already in the session — is gone.

Two rules came out of it, and both are load-bearing:

- **To-one → `joinedload`; a collection → `selectinload`.** Joining a collection multiplies the
  parent rows, which silently breaks `LIMIT`/`OFFSET` on a grid.
- **One row is not a page.** With no `LIMIT` there is nothing for a joined collection to break, so
  the single-aircraft paths use `_AIRCRAFT_ONE`, which joins the engines too. A joined collection
  makes the result rows non-unique, hence `.unique()` before `scalar_one_or_none()` — leave it out
  and SQLAlchemy raises rather than lying, so the mistake cannot reach production quietly.

The chains under a lease and a policy were joined for the same reason: agreement → lessor, policy →
insured/reinsured/retrocedent are to-one all the way down and would have cost four more trips each
*once contracts exist*. The card costs three trips whether the aircraft is leased and insured or
neither — the table above will not decay as the portal fills the tables.

`/policy/coverage/compare` had a subtler version of the same bug: it renders every aircraft with
`engines=False`, but `lazy="selectin"` does not care what the code reads — it fetched the engines
and their models for all 149 airframes regardless. `raiseload(Aircraft.engines)` refuses instead,
which is two round trips cheaper and turns a future `engines=True` into a loud error rather than a
silent pair of extra queries.

What is NOT fixed: `history=false` still costs the same three trips. It trims the payload, not the
work — the in-force lease and coverage have to be read either way.

### Per-request overhead
- The two `@app.middleware("http")` functions (each a `BaseHTTPMiddleware` with its own streams and
  task group) became one pure ASGI class, `RequestContextMiddleware`. Same `request.state` keys, same
  `X-Correlation-ID`, same log line, and a new **`Server-Timing: app;dur=<ms>`** header.
- `DBProxy` parsed `.env` (`DBSettings()`) on every request; now cached once.
- The response helpers serialize the envelope with **orjson** and return a ready `Response`. Routes
  that declare a `response_model` keep the FastAPI path (their model may filter fields), and so does
  any value orjson cannot encode the way `jsonable_encoder` would.
- **GZip** for responses ≥ 1 KB (level 5). SSE (`text/event-stream`) is not compressed or buffered.
- **Logging** goes through a `QueueHandler`/`QueueListener`: file and console I/O moved off the event loop.

### Server
- `app/main.py` runs `API_WORKERS` uvicorn processes (default 4), `loop/http=auto` (uvloop + httptools
  on Linux — added to `requirements.txt` for non-Windows), `timeout_keep_alive=API_KEEPALIVE_TIMEOUT`
  (65 s; uvicorn's default of 5 s is shorter than a proxy's upstream keep-alive, which makes the proxy
  hit closed sockets and reconnect), uvicorn access log off (the middleware logs every request).
- With several workers the log **file** is written by the main process only; workers log to stdout
  (`docker logs`).

## Measurements

Local A/B against the production database and Redis: old `HEAD` and the new tree side by side, same
machine. The local machine is ~300 ms from the database, so every round trip shows clearly.

| Endpoint                           | Old     | New     | Wire, old → new   |
|------------------------------------|---------|---------|-------------------|
| typical GET (profiles, snapshots, refs, scheduler) | 1800 ms | 307 ms | —      |
| `/insurance`, `/insurance/claims`, `/forecast/claims` (2 queries) | 2115 ms | 609 ms | — |
| `/status`                          | 1817 ms | 330 ms  | 30.4 → 4.8 KB     |
| `/scheduler`                       | 1799 ms | 310 ms  | 3.8 → 1.0 KB      |
| GET with `X-Api-Key`               | ~3950 ms| 290 ms  | —                 |
| `/queues`                          | 1250 ms | 311 ms  | —                 |
| `/insurance`, 10 concurrent        | 3.4 req/s | 6.3 req/s | —             |

Production baseline before the deploy (from a client): `/health` 524 ms, typical DB GET 665–700 ms,
`/status` 1087 ms, 33 req/s on `/health` at concurrency 20.

**Expected on production** (the ~524 ms floor stays): typical DB GET ~550 ms, `/status` ~560 ms,
API-key requests ~550 ms instead of ~760 ms. The floor itself only moves by fixing the proxy hop or
moving the API next to the database.

## Deploy

1. Rebuild and restart the `core-api` image (new dependencies: `uvloop`, `httptools`).
2. Optional environment (defaults in parentheses): `API_WORKERS` (4), `API_KEEPALIVE_TIMEOUT` (65),
   `API_TOKEN_CACHE_SECONDS` (10), `DB_POOL_SIZE` (3), `DB_MAX_OVERFLOW` (5),
   `DB_POOL_RECYCLE_SECONDS` (1800), `DB_PING_IDLE_SECONDS` (30).
3. Each worker is a full copy of the app; on the 6 GB / 6-core host keep `API_WORKERS` at 4 and check
   `docker stats core-api` after the start.
4. Connection budget: peak = workers × 2 databases × (pool + overflow) = 4 × 2 × 8 = **64**.
   The cluster has `max_connections = 100` with ~42 in use by everyone. Raise it at the next Postgres
   restart (it is already due for `shared_buffers`):
   ```sql
   ALTER SYSTEM SET max_connections = 200;   -- applies on restart
   ```
5. Check: `curl -sI https://api.aixii.com/health/ | grep -i server-timing` — `app;dur=` should be a
   few ms. The gap between it and the client's total is proxy + network.

## Proxy (not verified — no access)

The ~370 ms between openresty and api-master is the biggest single cost. In order of likelihood:
- **No upstream keep-alive** — a new TCP (and TLS, if the upstream is `https`) connection per request.
  Use an `upstream` block with `keepalive 32;` plus `proxy_http_version 1.1;` and
  `proxy_set_header Connection "";`.
- **DNS on every request** — `proxy_pass` to a variable hostname resolves each time; use a static
  upstream or `resolver … valid=300s`.
- **Route over the overlay relay** — if the overlay has no direct path between the two hosts, traffic
  goes through a relay (the data host's overlay address answers the local machine in ~309 ms).
- TLS 1.3 and HTTP/2 towards clients save a round trip on new connections.

## What is still on the table

- Moving the API onto the data host (the proxy, Postgres and Redis are already there) removes the
  proxy hop and the DB/Redis RTT — expected `/health` ~160 ms, a DB GET ~170 ms from the client.
- Every grid runs a page query and a count: two round trips. `count(*) OVER ()` would make it one,
  at a price — past the last page the window returns no rows and the total reads as 0, so a pager
  that overshoots would be told the collection is empty. Worth ~24 ms; not taken yet.
- Reference lists are cached in Redis since the insured-fleet module (`Utils/DomainCache`,
  generation-counter invalidation — see `insured-fleet.md`). The rest of the domain is deliberately
  uncached: it is read immediately after somebody writes it.

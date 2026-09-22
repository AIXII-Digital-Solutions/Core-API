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

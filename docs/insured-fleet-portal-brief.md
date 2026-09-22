# Insured fleet — integration brief for the portal

You are building the portal screens for the insured-aircraft domain. This is everything you need
about the API: the shapes it sends, the rules it enforces, and the traps that will otherwise cost
you an afternoon. It is written to be read once, front to back, before you write the first request.

The schema rationale lives in [`insured-fleet.md`](insured-fleet.md); read that only if you need to
know *why*. This document is the contract.

---

## 1. Connecting

Base URL: **`https://api.aixii.com/api/v1`**

| | |
|---|---|
| interactive schema | `https://api.aixii.com/api/docs` (and `/api/redoc`) |
| machine-readable schema | `https://api.aixii.com/openapi.json` — at the ROOT, not under `/api/v1`. Generate your client from this. |
| current version | `v1.1.0` |

Every request carries one of two credentials:

| header | who |
|---|---|
| `X-Service-Token: <token>` | the platform's own backends. Full access, no scopes. |
| `X-Api-Key: <prefix>.<secret>` | a scoped key minted at `/tokens`. Needs `insurance:read` for GETs, `insurance:write` for POST/PATCH/DELETE (or the `admin` superscope). |

Use an **API key**, not the service token — the portal is a caller, not a platform backend. Ask for
a key with `insurance:read insurance:write`.

**Every write is attributed to your key's name** in the audit trail, which is what the history
screens display as "changed by". Give the key a name a human will recognise; `portal` is fine,
`key-3` is not.

Trailing slashes are optional — `/ref/airlines` and `/ref/airlines/` both work, no redirect.

---

## 2. The envelope

Every response, success or failure, has the same three keys.

```json
{
  "status_code": 200,
  "details": { "msg": "Success", "correlationId": "d376aebd-19fa-498e-8081-f3b647696afa" },
  "data": { "items": [ ... ], "total": 4 }
}
```

On an error, `data` is `[]` and `details.msg` is the message to show the user:

```json
{
  "status_code": 400,
  "details": {
    "msg": "Unknown sort field 'nope'. Allowed: airline_name, created_at, iata, icao, is_asg, updated_at",
    "correlationId": "ca1a5062-3ce6-464a-bbad-2e23cb646b98"
  },
  "data": []
}
```

**`details.msg` is written for a person.** The API deliberately names the rule that was broken —
"that aircraft is already covered by a policy over part of this period", not "constraint violation".
Show it. Do not map status codes to your own strings; you will say less than the API already did.

**Log `correlationId` on every failure.** It is the only way anybody can find that request in the
server logs. The same value is on the `X-Correlation-ID` response header of every response,
success or not, so a client can log it without parsing the body.

Responses also carry `Server-Timing: app;dur=<ms>` — time spent inside the API, so a slow call
can be attributed to the service or to the network without guessing.

### Status codes

| code | means | what the UI should do |
|---|---|---|
| `200` / `201` | fine | — |
| `400` | the request is wrong in a way the API can explain (unknown sort field, ambiguous aircraft type, a rule broken) | show `details.msg` |
| `401` | missing or invalid credential | re-auth |
| `403` | the key lacks the scope | tell the user, do not retry |
| `404` | no such id | — |
| `409` | it conflicts with data already stored (duplicate, overlap, still referenced) | show `details.msg`; this is usually actionable |
| `422` | the body failed validation before it reached the database | field-level errors, see below |
| `500` | our fault | show the correlation id |

A `422` carries per-field detail in `data`, which you can bind straight to form fields:

```json
{ "status_code": 422,
  "details": { "msg": "Validation error", "correlationId": "99bd6471-…" },
  "data": [ { "field": "body.master_series", "msg": "Field required",
              "correlationId": "99bd6471-…" } ] }
```

(The correlation id is repeated on each entry; `field` is dotted from the request root, so
`body.master_series` and `body.engines.0.position` bind straight to your form.)

---

## 3. Conventions that hold everywhere

**A grid always answers `{"items": [...], "total": N}`** — never a bare array. `total` is the size
of the whole filtered set, not of the page, so you can render the pager from the first response.
Paging is `limit` (default 50, max 200) and `offset`.

**Sorting is `sort=<field>&order=asc|desc`**, and the field must be one the endpoint allows. Send a
wrong one and you get a 400 that *lists the allowed fields* — that is the cheapest way to discover
them. NULLs always sort last, in both directions.

**Writes take names, not ids.** `airline`, `aircraft_type`, `engine_type`, `lessor`, `insured`,
`reinsured`, `retrocedent` and the lease agreement are all resolved by name and created if absent.
So the portal's forms can be free-text with a typeahead and do not need to manage ids at all.
Where you do already hold an id, most endpoints accept it too (`aircraft_id`, `agreement_id`,
`insured_id`, `engine_type_id`).

Name matching is **case- and whitespace-insensitive**: `AerCap`, `AERCAP ` and `  aercap` are the
same counterparty. You cannot create a near-duplicate by accident, and you will get a 409 if you
try.

**The four reference listings are cached** — `/ref/airlines`, `/ref/parties`,
`/fleet/aircraft-types`, `/fleet/engine-types`. Type as fast as you like into a typeahead; those
answers come from Redis, keyed on the exact query you sent.

**You do not have to do anything about it, and you must not work around it.** Every write that can
change one of those listings invalidates it, including the ones you would not think of: creating an
aircraft can create an airline and two types as a side effect, and all three listings update. A row
you just wrote is in the next read. Nothing else in the domain is cached — an aircraft, a lease, a
policy, the comparison and the history are always read fresh, because they are read right after
somebody changes them.

**Dates** are `YYYY-MM-DD`. **Timestamps** are ISO-8601 with an offset (`2026-09-21T19:05:49.343046+00:00`).
**Money** is a plain JSON number, two decimals, and the currency lives in the aircraft's service
block. **`depreciation_ratio` and `reinsured_amount` are PERCENT** — send `5` for 5 %/year and
`97.5` for 97.5 %, never `0.05`.

---

## 4. The objects, and how they fit

```
ref.airline ─────┐                 ref.party ──────┬── lessor on an agreement
                 │                                 ├── insured / reinsured / retrocedent
                 ▼                                 │   on a policy
        fleet.aircraft ──┬── fleet.service_info    │   (+ its contact blocks)
         (the airframe)  │   (1:1, the service     │
                         │    block)               │
                         ├── fleet.aircraft_engine │
                         │   (one row per          │
                         │    installation)        │
                         │                         │
                         ├── leasing.aircraft_lease ──> leasing.agreement ──> lessor
                         │   (what the lease REQUIRES)
                         │
                         └── policy.coverage ──────────> policy.policy ─────> insured
                             (what the policy PROVIDES)
```

The one idea worth internalising: **`leasing` says what cover is required, `policy` says what is
provided.** Three amounts — `combined_single_limit`, `hull_spares_war_excess_liability`,
`hull_deductible_buy_down` — exist on both sides on purpose so they can be compared. That
comparison is a first-class endpoint (§8).

---

## 5. Endpoint map

```
REFERENCE
  GET    /ref/airlines                     ?q= &is_asg= &limit= &offset= &sort= &order=
  POST   /ref/airlines                     · GET|PATCH|DELETE /ref/airlines/{id}
  GET    /ref/parties                      ?q= &role=lessor|insured|reinsured|retrocedent
  POST   /ref/parties                      · GET|PATCH|DELETE /ref/parties/{id}
  POST   /ref/parties/{id}/contacts        · PATCH|DELETE /ref/contacts/{id}

CATALOGUES  (identical shape — one component can drive both)
  GET    /fleet/aircraft-types             ?q= &manufacturer=
  POST   /fleet/aircraft-types             · PATCH|DELETE /fleet/aircraft-types/{id}
  GET    /fleet/engine-types               ?q= &manufacturer=
  POST   /fleet/engine-types               · PATCH|DELETE /fleet/engine-types/{id}

FLEET
  GET    /fleet/aircraft                   ?q= &airline_id= &type_id=
  POST   /fleet/aircraft
  GET    /fleet/aircraft/{id}              ?on_date= &history=true
  GET    /fleet/aircraft/by-registration/{registration}   ?msn= &on_date= &history=
  PATCH  /fleet/aircraft/{id}   ·   DELETE /fleet/aircraft/{id}
  GET    /fleet/aircraft/{id}/engines      ?fitted_only=false
  POST   /fleet/aircraft/{id}/engines      · PATCH|DELETE /fleet/engines/{id}
  GET    /fleet/aircraft/{id}/service      · PATCH /fleet/aircraft/{id}/service

LEASING
  GET    /leasing/agreements               ?q= &lessor_id=
  POST   /leasing/agreements               · GET|PATCH|DELETE /leasing/agreements/{id}
  GET    /leasing/leases                   ?aircraft_id= &agreement_id= &on_date= &current_only=true
  POST   /leasing/leases                   · GET|PATCH|DELETE /leasing/leases/{id}

POLICY
  GET    /policy/policies                  ?insured_id= &on_date= &active=false
  POST   /policy/policies                  · GET|PATCH|DELETE /policy/policies/{id}
  POST   /policy/policies/{id}/renew
  GET    /policy/coverage                  ?aircraft_id= &policy_id= &on_date=
  POST   /policy/coverage                  · PATCH|DELETE /policy/coverage/{id}
  GET    /policy/coverage/compare          ?on_date= &mismatches_only=false

HISTORY  (read-only)
  GET    /history/                         ?schema= &table= &row_id= &changed_by= &operation=
                                           &since= &until=
  GET    /history/aircraft/{id}            ?include=aircraft,engines,service,leases,coverage
```

---

## 6. Screens

### 6.1 Fleet grid — `GET /fleet/aircraft`

Each row is complete: type, airline, service block and every engine. **No per-row follow-up call.**

```json
{
  "id": 14, "registration": "4R-EXR", "msn": "3183",
  "aircraft_type": { "id": 18, "manufacturer": "Airbus", "master_series": "A320", "template_url": null },
  "airline": { "id": 31, "airline_name": "FitsAir", "icao": "EXV", "iata": "8D",
               "is_asg": false, "logo_url": null },
  "service": { "id": 5, "agreed_value_fixed": false, "source": "cirium", "status": "insured",
               "usage_status": "In Service", "lease_currency": "USD", "policy_currency": "USD",
               "recorded": true },
  "engines": [
    { "id": 41, "position": 1,
      "engine_type": { "id": 126, "manufacturer": "International Aero Engines",
                       "master_series": "V2500-A5" },
      "msn": null, "installed_on": null, "details": null, "fitted": true },
    { "id": 42, "position": 2, "engine_type": { "id": 126, … }, "fitted": true }
  ]
}
```

`?q=` matches the registration **separator-insensitively** — `YLLTD`, `yl-ltd` and `YL LTD` all find
`YL-LTD` — or the MSN. Sort by `registration`, `msn`, `created_at`, `updated_at`.

### 6.2 Aircraft card — `GET /fleet/aircraft/{id}` or `.../by-registration/{reg}`

Everything above, plus:

| key | what |
|---|---|
| `as_of` | the date the card was read for (`?on_date=`, default today) |
| `lease` | the lease terms **in force on that date**, or `null` |
| `coverage` | the policy **in force on that date**, or `null` |
| `lease_history` | every lease record ever, newest first (omit with `?history=false`) |
| `coverage_history` | every coverage row ever |

`?on_date=2025-06-30` is how you build a "as it stood then" view: the card answers with the lease
and policy that were in force on that day, not today's.

`by-registration` is separator-insensitive and takes `?msn=` to disambiguate a tail number that two
airframes have shared.

### 6.3 Adding an aircraft — `POST /fleet/aircraft`

```json
{
  "registration": "YL-LTD",
  "msn": "3210",
  "aircraft_type": "A320",         "manufacturer": "Airbus",
  "airline": "SmartLynx Latvia",
  "engines": [
    { "position": 1, "engine_type": "V2500-A5", "engine_manufacturer": "International Aero Engines",
      "msn": "V12345", "installed_on": "2024-03-01" },
    { "position": 2, "engine_type": "V2500-A5", "engine_manufacturer": "International Aero Engines" }
  ],
  "service": { "source": "manual", "usage_status": "In Service" }
}
```

Three things to know:

**It is find-or-create, by MSN first.** Posting a registration that already exists under that MSN
**updates** the aircraft instead of creating a twin — you get `200` and `"Aircraft already known —
updated in place"` rather than `201`. That is how a re-registration is recorded: post the new tail
with the old MSN.

**`service` is optional.** Omit it and the aircraft gets `source: cirium`, `status: insured`,
currencies `USD`. Set `source: "manual"` when a person typed the record in — the column records
where the *record* came from, and it is what tells a later reader which rows the Cirium feed owns.

**Engine positions are the aircraft's own left-to-right, as the pilot sees it** — not as you see it
standing in front of the nose, which reverses the sides:

| engines | 1 | 2 | 3 | 4 |
|---|---|---|---|---|
| 2 | left wing | right wing | | |
| 3 | left | centre / tail | right | |
| 4 | left outboard | left inboard | right inboard | right outboard |

A three-engine aircraft with a tail engine (MD-11, Falcon 900) numbers the tail engine **2**,
because numbering follows lateral position and the tail sits on the centreline. Put this in the
form's help text; it is the single most common data-entry error in this domain.

### 6.4 Engine swap — `POST /fleet/aircraft/{id}/engines`

**An engine row is an INSTALLATION, not a slot.** To record a swap, POST a new row at the same
position with a later `installed_on`. Do **not** PATCH the old row — that rewrites history instead
of adding to it.

The API marks the newest installation per position `"fitted": true` and the superseded ones `false`.
`GET /fleet/aircraft/{id}/engines?fitted_only=true` gives just the current set. PATCH exists only
for correcting a row that was typed wrong.

### 6.5 The service block — `GET|PATCH /fleet/aircraft/{id}/service`

Six fields, all with defaults, one row per aircraft:

| field | values | default |
|---|---|---|
| `source` | `manual` · `lease_agreement` · `cirium` | `cirium` |
| `status` | `insured` · `not_insured` | `insured` |
| `usage_status` | free text — Cirium's word for what the airframe is doing: `In Service`, `Storage`, `Retired`, `Written off`, `On order`, `Type swap`, … | `null` |
| `agreed_value_fixed` | boolean — `true` freezes the agreed value | `false` |
| `lease_currency`, `policy_currency` | `USD` · `EUR` · `GBP` (sent lower-case is fine, it is upper-cased) | `USD` |

`recorded: false` in a response means no row exists yet and you are looking at the defaults. Render
it exactly like a stored row; a PATCH creates it.

**`status: not_insured` is an answer, not a blank.** It states that somebody decided this aircraft
carries no cover — which is different from an aircraft nobody has got to yet. The comparison report
(§8) reads it and stops flagging that aircraft. Make it a deliberate choice in the UI, not a default
the user can drift into.

`usage_status` is a free-text field because Cirium owns that vocabulary and adds to it. Offer the
known values as suggestions; do not make it a closed dropdown.

### 6.6 Type catalogues — `/fleet/aircraft-types`, `/fleet/engine-types`

Identical shape, so one component drives both: `{id, manufacturer, master_series}` (aircraft types
also carry `template_url`).

**A type is the manufacturer AND the master series together.** 48 airframe series are built by more
than one manufacturer under licence — Kawasaki builds the BK117, Mitsubishi the CRJ family and the
UH-60, Harbin the ERJ-145, Viking Air the DHC-6. So `?q=CRJ900` legitimately returns two rows, and
`?manufacturer=Mitsubishi` pins one.

This has a consequence you must handle: **sending only `aircraft_type` for an ambiguous series is a
400** that lists the builders —

> `Aircraft type 'CRJ900' is built by several manufacturers (Bombardier (Canadair), Mitsubishi). Send `manufacturer` as well to say which.`

Catch it and prompt for the manufacturer rather than surfacing it as a generic error. The same rule
and the same message apply to `engine_type` / `engine_manufacturer`.

### 6.7 Counterparties — `/ref/parties`

One row per legal entity whatever role it plays. `?role=lessor|insured|reinsured|retrocedent`
filters by **actual usage** — parties really referenced in that role — not by a stored flag, so it
cannot go stale.

Contact blocks are rows, matching how the source documents write them:

```json
{ "name": "BOC Aviation (Ireland) Limited",
  "details": "…",
  "contacts": [ { "company": "BOC Aviation (Ireland) Limited",
                  "contact": "Insurance", "email": "insurance@bocaviation.com",
                  "phone": null, "note": null } ] }
```

Send them inline on `POST /ref/parties`, or manage them afterwards at
`POST /ref/parties/{id}/contacts` and `PATCH|DELETE /ref/contacts/{id}`.

### 6.8 Airlines — `/ref/airlines`

`is_asg` decides which fleet an airline's aircraft belong to, and therefore **which tails
FlightRadar is polled for**. Changing it takes effect only after the platform's fleet matviews are
refreshed — the PATCH response says so in `details.msg`. Surface that message; a user who flips the
switch and sees nothing change will flip it back.

Airline names here are matched against Cirium **by substring**, so they are deliberately short:
the reference holds `Air Arabia` and Cirium writes `Air Arabia Abu Dhabi`. Do not "helpfully"
lengthen a name in the UI.

---

## 7. Leases and policies

### 7.1 A lease record has no end date

`POST /leasing/leases` records the terms for one aircraft from an `effective_date`. **A
renegotiation is another POST with a later date**, not a PATCH — the earlier row keeps its period,
and that sequence is the business history.

`GET /leasing/leases?aircraft_id=…` returns the terms **in force today** (one row per aircraft);
pass `current_only=false` for the whole sequence, which is what the card's `lease_history` shows.

The aircraft must already exist; the agreement is found-or-created from `agreement_name` +
`agreement_start_date` (+ `lessor`).

### 7.2 Agreed value: two figures, never one

| field | what it is |
|---|---|
| `agreed_value_preliminary` | the value at inception |
| `agreed_value_final` | what the schedule **states** as the depreciated value |
| `agreed_value_calculated` | what the formula gives at `agreed_value_as_of` |

The formula is compounding on whole years since `depreciation_start_date`:
`preliminary × (1 − ratio/100) ^ floor(years)`, and `agreed_value_fixed` in the service block turns
it off entirely.

**Show both, and flag a divergence.** They are supposed to agree; when they do not, that is
information, not an error, and the API deliberately does not resolve it for you.

### 7.3 A policy covers a fleet; `coverage` attaches the aircraft

Every limit and deductible lives on `policy.policy`, once. `POST /policy/coverage` puts one aircraft
on it; `covered_from` / `covered_to` default to the policy period and are set explicitly only for a
mid-term delivery or redelivery.

**An aircraft holds one policy at a time.** A second overlapping coverage is refused by the database
with a 409:

> `That aircraft is already covered by a policy over part of this period. An aircraft holds one policy at a time — end the existing coverage or correct it instead of adding a second.`

Consecutive annual policies meeting on 31 Dec / 1 Jan do **not** collide — the rule is about genuine
double-insurance.

### 7.4 Renewal is one call — `POST /policy/policies/{id}/renew`

```json
{ "carry_aircraft": true, "overrides": { "hull_all_risks_deductible": 6000000 } }
```

It creates next year's policy with the same terms and carries the aircraft onto it, **leaving the
expiring policy and its coverage rows untouched**. `period_from` defaults to the day after the
current period ends; anything in `overrides` replaces a copied value. The response adds
`aircraft_carried`.

This is the primary workflow of the whole domain — make it one button, not a wizard that re-types
eighteen limits. A policy renewed six times leaves an aircraft with six coverage rows, each pointing
at the contract in force that year, which is exactly what makes its insurance readable later.

Refused with a 400 when the expiring policy is open-ended and you did not give `period_from` —
the two periods would overlap.

### 7.5 Delete means "this should never have existed"

A lease that ended, a policy that expired, coverage that lapsed: **none of those are deletes.** The
lease keeps its period and the next record supersedes it; the policy keeps its period; coverage is
closed with `covered_to`. DELETE is for a row entered in error, and it is refused with a 409 while
anything still references it — an aircraft with contracts, a policy with aircraft on it, a type in
use.

Deletes are recoverable in the sense that matters: the audit trail keeps the full pre-image, so
`/history` still reads after the row is gone.

---

## 8. The comparison report — `GET /policy/coverage/compare`

Required versus provided, per aircraft, as of `on_date` (today by default). This is the report the
two-sided schema exists for.

```json
{
  "as_of": "2026-09-21", "total": 149,
  "items": [{
    "aircraft": { "id": 13, "registration": "2-EZBC", … },
    "has_lease": false, "has_policy": false,
    "lease_id": null, "policy_id": null,
    "status": "insured", "usage_status": "Storage",
    "match": false,
    "fields": {
      "combined_single_limit":            { "required": null, "provided": null, "match": true },
      "hull_spares_war_excess_liability": { "required": null, "provided": null, "match": true },
      "hull_deductible_buy_down":         { "required": null, "provided": null, "match": true }
    }
  }]
}
```

`mismatches_only=true` keeps only the rows that need attention — the limits disagree, or one side is
missing entirely. An aircraft whose service block says `status: not_insured` counts as **answered**
and drops out, carrying its reason in `status` / `usage_status`.

Note `limit` defaults to 200 here and the whole fleet fits in one page; raise it to 1000 if the
fleet grows.

---

## 9. History

Two different things, and the portal needs both.

**Business history is the sequence of rows** — the lease records, the coverage rows. "What covered
this aircraft in 2025" is `GET /fleet/aircraft/{id}?on_date=2025-06-30`, not a history call.

**Technical history is `/history`** — who changed what, and when.

```json
{ "id": 2279, "schema": "fleet", "table": "aircraft", "row_id": 162,
  "operation": "INSERT",
  "changed_at": "2026-09-21T19:05:49.343046+00:00",
  "changed_by": "service-token",
  "changes": [
    { "field": "registration", "old": null, "new": "ST-PSR" },
    { "field": "msn",          "old": null, "new": "114" },
    { "field": "aircraft_type","old": null, "new": "Dassault Falcon 900" },
    { "field": "airline",      "old": null, "new": "Government of Sudan" }
  ],
  "old_row": { … }, "new_row": { … } }
```

**Render `changes`.** Foreign keys are already resolved to names — `aircraft_type` reads
`"Dassault Falcon 900"`, not `329` — so an info panel needs no further lookups. `old_row` /
`new_row` are the raw snapshots, there for the rare field the diff skips (`id`, `created_at`,
`updated_at`); do not render them by default.

`GET /history/aircraft/{id}` is the one to put behind the card's "history" tab: it gathers the
airframe's own changes and those of its engines, service block, lease records and coverage rows into
one timeline. Narrow it with `?include=aircraft,engines,service,leases,coverage`.

`/history/` filters by `schema`, `table`, `row_id` (one object's trail), `changed_by` (one actor),
`operation`, and `since` / `until`. Send an unknown table and the 400 lists the real ones.

The log is **read-only**: there is no endpoint that writes or deletes it, by design.

---

## 10. What is in there right now

| | rows |
|---|---|
| `fleet.aircraft` | 149 |
| `fleet.service_info` | 149 |
| `fleet.aircraft_engine` | 300 |
| `fleet.aircraft_type` | 806 |
| `fleet.engine_type` | 365 |
| `ref.party` | 97 |
| `ref.airline` | 22 |
| `leasing.*`, `policy.*` | **0** |

The fleet and both catalogues were loaded from Cirium. **No lease and no policy exists yet** — so
`/leasing` and `/policy` return empty grids, every aircraft card shows `lease: null` and
`coverage: null`, and the comparison report lists all 149 as unmatched. That is correct, not a bug:
those contracts are entered through the portal, which is what you are building.

Consequences for your first release: the lease and policy screens must work from an empty state,
and the comparison report's most common row is "no lease, no policy".

---

## 11. What does not exist, so do not design for it

* **Claims / loss events.** The old claims tables were removed and will be modelled again later. If
  you see `/forecast/claims` in the schema, that is a *different thing* — aggregated claims
  experience per airline and year, feeding the forecast model, not per-aircraft losses.
* **Image upload.** `template_url` (aircraft type) and `logo_url` (airline) are URLs into an image
  store the platform does not have yet. Treat them as plain text fields the user pastes a link into.
* **Bulk import.** There is no file-upload endpoint for this domain; a schedule is loaded row by
  row through `POST`.
* **Engine serials from Cirium.** Every engine currently has `msn: null` and `installed_on: null`
  because the Cirium feed carries neither. The fields exist and work; they are simply empty until
  somebody types them.
* **Aircraft build year, delivery date, sub-series, market value.** Present in Cirium, no column
  here, deliberately.

---

## 12. The seven things most likely to bite you

1. **An ambiguous aircraft or engine type is a 400, not a guess.** Catch it and ask for the
   manufacturer. (§6.6)
2. **Engine positions are the pilot's left-to-right**, and a tail engine is number 2. (§6.3)
3. **A swap is a new engine row, never a PATCH.** (§6.4)
4. **A renegotiation is a new lease row, never a PATCH.** (§7.1)
5. **A renewal is `/renew`, never a PATCH of the policy** — patching destroys the expiring terms,
   which is the history somebody will need. (§7.4)
6. **Delete is for mistakes only**, and is refused while anything references the row. (§7.5)
7. **`status: not_insured` is a decision with a consequence** — it removes the aircraft from the
   comparison report. Never let it be set by accident. (§6.5)

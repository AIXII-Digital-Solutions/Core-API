# Aircraft insurance — schema and API

Owner: core-api. Schema: `api` (in the `aixii` database). Migrations: `ad60f0b27298` (policies and
records), `fa38ae0ab542` (claims).
Models: `db-contract/Database/ApiModels.py` (runtime copy `app/Database/ApiModels.py`).
Routers: `app/Routers/Insurance.py` (policies/records), `app/Routers/Claims.py` (claims), with the
shared plumbing in `app/Utils/InsuranceCommon.py`. Scopes: `insurance:read`, `insurance:write`.

The source data is two flat schedules — one row per aircraft per policy (25 columns), and one row
per loss event (19 columns). This document records how those rows are normalised, and the decisions
that are easy to undo wrongly.

## Tables

```
reference     api.airlines            (pre-existing)  airline
              api.parties             lessee, lessor, surveyor, leader — ONE table, the referencing
                                      column decides the role
              api.aircraft_types      aircraft_type   (+ default_* fallbacks for the importer)
              api.engine_types        engines_type

airframe      api.aircrafts           registration, msn, -> aircraft_type, -> airline
              api.aircraft_specs      1:1 OPTIONAL — mtow_kg, number_of_engines, source
              api.aircraft_engines    one row per installed engine, replaces engine_msn_1..4

insurance     api.insurance_policies  the contract: airline, number, period, currency, CSL
              api.insurance_records   one time-bounded record per aircraft — everything else
              api.insurance_record_history   audit trail, written by a DB trigger

claims        api.insurance_claims    one loss event: damage, reserves, payments, who is handling it
              api.insurance_claim_history    its audit trail, also trigger-written
```

### Where each source column went

| source column | lands in |
|---|---|
| `registration`, `msn` | `aircrafts` |
| `aircraft_type` | `aircraft_types` ← FK from `aircrafts` |
| `airline` | `airlines` ← FK from `aircrafts` and `insurance_policies` |
| `mtow`, `number_of_engines` | `aircraft_specs` (`mtow_kg`) |
| `engines_type`, `engine_msn_1..4` | `engine_types` + `aircraft_engines` |
| `policy_from`, `policy_to`, `combined_single_limit` | `insurance_policies` |
| `lessee`, `lessor` | `parties` ← FK from `insurance_records` |
| `status`, `source`, `hull_deductible`, `hull_spares_deductible`, `agreed_value_inception`, `agreed_value`, `agreed_value_fixed`, `depriciation_date`, `depriciation_rate` | `insurance_records` |

Note the last two: the source spells them `depriciation_*`, the schema and the API use the correct
`depreciation_*`. Map on ingest; do not propagate the typo.

### …and for the claims schedule

| source column | lands in |
|---|---|
| `registration`, `msn` | `aircrafts` (find-or-create) |
| `airline` | `airlines` ← FK from `insurance_claims` |
| `policy_period` | `insurance_policies` ← FK `policy_id` |
| `surveyor`, `leader` | `parties` ← FK from `insurance_claims` |
| everything else (`type_of_damage`, `date_of_loss`, `location_of_loss`, `paid_date`, `indemnity_reserve`, `paid_amount`, `damage`, `hd_*`, `hw_*`, `hsl_*`) | `insurance_claims` |

## Decisions worth not re-litigating

**MSN is the aircraft's identity, not the registration.** A tail number changes on re-registration
and can be reissued to a different airframe; the manufacturer serial cannot. Hence
`UNIQUE (msn) WHERE msn IS NOT NULL` and only an index on `registration`.
`registration_normalized` (a STORED generated column: upper, separators stripped) makes lookups
separator-insensitive — `YLLTD` finds `YL-LTD`.

**Never FK to `api.registration`.** That table is a projection of `cirium.asg` rebuilt by
`api.sync_registration_from_asg()` with `TRUNCATE … RESTART IDENTITY` after every asg refresh, so
its `id` is not stable. Join to it on `reg`/`msn` if you need the asg view; anchor on
`api.aircrafts`.

**Technical data is a separate 1:1 table**, not nullable columns on `aircrafts`. A missing
`aircraft_specs` row means "we have no technical data", which nullable columns could not
distinguish from "known to be empty".

**Engines are rows, not four columns.** They are separately-serialised assets that get swapped
between airframes. `installed_from` / `installed_to` give the swap history; the currently-fitted set
is `installed_to IS NULL`, guarded by a partial unique index on `(aircraft_id, position)`.

**`parties` is one table for every counterparty** — lessee, lessor, surveyor, lead underwriter —
because the same entity is a lessor on one aircraft and a lessee on another (sub-lease), and an
insurer can also act as surveyor. Separate tables would duplicate legal entities. The `is_lessee` /
`is_lessor` / `is_surveyor` / `is_insurer` flags accumulate as they are seen and exist only so the
portal can filter autocomplete; they never restrict which column may reference a row.

**`policy_id` on a record is nullable.** A `not_insured` record states that the aircraft is knowingly
uncovered over that period and has no contract behind it. `ck_insurance_records_insured_needs_policy`
keeps the pairing honest: `status = 'insured'` requires a policy.

**Two kinds of history, both needed.**
1. *Business history* — a renewal is a NEW policy + a NEW record; the old record keeps its period.
   "What was in force in 2024" is a period query, not an audit query.
2. *Technical audit* — an endorsement or correction inside a live period is an in-place UPDATE, and
   the trigger `api.insurance_records_audit()` copies the pre-image and post-image into
   `api.insurance_record_history`. Chosen over versioned rows so that reads of the current state
   stay a plain SELECT with no `WHERE is_current`.

`changed_by` reads the `app.actor` GUC, which the router sets per transaction with
`set_config('app.actor', <token name>, true)`. Changes made outside the API record the DB login.

**The exclusion constraint.** `ex_insurance_records_no_overlap` is
`EXCLUDE USING gist (aircraft_id WITH =, daterange(effective_from, effective_to, '[]') WITH &&)`
— an aircraft has exactly one known insurance state on any given day. It needs `btree_gist`
(created by the migration). **If the business ever layers concurrent policies on one aircraft (hull
+ war risk), drop this constraint** — nothing else in the schema depends on it. The API surfaces a
violation as `409`.

**Money and units.** All amounts are `NUMERIC(18,2)`, never float. The currency is a column on the
policy (`CHAR(3)`, default `'USD'`) so a non-USD contract needs no migration and no amount is ever
ambiguous. `mtow_kg` is kilograms. `depreciation_rate` is a **fraction per annum** (`0.05` = 5 %/yr),
constrained to `[0, 1]`.

**Record-level `combined_single_limit` is an OVERRIDE.** Read it as
`COALESCE(record.combined_single_limit, policy.combined_single_limit)`; the API already does. Most
schedules set the CSL once per policy, so the write path only stores it on the record when it
differs.

## Claims — decisions worth not re-litigating

**The money is FLAT on the claim, deliberately.** `hd_reserve` / `hd_paid` / `hw_*` / `hsl_*` are six
columns on `api.insurance_claims`, not a child table keyed by coverage section — even though the
sections line up exactly with the three `type_of_damage` values. The tidier normal form loses on the
one thing this table exists for: a single row means a SINGLE audit trigger captures every money
movement in one snapshot. Split the amounts out and you split their history too. The section set is
closed and fixed, so the flexibility a child table buys is flexibility nobody needs.

**Totals are not derived from the breakdown.** `indemnity_reserve` / `paid_amount` are the claim
totals as the schedule states them; the schedule supplies both levels and they do not always
reconcile. Store what was stated. The read layer exposes `outstanding` (= reserve − paid) and list
`totals`; nothing silently overwrites a stated figure with a computed one.

**`policy_period` is a link, not a string.** A claim points at `api.insurance_policies`. Send the
period explicitly and it is found-or-created against the airline (so a claim can be loaded before
its policy row exists); omit it and the claim attaches to whichever policy actually covered THAT
aircraft on `date_of_loss`, read from `api.insurance_records`. The FK is `ON DELETE RESTRICT` — a
claim must never vanish because a policy row was deleted.

**No overlap constraint here.** Unlike `insurance_records`, claims may freely coincide: one aircraft
can suffer several distinct losses on the same day. The only uniqueness is `claim_reference`, and
only when it is supplied (partial unique index).

**The claims trigger fires on INSERT too**, unlike the records one, hence `old_row` is nullable in
`api.insurance_claim_history`. The portal renders a full timeline per claim — who opened it, then
every reserve and payment movement — and the create event with its actor is part of that story.

**The API returns diffs, not just snapshots.** The trigger persists whole-row JSONB (self-contained,
survives schema drift), which is right to store and wrong to render. `GET …/history` turns each
snapshot pair into `changes: [{field, old, new}]` with foreign keys already resolved to names
(`surveyor: "Charles Taylor" → "McLarens"`, not `surveyor_id: 3 → 7`), so an info button needs no
further lookups. `old_row` / `new_row` still ride along for anything the diff skips (it skips `id`,
`created_at`, `updated_at`). `GET /insurance/{record_id}/history` gained the same `changes` key.

## API

All endpoints are under `/api/v1/insurance`. Reads need `insurance:read`, writes `insurance:write`;
the master `X-Service-Token` satisfies both.

```
POST   /insurance                        add a record (the flat schedule row)
PATCH  /insurance/{record_id}            endorsement / correction, audited
GET    /insurance/aircraft/{registration}  everything known about one aircraft (incl. its claims)
GET    /insurance/{record_id}/history    audit trail of one record, with `changes`
GET    /insurance                        list with filters (q, airline, status, on_date)

POST   /insurance/claims                 register a loss event (the flat claims row)
PATCH  /insurance/claims/{claim_id}      reserve movement / settlement / correction, audited
GET    /insurance/claims/{claim_id}      one claim in full
GET    /insurance/claims/{claim_id}/history   the info-button payload — see above
GET    /insurance/claims                 list + `totals` (q, airline, type_of_damage,
                                         date_from, date_to, settled)
```

`Claims.py` is registered BEFORE `Insurance.py` in `Routers/__init__.py` so the literal
`/insurance/claims` is matched before any `/insurance/{record_id}` pattern. Keep that order.

`POST` takes the source row verbatim — no surrogate ids. Reference rows (airline, aircraft type,
engine type, lessee, lessor) and the policy itself are found-or-created; reference lookups match on
the normalised name, so `AerCap` and `AERCAP ` cannot become two rows. `effective_from` /
`effective_to` default to the policy period; pass them explicitly for a mid-term delivery or
redelivery.

```bash
curl -X POST https://api.aixii.com/api/v1/insurance \
  -H "X-Service-Token: $SERVICE_TOKEN" -H "Content-Type: application/json" -d '{
    "registration": "YL-LTD", "msn": "3210", "airline": "SmartLynx",
    "aircraft_type": "A320-232", "mtow": 77000, "number_of_engines": 2,
    "engines_type": "V2527-A5", "engine_msn_1": "V12345", "engine_msn_2": "V12346",
    "policy_from": "2025-01-01", "policy_to": "2025-12-31",
    "policy_number": "AV-2025-001", "combined_single_limit": 750000000,
    "status": "insured", "source": "lease_agr",
    "lessee": "SmartLynx Airlines", "lessor": "AerCap",
    "hull_deductible": 1000000, "hull_spares_deductible": 100000,
    "agreed_value_inception": 40000000, "agreed_value": 38000000,
    "agreed_value_fixed": false,
    "depreciation_date": "2025-01-01", "depreciation_rate": 0.05
  }'
```

Response codes worth knowing:

* `409` — the aircraft already has a record overlapping that period (the exclusion constraint).
  Renew by POSTing a **non-overlapping** period; correct the existing one with `PATCH`.
* `409` — that MSN already belongs to a different aircraft.
* `400` — an `insured` record without an airline/policy, or `effective_to` before `effective_from`.

`GET /insurance/aircraft/{registration}` is separator-insensitive and returns the aircraft with its
type, operator, specs, all engines (fitted and removed), the record in force on `on_date` (default
today), every claim ever raised against it and, unless `history=false`, every record ever held. Pass
`?msn=` to disambiguate when two airframes have shared a tail number.

### Claims

`POST /insurance/claims` takes the claims row verbatim. The aircraft is found-or-created (a claim can
arrive before the insurance schedule does), as are the airline, surveyor and leader; the policy is
resolved as described above. `type_of_damage` is one of `hull_deductible` / `hull_war` /
`hull_spares`.

```bash
curl -X POST https://api.aixii.com/api/v1/insurance/claims \
  -H "X-Service-Token: $SERVICE_TOKEN" -H "Content-Type: application/json" -d '{
    "registration": "YL-LTD", "msn": "3210",
    "type_of_damage": "hull_deductible",
    "date_of_loss": "2025-06-15", "location_of_loss": "RIX, hangar 2",
    "damage": "Left wingtip contacted ground equipment",
    "claim_reference": "AV-CLM-2025-118",
    "surveyor": "Charles Taylor Adjusting", "leader": "Global Aerospace",
    "indemnity_reserve": 250000, "hd_reserve": 250000
  }'
```

Settle it later with a `PATCH` — `{"paid_amount": 180000, "paid_date": "2025-09-01",
"hd_paid": 180000, "indemnity_reserve": 200000}` — and the pre-image lands in the history
automatically. Response codes: `409` on a duplicate `claim_reference`, `400`/`422` when `paid_date`
precedes `date_of_loss` or an amount is negative.

`GET /insurance/claims` returns `{"claims": [...], "totals": {count, indemnity_reserve, paid_amount,
outstanding}}`. The totals cover the whole filtered set, not the returned page, so paging never
distorts the exposure figure.

## Changing the schema

The usual three steps (see `../CLAUDE.md`): edit `db-contract/Database/ApiModels.py`, run
`python tools/migrate.py revision aixii "…"` then `upgrade aixii head` **with the Core-API venv's
python**, then copy the model file to `app/Database/ApiModels.py`.

Things autogenerate cannot emit and will silently omit if you regenerate from scratch: the exclusion
constraint and BOTH audit triggers (`api.insurance_records_audit()`,
`api.insurance_claims_audit()`). Autogenerate also proposes unrelated drift on `api.registration`
and `cirium.ciriumaircrafts` — that predates this domain; strip it from any new revision.

Raw SQL in a migration runs through `sa.text()` and then asyncpg: one statement per `op.execute()`
(a function and its trigger must be two calls), and no `:word` sequences (they read as bind
parameters).

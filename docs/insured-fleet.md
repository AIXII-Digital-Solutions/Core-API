# The insured aircraft — schemas, columns and why they are where they are

Owner: core-api. Schemas: **`ref`**, **`fleet`**, **`leasing`**, **`policy`**, **`audit`** (all in the
`aixii` database). Migrations: `insured_fleet_rebuild`, `airlines_to_ref`. Models:
`db-contract/Database/RefModels.py`, `FleetModels.py`, `LeasingModels.py`, `PolicyModels.py`,
`AuditModels.py` (runtime copies under `app/Database/`). Routers: `app/Routers/Ref.py`, `Fleet.py`,
`Leasing.py`, `Policies.py`, `History.py`, with the shared plumbing in `app/Utils/DomainCommon.py`.
Scopes: `insurance:read`, `insurance:write`.

This replaces the single `insurance` schema, which was **dropped** — with its claims tables, its two
audit trails, its three enums and the routers that served them. Nothing was migrated because nothing
was in it: all eleven tables were empty.

## The shape

```
ref       airline               the airlines this business insures or tracks
          party                 every counterparty: lessor, insured, reinsured, retrocedent
          party_contact         one row per COMPANY / CONTACTS / EMAIL block

fleet     aircraft_type         manufacturer, master series, template drawing
          engine_type           manufacturer, master series — the same shape
          aircraft              the airframe — registration, MSN, -> type, -> ref.airline
          aircraft_engine       one row per installation, position 1..4, -> engine_type
          service_info          1:1 with the aircraft — the specification's service block

leasing   agreement             the lease contract — name, start, lessor, currency
          aircraft_lease        one aircraft under it: agreed values, depreciation, required cover

policy    policy                the insurance contract — insured/reinsured/retrocedent, period,
                                every deductible and limit
          coverage              this aircraft is covered by this policy over this window

audit     change_log            one row per INSERT/UPDATE/DELETE on any of the above
```

**The airline moved in.** It was `api.airlines`; revision `airlines_to_ref` made it `ref.airline`
once the domain had been rebuilt around it. The move was a catalogue update — no data copied, ids
and the sequence untouched, grants carried along — and the four cirium matviews that resolve
operator strings against it kept working without being rebuilt, because PostgreSQL records a view's
dependencies by OID rather than by name. What is left in `api` is `api.registration`, the hand-kept
list of tails to poll FlightRadar for, which is a different job.

`ref.airline` is deliberately NOT merged into `ref.party`: an airline carries ICAO and IATA codes
and is matched on them, a counterparty is matched on its name, and the two are distinct in every
source the platform reads.

## Why four schemas and not one

The old `insurance` schema held the airframe, the contract and the loss in one bag, and they are not
one subject. They have different lifetimes (an airframe outlives every contract written about it),
different owners (the lease comes from the lessor, the policy from the broker) and different read
patterns. Splitting them makes each schema's grants, and each schema's meaning, independent.

The division that matters most is **`leasing` vs `policy`: required versus provided.** The lease
stipulates cover; the policy provides it. Three columns exist on both sides deliberately —

| on `leasing.aircraft_lease` | on `policy.policy` |
|---|---|
| `combined_single_limit` | `combined_single_limit` |
| `hull_spares_war_excess_liability` | `hull_spares_war_excess_liability` |
| `hull_deductible_buy_down` | `hull_deductible_buy_down` |

— so that "is this aircraft insured to the standard its lease demands" is a comparison between two
rows. **Never collapse them into one column.** They are equal most of the time and the times they
are not are the whole point.

## Where each field from the specification went

**1. Aircraft identity → `fleet.aircraft`**

| spec | column |
|---|---|
| Registration | `registration` (+ `registration_normalized`, generated) |
| MSN | `msn` — UNIQUE where not null |
| Type | `aircraft_type_id` → `fleet.aircraft_type` |
| Airline | `airline_id` → `ref.airline` |

**2. Engines → `fleet.aircraft_engine`**

| spec | column |
|---|---|
| Master Series | `engine_type_id` → `fleet.engine_type` |
| MSN | `msn` |
| Position | `position`, CHECK 1..4 |
| Installed | `installed_on` |
| Details | `details` |

**3. Lease information → `leasing.agreement` + `leasing.aircraft_lease`**

| spec | table.column |
|---|---|
| Agreed Value Preliminary | `aircraft_lease.agreed_value_preliminary` |
| Agreed Value Final | `aircraft_lease.agreed_value_final` |
| Depreciation Ratio | `aircraft_lease.depreciation_ratio` — **percent** |
| Depreciation Start Date | `aircraft_lease.depreciation_start_date` |
| Combined Single Limit | `aircraft_lease.combined_single_limit` |
| Hull and Spares War Excess Liability | `aircraft_lease.hull_spares_war_excess_liability` |
| Hull Deductible Buy Down | `aircraft_lease.hull_deductible_buy_down` |
| Lessor | `agreement.lessor_id` → `ref.party` |
| Alternative Contract Party | `agreement.alternative_contract_party` |
| Lease Agreement Name | `agreement.name` |
| Lease Agreement Start Date | `agreement.start_date` |
| Contracts to be mentioned other than the lease | `agreement.other_contracts` |
| Effective date | `aircraft_lease.effective_date` |

**4. Policy information → `policy.policy`**

| spec | column |
|---|---|
| Insured / Reinsured / Retrocedent | `insured_id` / `reinsured_id` / `retrocedent_id` → `ref.party` |
| Policy Period From / To | `period_from` / `period_to` |
| Hull All Risks Deductible | `hull_all_risks_deductible` |
| Spares Deductible | `spares_deductible` |
| Reinsured Amount | `reinsured_amount` — **percent** |
| Hull war risk of confiscation limit | `hull_war_confiscation_limit` |
| … on flights to the selected country | `hull_war_confiscation_limit_selected_country` |
| Select Country | `selected_country` |
| Hull War Overall Limit | `hull_war_overall_limit` |
| Hull Spares Limit | `hull_spares_limit` |
| Cut through clause | `cut_through_clause` |
| Combined Single Limit | `combined_single_limit` |
| Hull and Spares War Excess Liability | `hull_spares_war_excess_liability` |
| Hull Deductible Buy Down | `hull_deductible_buy_down` |
| Hull Deductible Aggregate | `hull_deductible_aggregate` |

**5. Service fields → `fleet.service_info`**, one row per aircraft. The six fields were scattered
across three tables until revision `service_info_table`; they are one block about one aircraft, so
they live together beside the airframe they describe.

| spec | column | default |
|---|---|---|
| Agreed Value Fixed | `agreed_value_fixed` | `false` |
| Source | `source` — manual / lease_agreement / cirium | **`cirium`** |
| Status | `status` — insured / not_insured | `insured` |
| Usage Status | `usage_status` — Cirium's `Status`, verbatim | — |
| Lease Agreement Currency | `lease_currency` | `USD` |
| Policy Currency | `policy_currency` | `USD` |

`source` defaults to `cirium` because most records arrive from the feed. Both currencies are
`VARCHAR(3)` with a CHECK of `USD` / `EUR` / `GBP`, not an enum: adding a currency is then one
migration that touches no type shared by two schemas.

The row is created with the aircraft, so every airframe has one. A missing row reads as these
defaults with `recorded: false` rather than as a screen of nulls.

**6. Aircraft type → `fleet.aircraft_type`** — `manufacturer`, `master_series`, `template_url`.
The two names are unique TOGETHER (normalised), not the series alone — see below.

**7. Airline → `ref.airline`** — `airline_name`, `icao`, `iata` and `is_asg` came with the table;
`logo_url` is new.

**8. Reusable entities → `ref.party` + `ref.party_contact`** — `name` (unique, normalised),
`details`, and one contact row per block.

## Decisions worth not re-litigating

**Holding the currencies per aircraft gives something up, and it is worth knowing.** They are
properties of a CONTRACT — one lease agreement covers several aircraft in one currency, one policy
likewise. On `fleet.service_info` nothing stops two aircraft on the same agreement recording
different currencies for it: the schema can no longer state that they must agree, so whoever writes
them must. Putting `currency` back on `leasing.agreement` / `policy.policy` would restore it and
leaves the rest of the block alone — the two are not entangled.

**`status = not_insured` is an answer, not a gap.** It states that somebody decided this aircraft
carries no cover over this record's period, which is a different fact from an aircraft nobody has
entered a policy for yet. `GET /policy/coverage/compare` reads it off the aircraft's service block: a declared gap counts as
matched and carries its reason, so the report shows only the cases that are actually unexplained.
The default is `insured`, so saying nothing means the ordinary case.

**`usage_status` is Cirium's word, stored verbatim — and it is TEXT, not an enum.** Cirium uses
twelve values today (`In Service`, `Storage`, `On order`, `Retired`, `Written off`, `Cancelled`,
`Type swap`, `LOI to Order`, `LOI to Option`, `On option`, `Reengineered`, `Unknown`) and owns that
vocabulary; `Type swap` and `Reengineered` are not a set anyone would have predicted. An enum would
turn each new value into a migration that blocks an import.

It sits on the aircraft, not on a lease record, so keeping it current is an ordinary
`PATCH /fleet/aircraft/{id}/service` and not an audited change to a contract. A sync job can own it
without touching anything the lease says.

**MSN is the identity, not the registration.** A tail number changes on re-registration and can be
reissued to a different airframe; the manufacturer serial cannot. Hence `UNIQUE (msn) WHERE msn IS
NOT NULL` and only an index on `registration`. `registration_normalized` (upper, separators
stripped) makes lookups separator-insensitive — `YLLTD` finds `YL-LTD`.

**One `party` table for every role.** The same company is a lessor on one aircraft, the insured on a
policy and a retrocedent on another. The role is decided by the referencing column
(`agreement.lessor_id`, `policy.insured_id`, …) and never by a flag on the party, so no entity is
stored twice and no flag can disagree with a link. The portal filters autocomplete by role with an
`EXISTS` over the referencing tables.

**Contacts are rows.** The source documents carry several `COMPANY / CONTACTS / EMAIL` blocks per
entity (a group lists its subsidiaries), so they are rows in `ref.party_contact` and not a text blob:
the portal renders a list and an address can be searched for.

**An engine model is a reference too, keyed exactly like an airframe type.**
`fleet.engine_type` holds the manufacturer and the master series, and `fleet.aircraft_engine`
points at it instead of spelling the model out on every installation. What stays on the
installation is what is true of THAT engine and no other: its serial, when it went on, the note.

Cirium nests engine names four deep — Engine Type (V2500), Engine **Master Series** (V2500-A5),
Engine Series (V2527), Engine Sub Series (V2527-A5); for CFM, CFM56 / CFM56-5 / CFM56-5B /
CFM56-5B3/3. The catalogue holds the MASTER SERIES, the same granularity the airframe side holds,
so both halves of the domain describe hardware at one level rather than two. A finer column is one
migration away if a schedule ever needs the sub-series (464 pairs at series level, 952 at
sub-series).

**An aircraft type is the manufacturer AND the master series.** Cirium carries 806 distinct pairs
across Commercial and Business & Helicopters but only 751 distinct series: **48 series are built by
more than one manufacturer** under licence — Kawasaki builds the BK117, Mitsubishi the CRJ family and
the UH-60, Harbin the ERJ-145, Viking Air the DHC-6, Indonesia Aerospace the CN235. Same design,
different build. `uq_aircraft_type_manufacturer_series` is therefore over the pair, declared NULLS
NOT DISTINCT so a series entered without a manufacturer still cannot be inserted twice
(revision `aircraft_type_manufacturer`).

The consequence for the write path: looking a type up by series alone can match several rows.
`get_or_create_aircraft_type` matches the pair when a manufacturer is given, and by series alone
otherwise — but only when exactly ONE row matches. An ambiguous series with no manufacturer is
rejected with a 400 listing the builders, because resolving it by guesswork would attach the
aircraft to the wrong one — and the same resolver serves engine models, so the rule is one rule.
No engine master series currently collides; the key is still the pair so the first one that does
needs no migration.

Both catalogues are loaded by `_admin/load_types.py` from Cirium: **806 airframe types** over 191
manufacturers, **365 engine models** over 54. `template_url` stays NULL — the drawings are not in
Cirium.

**The fleet itself is loaded too** — `_admin/load_insured_fleet.py` reads the four
`cirium.asg_*` / `cirium.non_asg_insured_*` matviews into `fleet.aircraft` (148),
`fleet.service_info` (148), `fleet.aircraft_engine` (297) and `ref.party` (97). Nine registrations
appear twice because the matviews expose three Cirium revisions as current; the newest revision
wins. What Cirium cannot supply is left empty rather than approximated: no lease (it has a period
and a lessor but no agreement name and no money — and `Indicative Market Value` is a market
estimate, not a contractual agreed value) and no policy at all.

**Engine swaps are new rows, not edits.** There is no `installed_to` — a removal is implied by the
next installation at that position. The fitted set is

```sql
SELECT DISTINCT ON (aircraft_id, position) *
FROM fleet.aircraft_engine
ORDER BY aircraft_id, position, installed_on DESC NULLS LAST, id DESC
```

`UNIQUE (aircraft_id, position, installed_on)` with NULLS NOT DISTINCT stops a position collecting
two undated rows, which would leave "which one is fitted" undecidable.

**Engine positions are the pilot's left-to-right**, not the view from in front of the nose, which
reverses the sides. Two engines: 1 left, 2 right. Three: 1 left, 2 centre/tail, 3 right — the tail
engine is on the centreline, so it takes the middle number (L-1011, DC-10, MD-11, and the
rear-engined Falcon 50/900/7X/8X). Four: 1 left outboard, 2 left inboard, 3 right inboard, 4 right
outboard.

**A lease row has no end date.** A change of terms is a NEW row with a later `effective_date`; the
terms in force on a day are the newest row not later than it. Same rule as the engines, and for the
same reason: the business history is the sequence of rows, the audit log is what happened to one row.

**Agreed Value Final is stored, and also computable.** The schedule states it, and a stated figure
and a formula do not always reconcile — so the column holds what was stated.
`leasing.agreed_value_at(preliminary, ratio, start_date, fixed, on_date)` recomputes it from the same
inputs so the read layer can show both and flag a divergence. The formula is **compounding on whole
years elapsed**: `preliminary × (1 − ratio/100) ^ floor(years since start_date)`, and it returns the
preliminary value untouched when `agreed_value_fixed` is true. Change the function, not the column,
if the business means straight-line instead.

**An aircraft holds one policy at a time.** `ex_coverage_no_overlap` is

```sql
EXCLUDE USING gist (aircraft_id WITH =, daterange(covered_from, covered_to, '[]') WITH &&)
```

(needs `btree_gist`). The range is inclusive at both ends, so consecutive annual policies meeting on
31 Dec / 1 Jan do not collide, but a genuine double-insurance does. **If the business ever layers
concurrent contracts** — a separate war-risk policy alongside the all-risks one — drop that
constraint; nothing else depends on it. The API should surface a violation as `409`.

**Money and units.** Every amount is `NUMERIC(18,2)`, never float. `depreciation_ratio` and
`reinsured_amount` are **percent** (`5.0` = 5 %/yr, `97.5` = 97.5 %), constrained to `[0, 100]` —
the old schema used a fraction for depreciation, the spec and the portal use percent, so the column
does too. Negative amounts are rejected by a `LEAST(...) >= 0` check, which ignores NULLs and so lets
unknown figures through.

**Images are URLs, never bytes.** `fleet.aircraft_type.template_url` and `ref.airline.logo_url`
point into the platform's image store. A grid reads every row on the page; a blob per row would drag
megabytes through the connection for nothing.

## History

Two kinds, both kept, answering different questions.

**Business history** is the sequence of rows. A policy renewal is a new `policy.policy` and a new
`policy.coverage`; the old rows keep their periods and are never touched. So an aircraft insured for
six years has six coverage rows, each pointing at the contract in force that year — "what covered
this aircraft in 2025" is a period query, not an audit query. A change of lease terms is the same
pattern in `leasing.aircraft_lease`.

**Technical history** is `audit.change_log`: one row per INSERT / UPDATE / DELETE on every table in
`ref`, `fleet`, `leasing` and `policy`, written by one trigger function,
`audit.log_change()`.

```
schema_name | table_name | row_id | operation | changed_at | changed_by | old_row | new_row
```

* **One table, not a `*_history` twin per subject.** The old design needed a new table, a new
  trigger function and a new endpoint per audited table, and "what happened to this aircraft last
  week" was a UNION across all of them. Here it is one `WHERE`.
* **Whole-row JSONB, not a stored diff.** A snapshot is self-contained and survives a later schema
  change; a diff stops making sense the moment a column is renamed. `to_jsonb(NEW)` also means a
  column added tomorrow is audited without touching anything. Rendering the diff is the read layer's
  job — with foreign keys resolved to names, so an info button needs no further lookups.
* **No foreign key on `row_id`**, deliberately: the audit row must outlive the row it describes, so
  the history of a deleted object still reads and its newest entry is the deletion, with the actor.
* **A no-op UPDATE is not logged.** The trigger compares the two snapshots with `updated_at`
  removed first, so saving a form without changing anything does not fill the log.
* **`changed_by`** is the `app.actor` GUC when the API sets it
  (`set_config('app.actor', <token name>, true)` inside the transaction), falling back to the
  database login for anything done outside the API. **A write path that does not set it records
  `svc_api` and nothing more** — setting the actor is the API's job on every write.

## API

> Building a client? Read **[insured-fleet-portal-brief.md](insured-fleet-portal-brief.md)**
> instead — the same API written as an integration contract, with real payloads and the rules that
> are easy to get wrong. What follows is the summary for somebody working on the service itself.

All endpoints are under `/api/v1`. Reads need `insurance:read`, writes `insurance:write`; the master
`X-Service-Token` satisfies both.

```
/ref/airlines                     GET (search)  POST  .  /{id} GET PATCH DELETE
/ref/parties                      GET (search)  POST  .  /{id} GET PATCH DELETE
/ref/parties/{id}/contacts        POST          .  /ref/contacts/{id} PATCH DELETE

/fleet/aircraft-types             GET  POST  .  /{id} PATCH DELETE
/fleet/engine-types               GET  POST  .  /{id} PATCH DELETE
/fleet/aircraft                   GET  POST  .  /{id} GET PATCH DELETE
/fleet/aircraft/by-registration/{registration}   GET  (separator-insensitive, ?msn= disambiguates)
/fleet/aircraft/{id}/engines      GET  POST  .  /fleet/engines/{id} PATCH DELETE
/fleet/aircraft/{id}/service      GET  PATCH

/leasing/agreements               GET  POST  .  /{id} GET PATCH DELETE
/leasing/leases                   GET  POST  .  /{id} GET PATCH DELETE

/policy/policies                  GET  POST  .  /{id} GET PATCH DELETE
/policy/policies/{id}/renew       POST
/policy/coverage                  GET  POST  .  /{id} PATCH DELETE
/policy/coverage/compare          GET    required vs provided, per aircraft

/history                          GET    the change log, filtered
/history/aircraft/{id}            GET    one airframe's whole timeline
```

### Conventions

**Every grid returns `{items, total}`**, never a bare array, and `total` is the whole filtered set
rather than the page — so a client can page without a second call to learn the size.

**Sorting is whitelist-driven.** `sort=<field>&order=asc|desc`; the field is looked up in a map the
router declares and an unknown one returns 400 listing what is allowed, so no caller string ever
reaches ORDER BY. NULLs always sort last in both directions.

**Writes take NAMES, not ids.** `airline`, `aircraft_type`, `engine_type`, `lessor`, `insured`,
`reinsured`, `retrocedent` and the lease agreement are found-or-created on the normalised name, so
`AerCap` and `AERCAP ` cannot become two rows. Ids are accepted too wherever the caller already has
one.

**The two type references are served identically.** An airframe type and an engine model are the
same shape — a (manufacturer, master series) pair loaded from Cirium — so `/fleet/aircraft-types`
and `/fleet/engine-types` take the same parameters and answer the same payload, and one portal
component can drive both. Naming a series several manufacturers build, without saying which, is a
400 that lists them rather than a guess.

**The schema states the rules; the API translates the verdict.** Nothing re-checks a constraint in
Python before writing — that would be two sources of truth with a race between them. The write goes
ahead and `Utils/DomainCommon.integrity_error` turns the violation into the right status and a
message naming the actual rule. Note where the constraint name lives: SQLAlchemy's asyncpg adapter
re-raises the driver error as its own `IntegrityError` carrying only `sqlstate`; the real asyncpg
exception, the one with `constraint_name`, hangs off it as `__cause__`.

**A serializer that reads `updated_at` needs the row refreshed after an UPDATE.** The column's
`onupdate` is a SQL expression, so SQLAlchemy expires the attribute once the flush has run; reading
it lazily issues IO, and outside the greenlet the async session runs in that raises
`MissingGreenlet` — turning a working PATCH into a 500. `lease_json` and `policy_json` are the two
that expose timestamps, and their handlers refresh `updated_at` explicitly.

**The reference listings are cached; nothing else is.** `/ref/airlines`, `/ref/parties`,
`/fleet/aircraft-types` and `/fleet/engine-types` are read by every typeahead keystroke, are large
(806 airframe types, 365 engine models) and change a handful of times a year, so they are served
from Redis. An aircraft, a lease, a policy, the coverage comparison and the change log are NOT
cached: they are read immediately after somebody writes them, and showing a portal user a stale
copy of their own change is worse than the query it saved.

Correctness comes from explicit invalidation, not from the TTL. Each entity has a GENERATION
counter in Redis and the payload key contains it, so a write bumps the counter and every cached
listing of that entity becomes unreachable at once — one `INCR`, no `SCAN`. That matters because
this Redis also carries the FlightRadar polling set and the status channel; scanning the keyspace
on every write would walk data that has nothing to do with this domain. `INSURED_FLEET_CACHE_SECONDS`
(default 300) is only a backstop for an invalidation that failed to land.

The trap when adding a cached listing is the third step, not the first two: read it through
`cached()`, and invalidate its entity from EVERY write that can touch it — **including the
find-or-create paths**, where `POST /fleet/aircraft` quietly creates an airline, an aircraft type
and an engine model, and so invalidates all three. `Utils/DomainCache` refuses an entity name it
does not know, so a typo cannot silently become a listing that never invalidates.

Everything fails open: a Redis error falls through to the database. A cache that can take the
endpoint down with it is a worse bug than the latency it saves.

**Every write sets the actor.** `set_actor(session, token)` issues
`set_config('app.actor', …, true)` inside the transaction, which is what
`audit.change_log.changed_by` records. A write path that forgets it logs the database login,
`svc_api`, and nothing useful.

### The calls that carry the domain

`POST /fleet/aircraft` looks an airframe up by MSN first and only then by registration, so
re-posting a re-registered aircraft UPDATES it instead of creating a twin; the response says which
happened. Recording an engine SWAP is a POST of a new installation at the same position with a later
date — never a PATCH of the old row, which would rewrite history instead of adding to it.

`POST /policy/policies/{id}/renew` is the yearly path: it creates next year's contract with the same
terms (`period_from` defaults to the day after the current one ends, `overrides` replaces any copied
value) and carries the aircraft onto it, leaving the expiring policy and its coverage rows alone.
That is what makes an aircraft's insurance readable years later.

`GET /policy/coverage/compare` is the report the two-sided schema exists for: the three limits the
lease stipulates beside the same three on the policy in force, per aircraft, with
`mismatches_only=true` to see just the disagreements — including an aircraft that has a lease and no
policy, or the reverse. A lease whose `status` is `not_insured` counts as answered rather than as a
missing policy, and the row carries `status` and `usage_status` so the reason is visible.

`GET /fleet/aircraft/{id}?on_date=2025-06-30` reads the lease terms and the policy that were in
force on that day, with the full history and the service block beside them.

`PATCH /fleet/aircraft/{id}/service` is where the service block is maintained — the insurance
status, what Cirium says the airframe is doing, whether the agreed value depreciates, and the two
currencies. The row is created with the defaults if the aircraft has none.

`GET /history/aircraft/{id}` gathers the airframe's own changes and those of its engines, lease
records and coverage rows into one timeline. Each entry carries a field-level `changes` list with
foreign keys already resolved to names, plus the raw snapshots for anything the diff skips.

### Agreed value

`agreed_value_final` is what the schedule STATES; `agreed_value_calculated` is the formula applied
at `on_date`. Both are returned and neither overwrites the other. Two of the formula's inputs live
in the service block rather than on the lease — `agreed_value_fixed`, which switches depreciation
off, and the currency — so a lease payload resolves them through the aircraft. The formula exists twice — as
`leasing.agreed_value_at()` for reports and as `Utils/DomainCommon.agreed_value_at` for the API —
and the two are verified equal over randomised inputs. **Change both or neither.**

## Claims

Removed. `insurance.insurance_claims` and `insurance.insurance_claim_history` went with the schema.
Note that `forecast.acys_claims` is a **different thing** and is untouched: it holds claims
experience aggregated per airline and calendar year for the forecast model, not individual loss
events, and is served by `Routers/AcysClaims.py` at `/forecast/claims`.

## Changing the schema

The usual three steps (see `../CLAUDE.md`): edit the model in `db-contract/Database/`, run
`python tools/migrate.py revision aixii "…"` then `upgrade aixii head` **with the Core-API venv's
python**, then copy the changed file to `app/Database/`.

Things autogenerate cannot emit, and will silently omit if a revision is regenerated from scratch:
the **exclusion constraint** on `policy.coverage`, the **audit trigger function and its triggers**,
and `leasing.agreed_value_at()`. They are raw SQL in the migration on purpose — being invisible to
autogenerate is also what stops it proposing to drop them.

Two Alembic rules this domain keeps running into:

* **Cross-schema foreign keys must use the Column object.** `ForeignKey("ref.party.id")` is a string
  resolved inside the OWNING MetaData, which cannot see another Base's tables, and fails with
  `NoReferencedTableError`. Write `ForeignKey(Party.__table__.c.id)` and name the class in the
  relationship.
* **Raw SQL goes through `sa.text()` and then asyncpg**: one statement per `op.execute()` (a
  function and its trigger are two calls), no `:word` sequences anywhere — including inside SQL
  comments — because they read as bind parameters, and no `:=` in PL/pgSQL (use `=`).

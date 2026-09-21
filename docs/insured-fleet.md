# The insured aircraft — schemas, columns and why they are where they are

Owner: core-api. Schemas: **`ref`**, **`fleet`**, **`leasing`**, **`policy`**, **`audit`** (all in the
`aixii` database). Migration: `insured_fleet_rebuild`. Models: `db-contract/Database/RefModels.py`,
`FleetModels.py`, `LeasingModels.py`, `PolicyModels.py`, `AuditModels.py` (runtime copies under
`app/Database/`).

This replaces the single `insurance` schema, which was **dropped** — with its claims tables, its two
audit trails, its three enums and the routers that served them. Nothing was migrated because nothing
was in it: all eleven tables were empty.

## The shape

```
ref       party                 every counterparty: lessor, insured, reinsured, retrocedent
          party_contact         one row per COMPANY / CONTACTS / EMAIL block

fleet     aircraft_type         manufacturer, master series, template drawing
          aircraft              the airframe — registration, MSN, -> type, -> api.airlines
          aircraft_engine       one row per installation, position 1..4

leasing   agreement             the lease contract — name, start, lessor, currency
          aircraft_lease        one aircraft under it: agreed values, depreciation, required cover

policy    policy                the insurance contract — insured/reinsured/retrocedent, period,
                                every deductible and limit
          coverage              this aircraft is covered by this policy over this window

audit     change_log            one row per INSERT/UPDATE/DELETE on any of the above
```

`api.airlines` is **not** part of this domain and stays in `api`: the cirium asg sync resolves
against it, `api.registration` and the fleet matviews read it. `fleet.aircraft` links to it across
schemas, which is ordinary in PostgreSQL.

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
| Airline | `airline_id` → `api.airlines` |

**2. Engines → `fleet.aircraft_engine`**

| spec | column |
|---|---|
| Master Series | `master_series` (text) |
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

**5. Service fields** — `agreed_value_fixed`, `source` and the lease currency sit on the lease
(`leasing.aircraft_lease` / `leasing.agreement`); the policy currency on `policy.policy`. Both
currencies are `CHAR(3)` with a CHECK of `USD` / `EUR` / `GBP`, not an enum: adding a currency is
then one migration that touches no type shared by two schemas.

**6. Aircraft type → `fleet.aircraft_type`** — `manufacturer`, `master_series` (unique, normalised),
`template_url`.

**7. Airline → `api.airlines`** — `airline_name`, `icao`, `iata` were already there; `logo_url` is
new.

**8. Reusable entities → `ref.party` + `ref.party_contact`** — `name` (unique, normalised),
`details`, and one contact row per block.

## Decisions worth not re-litigating

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

**Images are URLs, never bytes.** `fleet.aircraft_type.template_url` and `api.airlines.logo_url`
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
`ref`, `fleet`, `leasing`, `policy` and on `api.airlines`, written by one trigger function,
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

**There is none yet.** `Routers/Insurance.py`, `Routers/Claims.py`, `Routers/InsuranceRefs.py` and
`Utils/InsuranceCommon.py` were deleted with the schema they served — they referenced tables that no
longer exist, and leaving them would have stopped the service from starting. The scopes
`insurance:read` / `insurance:write` are still defined in `app/api_auth.py` for the replacement.

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

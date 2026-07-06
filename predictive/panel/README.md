# predictive/panel — forecast data-prep (Cirium × FR24 → forecast.* tables)

Assembles the per-aircraft historical panel that feeds the airline-forecast engine. A frontend
request is `(operator, date)`. Everything runs in SQL against the `aixii` DB (the flightsummary side
is ~8M rows — never pulled into Python).

## Tables (owned by Core-API Alembic, migration `d7e8f9a0b1c2`, schema `forecast`)

- **history_1** — Cirium fleet rows LEFT-JOINed to their FR24 flights (date-respecting).
- **future_1** — identical columns; populated later by the forecast model (table only for now).
- **final_1** — history_1 (flights only) + future_1, enriched with origin/destination airport geography.

Column names are the quoted mixed-case names from the task spec / Cirium source
(`"Registration"`, `"Master Series"`, `"IATA Origin"`, …). The tables are **working sets**: each run
`TRUNCATE`s and rebuilds for the requested operator (pass `--no-truncate` to accumulate).

## Pipeline

**build** (steps 5–7) — `python -m predictive.panel.build --operator "Avianca" [--as-of YYYY-MM-DD]`
1. **array5** = `cirium.ciriumaircrafts` ⋈ `cirium.aircraftrevision` (`revision_id = id`) filtered by
   `"Operator"` and `to_date(period,'MM-YYYY') >= 2023-07-01`. `period` is `'MM-YYYY'` text, so it is
   ALWAYS parsed with `to_date` (a string compare is wrong: `'12-2023' > '01-2024'` lexically).
   Overlapping revisions for the same `(tail, month)` are deduped to the latest (`max(revision_id)`).
2. **array6** = `flightradar.flightsummary` for those tails, `[2023-07-01, as_of)`. Flight date =
   `coalesce(datetime_takeoff, first_seen)` (takeoff is true departure but ~4.5% null).
3. **history_1** = array5 LEFT JOIN array6 with `date_trunc('month', flight) = period-month` — a tail
   migrates between operators, so a `09-2024` Cirium row must never pick up a `07-2025` flight.
   Cirium rows with no flight in their month are KEPT here (null flight fields).
   - **FR24 backfill STUB**: tails with no flightsummary rows are reported; wiring the FR24 fetch
     (external-worker `FlightSummary.fetch_all_ranges`) is future work.

**merge** (step 8) — `python -m predictive.panel.merge`
- `final_1` = `history_1 WHERE "Date" IS NOT NULL` (FLIGHTS ONLY — no-flight Cirium rows dropped)
  `UNION ALL future_1`, LEFT-JOINed to airport geography.
- `main.virtual_airport_list` has ~2 rows per IATA (18k rows / 9k codes), so it is **deduped with
  `DISTINCT ON ("IATA Code")`** before the join — otherwise every panel row fans out ~4×.
- Destination geography keys on `"IATA Destination"` (dest_iata); switch to `"IATA Destination Actual"`
  if the actual-landing airport is wanted.

## Verified (operator = Avianca, 2026-07)

history_1 = 526,720 (526,010 with a flight + 710 Cirium-only) · final_1 = 526,010 (== flights-only,
future_1 empty) · date-respecting violations = 0 · final_1 rows without a flight = 0 · airport
enrichment origin 99.85% / dest 99.7% (unfilled = IATAs absent from the legacy list).

## Assumptions (flagged; easy to change)

- Flight date basis = `coalesce(datetime_takeoff, first_seen)` (task said "timestamp", which doesn't
  exist on flightsummary).
- Tables are a per-request working set (TRUNCATE+rebuild). An `"Operator"` column is present, so
  switching to multi-operator accumulation is trivial.
- `future_1` is table-only for now.

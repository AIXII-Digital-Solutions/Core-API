# predictive/panel — forecast data-prep (Cirium × FR24 → forecast.* tables)

Standalone harness that assembles the per-aircraft panel feeding the airline-forecast engine. The
**production path** is the `/forecast` endpoint → external-worker `forecast_panel` job; this CLI
writes the SAME shared `forecast.*` tables and mirrors that SQL. Everything runs in SQL against the
`aixii` DB (the flightsummary side is ~8M rows — never pulled into Python).

Request scope: `--operator` and/or `--registrations` (UNION).

## Tables (schema `forecast`, Core-API Alembic)

- **acys_actuals** (was history_1) — Cirium fleet rows LEFT-JOINed to their FR24 flights
  (date-respecting). **PERSISTS** across requests: a run DELETEs only ITS scope, then re-inserts.
- **acys_forecast** (was future_1) — identical columns; populated later by the forecast model.
  TRUNCATEd per request.
- **acys_summary** (was final_1) — acys_actuals (flights only) + acys_forecast, enriched with
  origin/destination airport geography (Country/City/Airport Name + lat/lon). TRUNCATEd per request.

Column names are the quoted mixed-case names from the spec / Cirium source. Beyond the flight/aircraft
fields: `Contract Year`, `Circle Distance`, `Flight Time`, and (populated in acys_actuals, carried to
acys_summary) `Agreed Value` (Cirium Indicative Market Value), `Total Seats` (Cirium Number of Seats),
`Total PAX` (Total Seats × `FORECAST_PAX_LOAD_FACTOR`, default 0.8), `Actual Distance FR`
(flightsummary.circle_distance), `Flight Time FR` (flightsummary.flight_time seconds → interval).

## Pipeline

**build** (steps 5–7) — `python -m predictive.panel.build --operator "Avianca" [--registrations R1,R2] [--as-of YYYY-MM-DD]`
1. **array5** = `cirium.ciriumaircrafts` ⋈ `cirium.aircraftrevision` (`revision_id = id`) filtered by
   the scope and `to_date(period,'MM-YYYY') >= 2023-07-01`. `period` is `'MM-YYYY'` text, so ALWAYS
   parsed with `to_date` (string compare is wrong: `'12-2023' > '01-2024'`). Overlapping revisions
   for the same `(tail, month)` are deduped to the latest (`max(revision_id)`).
2. **array6** = `flightradar.flightsummary` for those tails, `[2023-07-01, as_of)`. Flight date =
   `coalesce(datetime_takeoff, first_seen)`.
3. **acys_actuals** = array5 LEFT JOIN array6 with `date_trunc('month', flight) = period-month` — a
   tail migrates operators, so a `09-2024` Cirium row must never pick up a `07-2025` flight. Cirium
   rows with no flight in their month are KEPT here (null flight fields).
   - **FR24 backfill STUB**: tails with no flightsummary rows are reported; the FR24 fetch is TODO.

**merge** (step 8) — `python -m predictive.panel.merge [--operator ... | --registrations ...]`
- `acys_summary` = `acys_actuals WHERE "Date" IS NOT NULL` (FLIGHTS ONLY — no-flight rows dropped,
  optionally scoped) `UNION ALL acys_forecast`, LEFT-JOINed to airport geography.
- `main.virtual_airport_list` has ~2 rows per IATA (18k rows / 9k codes), so it is **deduped with
  `DISTINCT ON ("IATA Code")`** before the join — otherwise every panel row fans out ~4×.
- Destination geography keys on `"IATA Destination"` (dest_iata); switch to `"IATA Destination Actual"`
  if the actual-landing airport is wanted.

## Notes / assumptions

- Flight date basis = `coalesce(datetime_takeoff, first_seen)` (spec said "timestamp", which doesn't
  exist on flightsummary).
- Contract Year uses the START-year of the fiscal window anchored at the request date's month/day.
- `Actual Distance FR` == `Circle Distance` (both = flightsummary.circle_distance) — kept as separate
  columns per the spec.

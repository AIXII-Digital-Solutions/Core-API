"""Assemble forecast.history_1 for one operator (steps 5-7 of the forecast data-prep).

Pipeline (all in SQL — the flightsummary side is ~8M rows, so we never pull it into Python):

  array5  = Cirium fleet rows for the operator: one row per (Registration x monthly `period`),
            from period 07-2023 onward. `period` lives on cirium.aircraftrevision (joined via
            ciriumaircrafts.revision_id = aircraftrevision.id) and is stored as 'MM-YYYY', so it is
            parsed with to_date(period,'MM-YYYY') for every comparison (a plain string compare is
            WRONG: '12-2023' > '01-2024' lexically). Overlapping revisions for the same
            (tail, month) are deduped to the latest (max revision_id) — the platform's "latest" rule.

  array6  = FR24 flights (flightradar.flightsummary) for those tails, from 01.07.2023 to yesterday.
            The flight's timeline date is coalesce(datetime_takeoff, first_seen): takeoff is the
            true departure but ~4.5% null, first_seen (100% populated) is the fallback.

  history_1 = array5 LEFT JOIN array6 with a DATE-RESPECTING match: a flight only pairs with the
            Cirium row whose MONTH it falls in (date_trunc('month', flight) = period-month). A tail
            migrates between operators, so a 09-2024 Cirium row must never pick up a 07-2025 flight.
            Cirium rows with no flight in their month are KEPT here (null flight fields); they are
            dropped later in the merge to final_1.

Run:
    G:/Projects/Core-API/.venv/Scripts/python.exe -m predictive.panel.build --operator "Avianca"
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date

from predictive.db import DB

# Inclusive lower bound for both the Cirium period and the flight date (task: "начиная с 07-2023").
HISTORY_START = date(2023, 7, 1)

# The flight's timeline anchor: real takeoff when present, else first ADS-B contact.
_FLIGHT_DT = "coalesce(f.datetime_takeoff, f.first_seen)"


def _log(msg: str) -> None:
    print(msg, flush=True)


# Registrations (for this operator, >= 07-2023) that have NO flightsummary rows at all — the gap the
# FR24 backfill will eventually fill.
_MISSING_TAILS_SQL = """
SELECT DISTINCT ca."Registration" AS reg
FROM cirium.ciriumaircrafts ca
JOIN cirium.aircraftrevision r ON r.id = ca.revision_id
WHERE ca."Operator" = $1
  AND to_date(r.period,'MM-YYYY') >= $2
  AND NOT EXISTS (SELECT 1 FROM flightradar.flightsummary f WHERE f.reg = ca."Registration")
"""

_ASSEMBLE_SQL = f"""
INSERT INTO forecast.history_1
    ("Registration","Period","Date","Time Departed","Time Landed",
     "IATA Origin","IATA Destination","IATA Destination Actual",
     "Operator","Master Series","Manufacturer","Aircraft Sub Series","Primary Usage")
WITH array5 AS (
    SELECT DISTINCT ON (ca."Registration", to_date(r.period,'MM-YYYY'))
           ca."Registration"          AS registration,
           r.period                    AS period,
           to_date(r.period,'MM-YYYY') AS period_month,
           ca."Operator"               AS operator,
           ca."Master Series"          AS master_series,
           ca."Manufacturer"           AS manufacturer,
           ca."Aircraft Sub Series"    AS sub_series,
           ca."Primary Usage"          AS primary_usage
    FROM cirium.ciriumaircrafts ca
    JOIN cirium.aircraftrevision r ON r.id = ca.revision_id
    WHERE ca."Operator" = $1
      AND to_date(r.period,'MM-YYYY') >= $2
    ORDER BY ca."Registration", to_date(r.period,'MM-YYYY'), ca.revision_id DESC
),
array6 AS (
    SELECT f.reg, f.datetime_takeoff, f.datetime_landed,
           f.orig_iata, f.dest_iata, f.dest_iata_actual,
           {_FLIGHT_DT} AS flight_dt
    FROM flightradar.flightsummary f
    WHERE f.reg IN (SELECT registration FROM array5)
      AND {_FLIGHT_DT} >= $2
      AND {_FLIGHT_DT} <  $3          -- exclusive upper bound = as-of date -> includes up to yesterday
)
SELECT
    a5.registration, a5.period,
    a6.flight_dt::date,               -- "Date": NULL when no flight matched this Cirium month
    a6.datetime_takeoff, a6.datetime_landed,
    a6.orig_iata, a6.dest_iata, a6.dest_iata_actual,
    a5.operator, a5.master_series, a5.manufacturer, a5.sub_series, a5.primary_usage
FROM array5 a5
LEFT JOIN array6 a6
       ON a6.reg = a5.registration
      AND date_trunc('month', a6.flight_dt) = a5.period_month
"""


async def _fr24_backfill_stub(db: DB, operator: str) -> list[str]:
    """STUB — future: for tails with NO flightsummary rows, call the FR24 flight-summary API and
    insert the missing flights into flightradar.flightsummary so the assemble step below picks them
    up. For now it only REPORTS the gap. Wiring uses external-worker's
    API.FlightRadarAPI.FlightSummary.fetch_all_ranges (registrations=<missing>, start=2023-07-01)."""
    rows = await db.fetch(_MISSING_TAILS_SQL, operator, HISTORY_START)
    missing = [r["reg"] for r in rows]
    if missing:
        _log(f"[forecast.build] FR24 backfill STUB: {len(missing)} tail(s) have no flightsummary "
             f"rows (e.g. {missing[:5]}); FR24 fetch not yet wired — these tails contribute "
             f"Cirium-only rows for now.")
    return missing


async def build_history(operator: str, *, as_of: date | None = None, truncate: bool = True) -> int:
    """Assemble forecast.history_1 for `operator`. `as_of` is the request date (default today); the
    history window is [2023-07-01, as_of) so it ends at yesterday. Returns rows inserted."""
    as_of = as_of or date.today()
    async with DB(statement_timeout_ms=0) as db:
        if truncate:
            await db.execute("TRUNCATE forecast.history_1")
        await _fr24_backfill_stub(db, operator)
        tag = await db.conn.execute(_ASSEMBLE_SQL, operator, HISTORY_START, as_of)
        inserted = int(tag.rsplit(" ", 1)[-1]) if tag else 0
    _log(f"[forecast.build] operator={operator!r} as_of={as_of} -> history_1: {inserted} row(s)")
    return inserted


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="Assemble forecast.history_1 for one operator.")
    p.add_argument("--operator", required=True, help='Cirium "Operator" value, e.g. "Avianca"')
    p.add_argument("--as-of", type=date.fromisoformat, default=None,
                   help="request date YYYY-MM-DD (history ends the day before); default today")
    p.add_argument("--no-truncate", action="store_true", help="append instead of TRUNCATE+rebuild")
    return p.parse_args(argv)


def main(argv=None) -> None:
    a = _parse_args(argv)
    asyncio.run(build_history(a.operator, as_of=a.as_of, truncate=not a.no_truncate))


if __name__ == "__main__":
    main()

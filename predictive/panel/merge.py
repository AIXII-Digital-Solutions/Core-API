"""Merge forecast.history_1 + forecast.future_1 into forecast.final_1 (step 8).

final_1 = history_1 (FLIGHTS ONLY — the no-flight Cirium rows are dropped here, unlike history_1)
          UNION ALL future_1 (all rows), enriched with origin/destination airport geography
          (Country / City / Airport Name) looked up from main.virtual_airport_list by "IATA Code".

The "has a flight" test on history_1 is "Date" IS NOT NULL: the assemble step sets "Date" only when
a flight matched the Cirium row's month (see build.py).

Destination geography is keyed on "IATA Destination" (dest_iata). Switch the second join to
"IATA Destination Actual" if the actual-landing airport is wanted instead.

Run:
    G:/Projects/Core-API/.venv/Scripts/python.exe -m predictive.panel.merge
"""
from __future__ import annotations

import argparse
import asyncio

from predictive.db import DB

_PANEL_COLS = """"Registration","Period","Date","Time Departed","Time Landed",
     "IATA Origin","IATA Destination","IATA Destination Actual",
     "Operator","Master Series","Manufacturer","Aircraft Sub Series","Primary Usage\""""

_MERGE_SQL = f"""
INSERT INTO forecast.final_1
    ({_PANEL_COLS},
     "Origin Country","Origin City","Origin Airport Name",
     "Destination Country","Destination City","Destination Airport Name")
WITH airports AS (
    -- main.virtual_airport_list has ~2 rows per IATA (18k rows / 9k codes); a plain join would
    -- FAN OUT every panel row ~2x per side (~4x total). Collapse to ONE row per IATA first.
    SELECT DISTINCT ON ("IATA Code")
           "IATA Code" AS iata, "Country" AS country, "City" AS city, "Airport Name" AS airport_name
    FROM main.virtual_airport_list
    WHERE "IATA Code" IS NOT NULL AND "IATA Code" <> ''
    ORDER BY "IATA Code"
),
panel AS (
    SELECT {_PANEL_COLS}
    FROM forecast.history_1
    WHERE "Date" IS NOT NULL                 -- FLIGHTS ONLY: drop the no-flight Cirium rows
    UNION ALL
    SELECT {_PANEL_COLS}
    FROM forecast.future_1                    -- future rows: all of them
)
SELECT p.*,
       o.country, o.city, o.airport_name,
       d.country, d.city, d.airport_name
FROM panel p
LEFT JOIN airports o ON o.iata = p."IATA Origin"
LEFT JOIN airports d ON d.iata = p."IATA Destination"
"""


def _log(msg: str) -> None:
    print(msg, flush=True)


async def merge_final(*, truncate: bool = True) -> int:
    """Rebuild forecast.final_1 from the current history_1 + future_1. Returns rows inserted."""
    async with DB(statement_timeout_ms=0) as db:
        if truncate:
            await db.execute("TRUNCATE forecast.final_1")
        tag = await db.conn.execute(_MERGE_SQL)
        inserted = int(tag.rsplit(" ", 1)[-1]) if tag else 0
    _log(f"[forecast.merge] final_1: {inserted} row(s) "
         f"(history_1 flights-only + future_1, airport-enriched)")
    return inserted


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="Merge history_1 + future_1 -> final_1 (airport-enriched).")
    p.add_argument("--no-truncate", action="store_true", help="append instead of TRUNCATE+rebuild")
    return p.parse_args(argv)


def main(argv=None) -> None:
    a = _parse_args(argv)
    asyncio.run(merge_final(truncate=not a.no_truncate))


if __name__ == "__main__":
    main()

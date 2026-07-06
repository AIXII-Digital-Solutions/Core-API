"""Merge forecast.history_1 (FLIGHTS ONLY) + forecast.future_1 into forecast.final_1 (step 8) —
standalone harness mirror of the production path (external-worker). Prefer POST /forecast.

final_1 is per-request: TRUNCATEd and rebuilt. Pass --operator/--registrations to scope history_1
to the current request (matching the endpoint); with no scope it takes ALL accumulated history.
Adds the 3 columns (Contract Year / Circle Distance / Flight Time carried from history/future) and
origin/destination airport geography from main.virtual_airport_list (deduped to one row per IATA).
"""
from __future__ import annotations

import argparse
import asyncio

from predictive.db import DB

_COLS = """"Registration","Period","Date","Time Departed","Time Landed",
       "IATA Origin","IATA Destination","IATA Destination Actual",
       "Operator","Master Series","Manufacturer","Aircraft Sub Series","Primary Usage",
       "Contract Year","Circle Distance","Flight Time\""""


def _merge_sql(final_scope: str) -> str:
    return f"""
INSERT INTO forecast.final_1
    ({_COLS},
     "Origin Country","Origin City","Origin Airport Name",
     "Destination Country","Destination City","Destination Airport Name")
WITH airports AS (
    SELECT DISTINCT ON ("IATA Code")
           "IATA Code" AS iata, "Country" AS country, "City" AS city, "Airport Name" AS airport_name
    FROM main.virtual_airport_list
    WHERE "IATA Code" IS NOT NULL AND "IATA Code" <> ''
    ORDER BY "IATA Code"
),
panel AS (
    SELECT {_COLS} FROM forecast.history_1 WHERE "Date" IS NOT NULL {final_scope}
    UNION ALL
    SELECT {_COLS} FROM forecast.future_1
)
SELECT p.*, o.country, o.city, o.airport_name, d.country, d.city, d.airport_name
FROM panel p
LEFT JOIN airports o ON o.iata = p."IATA Origin"
LEFT JOIN airports d ON d.iata = p."IATA Destination"
"""


def _log(msg: str) -> None:
    print(msg, flush=True)


async def merge_final(operator: str | None = None, registrations: list[str] | None = None,
                      *, truncate: bool = True) -> int:
    """Rebuild forecast.final_1 from history_1 (flights only, optionally scoped to operator and/or
    registrations = UNION) + future_1."""
    parts, args = [], []
    n = 0
    if operator:
        n += 1; parts.append(f'"Operator" = ${n}'); args.append(operator)
    if registrations:
        n += 1; parts.append(f'"Registration" = ANY(${n})'); args.append(list(registrations))
    final_scope = ("AND (" + " OR ".join(parts) + ")") if parts else ""

    async with DB(statement_timeout_ms=0) as db:
        if truncate:
            await db.execute("TRUNCATE forecast.final_1")
        sql = _merge_sql(final_scope)
        tag = await (db.conn.execute(sql, *args) if args else db.conn.execute(sql))
        inserted = int(tag.rsplit(" ", 1)[-1]) if tag else 0
    _log(f"[forecast.merge] final_1: {inserted} row(s) (history flights-only"
         f"{' scoped' if args else ''} + future_1, airport-enriched)")
    return inserted


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="Merge history_1 + future_1 -> final_1 (airport-enriched).")
    p.add_argument("--operator", help="scope history_1 to this operator")
    p.add_argument("--registrations", help="scope history_1 to these comma-separated registrations")
    p.add_argument("--no-truncate", action="store_true", help="append instead of TRUNCATE+rebuild")
    return p.parse_args(argv)


def main(argv=None) -> None:
    a = _parse_args(argv)
    regs = [r.strip() for r in a.registrations.split(",") if r.strip()] if a.registrations else None
    asyncio.run(merge_final(operator=a.operator, registrations=regs, truncate=not a.no_truncate))


if __name__ == "__main__":
    main()

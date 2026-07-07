"""Merge forecast.acys_actuals (FLIGHTS ONLY) + forecast.acys_forecast into forecast.acys_summary
(step 8) — standalone harness mirror of the production path (external-worker). Prefer POST /forecast.

acys_summary is per-request: TRUNCATEd and rebuilt. Pass --operator/--registrations to scope
acys_actuals to the current request (matching the endpoint); with no scope it takes ALL accumulated
actuals. Adds origin/destination airport geography (Country/City/Airport Name + lat/lon) from
main.virtual_airport_list (deduped to one row per IATA).
"""
from __future__ import annotations

import argparse
import asyncio

from predictive.db import DB

_COLS = """"Registration","Period","Date","Time Departed","Time Landed",
       "IATA Origin","IATA Destination","IATA Destination Actual",
       "Operator","Master Series","Manufacturer","Aircraft Sub Series","Primary Usage",
       "Contract Year","Circle Distance","Flight Time",
       "Agreed Value","Total Seats","Total PAX","Actual Distance FR","Flight Time FR\""""


def _merge_sql(final_scope: str) -> str:
    return f"""
INSERT INTO forecast.acys_summary
    ({_COLS},
     "Origin Country","Origin City","Origin Airport Name",
     "Destination Country","Destination City","Destination Airport Name",
     origin_lat, origin_lon, dest_lat, dest_lon)
WITH va AS (   -- main.virtual_airport_list, one row per IATA (prefer the most complete)
    SELECT DISTINCT ON ("IATA Code")
           "IATA Code" AS iata, "Country" AS country, "City" AS city,
           "Airport Name" AS airport_name, "Latitude" AS lat, "Longitude" AS lon
    FROM main.virtual_airport_list
    WHERE "IATA Code" IS NOT NULL AND "IATA Code" <> ''
    ORDER BY "IATA Code", ("City" IS NULL), ("Airport Name" IS NULL)
),
fa AS (        -- flightradar.airports fallback (iata already unique)
    SELECT iata, country_name AS country, city, name AS airport_name, lat, lon
    FROM flightradar.airports
    WHERE iata IS NOT NULL AND iata <> ''
),
airports AS (
    -- per-field merge: prefer virtual, fill gaps from flightradar. City in particular is ~97% NULL
    -- in virtual_airport_list, so it comes from flightradar whenever it has the airport.
    SELECT coalesce(va.iata, fa.iata) AS iata, coalesce(va.country, fa.country) AS country,
           coalesce(va.city, fa.city) AS city, coalesce(va.airport_name, fa.airport_name) AS airport_name,
           coalesce(va.lat, fa.lat) AS lat, coalesce(va.lon, fa.lon) AS lon
    FROM va FULL OUTER JOIN fa ON va.iata = fa.iata
),
panel AS (
    SELECT {_COLS} FROM forecast.acys_actuals WHERE "Date" IS NOT NULL {final_scope}
    UNION ALL
    SELECT {_COLS} FROM forecast.acys_forecast
)
SELECT p.*, o.country, o.city, o.airport_name, d.country, d.city, d.airport_name,
       o.lat, o.lon, d.lat, d.lon
FROM panel p
LEFT JOIN airports o ON o.iata = p."IATA Origin"
-- destination: prefer the ACTUAL landing airport, fall back to the planned destination
LEFT JOIN airports d ON d.iata = coalesce(p."IATA Destination Actual", p."IATA Destination")
"""


def _log(msg: str) -> None:
    print(msg, flush=True)


async def merge_final(operator: str | None = None, registrations: list[str] | None = None,
                      *, truncate: bool = True) -> int:
    """Rebuild forecast.acys_summary from acys_actuals (flights only, optionally scoped to operator
    and/or registrations = UNION) + acys_forecast."""
    parts, args = [], []
    n = 0
    if operator:
        n += 1; parts.append(f'"Operator" = ${n}'); args.append(operator)
    if registrations:
        n += 1; parts.append(f'"Registration" = ANY(${n})'); args.append(list(registrations))
    final_scope = ("AND (" + " OR ".join(parts) + ")") if parts else ""

    async with DB(statement_timeout_ms=0) as db:
        if truncate:
            await db.execute("TRUNCATE forecast.acys_summary")
        sql = _merge_sql(final_scope)
        tag = await (db.conn.execute(sql, *args) if args else db.conn.execute(sql))
        inserted = int(tag.rsplit(" ", 1)[-1]) if tag else 0
    _log(f"[forecast.merge] acys_summary: {inserted} row(s) (actuals flights-only"
         f"{' scoped' if args else ''} + acys_forecast, airport-enriched)")
    return inserted


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="Merge acys_actuals + acys_forecast -> acys_summary.")
    p.add_argument("--operator", help="scope acys_actuals to this operator")
    p.add_argument("--registrations", help="scope acys_actuals to these comma-separated registrations")
    p.add_argument("--no-truncate", action="store_true", help="append instead of TRUNCATE+rebuild")
    return p.parse_args(argv)


def main(argv=None) -> None:
    a = _parse_args(argv)
    regs = [r.strip() for r in a.registrations.split(",") if r.strip()] if a.registrations else None
    asyncio.run(merge_final(operator=a.operator, registrations=regs, truncate=not a.no_truncate))


if __name__ == "__main__":
    main()

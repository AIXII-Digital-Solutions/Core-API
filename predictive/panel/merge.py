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
       "ICAO Origin","ICAO Destination","ICAO Destination Actual",
       "Operator","Master Series","Manufacturer","Aircraft Sub Series","Primary Usage",
       "Contract Year","Circle Distance","Flight Time",
       "Agreed Value","Total Seats","Total PAX","Actual Distance FR","Flight Time FR",
       "Delivery Date","Lease Type","Lease Dry Wet","Operational Lessor\""""

# acys_summary projection: same order as _COLS, but Agreed Value = 0 for a Wet lease, then Age
# (decimal years) appended. These two derivations are acys_summary-ONLY.
_PROJ = """p."Registration", p."Period", p."Date", p."Time Departed", p."Time Landed",
       p."IATA Origin", p."IATA Destination", p."IATA Destination Actual",
       p."ICAO Origin", p."ICAO Destination", p."ICAO Destination Actual",
       p."Operator", p."Master Series", p."Manufacturer", p."Aircraft Sub Series", p."Primary Usage",
       p."Contract Year", p."Circle Distance", p."Flight Time",
       CASE WHEN p."Lease Dry Wet" = 'Wet' THEN 0 ELSE p."Agreed Value" END,
       p."Total Seats", p."Total PAX", p."Actual Distance FR", p."Flight Time FR",
       p."Delivery Date", p."Lease Type", p."Lease Dry Wet", p."Operational Lessor",
       round((p."Date" - p."Delivery Date")::numeric / 365.25, 2)"""


# Airport lookup CHAIN for one airport: main.airports by IATA -> main.airports by ICAO ->
# flightradar.airports by IATA (pri orders the sources; LIMIT 1 = first that matched).
def _airport_lookup(iata_expr: str, icao_expr: str) -> str:
    return f"""(
        SELECT city, country, airport_name, lat, lon FROM (
            SELECT city, country, name AS airport_name, latitude AS lat, longitude AS lon, 1 AS pri
              FROM main.airports WHERE iata = {iata_expr}
            UNION ALL
            SELECT city, country, name, latitude, longitude, 2
              FROM main.airports WHERE icao = {icao_expr}
            UNION ALL
            SELECT city, country_name, name, lat, lon, 3
              FROM flightradar.airports WHERE iata = {iata_expr}
        ) s ORDER BY pri LIMIT 1
    )"""


def _merge_sql(final_scope: str) -> str:
    origin = _airport_lookup('p."IATA Origin"', 'p."ICAO Origin"')
    dest = _airport_lookup('coalesce(p."IATA Destination Actual", p."IATA Destination")',
                           'coalesce(p."ICAO Destination Actual", p."ICAO Destination")')
    return f"""
INSERT INTO forecast.acys_summary
    ({_COLS},"Age","Data Type",
     "Origin Country","Origin City","Origin Airport Name",
     "Destination Country","Destination City","Destination Airport Name",
     origin_lat, origin_lon, dest_lat, dest_lon)
WITH panel AS (
    -- tag each branch so acys_summary rows carry Data Type = Actuals / Forecast
    SELECT {_COLS}, 'Actuals' AS "Data Type" FROM forecast.acys_actuals WHERE "Date" IS NOT NULL {final_scope}
    UNION ALL
    SELECT {_COLS}, 'Forecast' AS "Data Type" FROM forecast.acys_forecast
)
SELECT {_PROJ}, p."Data Type", o.country, o.city, o.airport_name, d.country, d.city, d.airport_name,
       o.lat, o.lon, d.lat, d.lon
FROM panel p
LEFT JOIN LATERAL {origin} o ON true
LEFT JOIN LATERAL {dest} d ON true
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

"""Merge forecast.acys_actuals (FLIGHTS ONLY) + forecast.acys_forecast into forecast.acys_summary —
standalone harness mirror of the production path (external-worker). Prefer POST /forecast.

acys_summary is per-request: TRUNCATEd and rebuilt. Adds Age / origin+destination geography
(Country/City/Airport Name + lat/lon) / # Of Flights / Data Type, applies the Wet rule (Agreed
Value = 0), then GROUPS identical (aircraft, month, route) rows — summing Actual Distance FR,
Circle Distance, Flight Time FR, Flight Time and counting # Of Flights.
"""
from __future__ import annotations

import argparse
import asyncio

from predictive.db import DB
from predictive.panel._geo import ne, geo_lookup

# Columns carried verbatim from acys_actuals / acys_forecast into the merge panel.
_PANEL_COLS = """"Registration","Period","Date","Time Departed","Time Landed",
       "IATA Origin","IATA Destination","IATA Destination Actual",
       "ICAO Origin","ICAO Destination","ICAO Destination Actual",
       "Operator","Master Series","Manufacturer","Aircraft Sub Series","Primary Usage",
       "Contract Year","Circle Distance","Flight Time",
       "Agreed Value","Total Seats","Total PAX","Actual Distance FR","Flight Time FR",
       "Delivery Date","Lease Type","Lease Dry Wet","Operational Lessor\""""


def _merge_sql(final_scope: str) -> str:
    o_geo = geo_lookup(ne('p."IATA Origin"'), ne('p."ICAO Origin"'))
    dia, di = ne('p."IATA Destination Actual"'), ne('p."IATA Destination"')
    dica, dic = ne('p."ICAO Destination Actual"'), ne('p."ICAO Destination"')
    d_geo = geo_lookup(f"coalesce({dia}, {di})", f"coalesce({dica}, {dic})")
    return f"""
INSERT INTO forecast.acys_summary
    ("Registration","Period","Time Departed","Time Landed",
     "IATA Origin","IATA Destination","IATA Destination Actual",
     "ICAO Origin","ICAO Destination","ICAO Destination Actual",
     "Operator","Master Series","Manufacturer","Aircraft Sub Series","Primary Usage",
     "Contract Year","Circle Distance","Flight Time",
     "Agreed Value","Total Seats","Total PAX","Actual Distance FR","Flight Time FR",
     "Delivery Date","Lease Type","Lease Dry Wet","Operational Lessor",
     "Age","Data Type","# Of Flights",
     "Origin Country","Origin City","Origin Airport Name",
     "Destination Country","Destination City","Destination Airport Name",
     origin_lat, origin_lon, dest_lat, dest_lon)
WITH panel AS (
    SELECT {_PANEL_COLS}, 'Actuals' AS "Data Type" FROM forecast.acys_actuals
    WHERE "Date" IS NOT NULL {final_scope}
    UNION ALL
    SELECT {_PANEL_COLS}, 'Forecast' AS "Data Type" FROM forecast.acys_forecast
),
enriched AS (
    SELECT p.*,
           o.city AS o_city, o.country AS o_country, o.airport_name AS o_name, o.lat AS o_lat, o.lon AS o_lon,
           d.city AS d_city, d.country AS d_country, d.airport_name AS d_name, d.lat AS d_lat, d.lon AS d_lon
    FROM panel p
    LEFT JOIN LATERAL {o_geo} o ON true
    LEFT JOIN LATERAL {d_geo} d ON true
)
SELECT
    "Registration","Period",
    min("Time Departed"), max("Time Landed"),
    "IATA Origin","IATA Destination","IATA Destination Actual",
    "ICAO Origin","ICAO Destination","ICAO Destination Actual",
    "Operator","Master Series","Manufacturer","Aircraft Sub Series","Primary Usage",
    "Contract Year",
    sum("Circle Distance"),
    sum("Flight Time"),
    CASE WHEN "Lease Dry Wet" = 'Wet' THEN 0 ELSE "Agreed Value" END,
    "Total Seats","Total PAX",
    sum("Actual Distance FR"),
    sum("Flight Time FR"),
    "Delivery Date","Lease Type","Lease Dry Wet","Operational Lessor",
    round((min("Date") - "Delivery Date")::numeric / 365.25, 2),
    "Data Type",
    count(*),
    o_country, o_city, o_name,
    d_country, d_city, d_name,
    o_lat, o_lon, d_lat, d_lon
FROM enriched
GROUP BY
    "Registration","Period",
    "IATA Origin","IATA Destination","IATA Destination Actual",
    "ICAO Origin","ICAO Destination","ICAO Destination Actual",
    "Operator","Master Series","Manufacturer","Aircraft Sub Series","Primary Usage",
    "Contract Year",
    "Agreed Value","Total Seats","Total PAX",
    "Delivery Date","Lease Type","Lease Dry Wet","Operational Lessor",
    "Data Type",
    o_country, o_city, o_name, o_lat, o_lon,
    d_country, d_city, d_name, d_lat, d_lon
"""


def _log(msg: str) -> None:
    print(msg, flush=True)


async def merge_final(operator: str | None = None, registrations: list[str] | None = None,
                      *, truncate: bool = True) -> int:
    """Rebuild forecast.acys_summary from acys_actuals (flights only, optionally scoped to operator
    and/or registrations = UNION) + acys_forecast, grouped by (aircraft, month, route)."""
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
    _log(f"[forecast.merge] acys_summary: {inserted} grouped row(s) (actuals flights-only"
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

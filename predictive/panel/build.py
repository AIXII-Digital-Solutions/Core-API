"""Assemble forecast.acys_actuals — standalone harness mirror of the production path (external-worker
API/ForecastAPI/panel.py, which the POST /forecast endpoint drives). Prefer the endpoint; this CLI
writes the SAME shared forecast.* tables.

One row per FLIGHT: Cirium aircraft (latest revision per Registration+Period-month, Period >= 07-2022)
INNER-joined to its flightradar.flightsummary flight (matched by reg AND first_seen's month == Period
month). Circle Distance is the GREAT-CIRCLE (haversine, km) between origin & destination coordinates.
Modes: --operator and/or --registrations (UNION scope). acys_actuals ACCUMULATES (only THIS scope is
deleted+rebuilt).
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date

from predictive.db import DB
from predictive.panel._geo import ne, geo_lookup, great_circle

HISTORY_START = date(2022, 7, 1)   # Period >= 07-2022 (2.2) and flightsummary first_seen >= this
# Total PAX = Total Seats * this load factor. Env-tunable; default 0.8 (same var as the worker).
PAX_LOAD_FACTOR = float(os.getenv("FORECAST_PAX_LOAD_FACTOR", "0.8"))

# Contract Year: fiscal window (anchored at the request date's month/day = $3/$4) containing the
# flight's first_seen, labelled by its START year.
_CONTRACT_YEAR = """'CY' || (extract(year from a6.first_seen)::int - CASE
        WHEN extract(month from a6.first_seen)::int < $3
          OR (extract(month from a6.first_seen)::int = $3 AND extract(day from a6.first_seen)::int < $4)
        THEN 1 ELSE 0 END)::text"""

_FLIGHT_TIME_FR = "CASE WHEN a6.flight_time >= 0 THEN a6.flight_time * interval '1 second' ELSE NULL END"


def _assemble_sql(a5_where: str) -> str:
    # $1=start_date $2=as_of $3=anchor_month $4=anchor_day $5=pax_factor, then scope $6..
    o_geo = geo_lookup(ne("a6.orig_iata"), ne("a6.orig_icao"))
    d_geo = geo_lookup(f'coalesce({ne("a6.dest_iata_actual")}, {ne("a6.dest_iata")})',
                       f'coalesce({ne("a6.dest_icao_actual")}, {ne("a6.dest_icao")})')
    return f"""
INSERT INTO forecast.acys_actuals
    ("Registration","Period","Date","Time Departed","Time Landed",
     "IATA Origin","IATA Destination","IATA Destination Actual",
     "ICAO Origin","ICAO Destination","ICAO Destination Actual",
     "Operator","Master Series","Manufacturer","Aircraft Sub Series","Primary Usage",
     "Contract Year","Circle Distance","Flight Time",
     "Agreed Value","Total Seats","Total PAX","Actual Distance FR","Flight Time FR",
     "Delivery Date","Lease Type","Lease Dry Wet","Operational Lessor")
WITH array5 AS (
    SELECT DISTINCT ON (ca."Registration", to_date(r.period,'MM-YYYY'))
           ca."Registration" AS registration, r.period AS period,
           to_date(r.period,'MM-YYYY') AS period_month,
           ca."Operator" AS operator, ca."Master Series" AS master_series,
           ca."Manufacturer" AS manufacturer, ca."Aircraft Sub Series" AS sub_series,
           ca."Primary Usage" AS primary_usage,
           ca."Indicative Market Value (US$m)" AS agreed_value,
           ca."Number of Seats" AS total_seats,
           ca."Delivery Date" AS delivery_date, ca."Lease Type" AS lease_type,
           ca."Lease Dry / Wet" AS lease_dry_wet, ca."Operational Lessor" AS operational_lessor
    FROM cirium.ciriumaircrafts ca
    JOIN cirium.aircraftrevision r ON r.id = ca.revision_id
    WHERE {a5_where}
      AND to_date(r.period,'MM-YYYY') >= $1
    ORDER BY ca."Registration", to_date(r.period,'MM-YYYY'), ca.revision_id DESC
),
array6 AS (
    SELECT f.reg, f.first_seen, f.datetime_takeoff, f.datetime_landed,
           f.orig_iata, f.dest_iata, f.dest_iata_actual,
           f.orig_icao, f.dest_icao, f.dest_icao_actual,
           f.actual_distance, f.flight_time
    FROM flightradar.flightsummary f
    WHERE f.reg IN (SELECT registration FROM array5)
      -- lower bound is ALWAYS the start of CY2022 (anchor month/day in 2022) -> CY2021 excluded
      AND f.first_seen >= make_date(2022, $3, $4)
      AND f.first_seen <  $2
      -- DROP a flight with NO ICAO origin, or NO ICAO destination (neither actual nor planned)
      AND nullif(f.orig_icao, '') IS NOT NULL
      AND coalesce(nullif(f.dest_icao_actual, ''), nullif(f.dest_icao, '')) IS NOT NULL
)
SELECT a5.registration, a5.period, CAST(a6.first_seen AS date),
       a6.datetime_takeoff, a6.datetime_landed,
       a6.orig_iata, a6.dest_iata, a6.dest_iata_actual,
       a6.orig_icao, a6.dest_icao, a6.dest_icao_actual,
       a5.operator, a5.master_series, a5.manufacturer, a5.sub_series, a5.primary_usage,
       {_CONTRACT_YEAR}, {great_circle('o', 'd')}, (a6.datetime_landed - a6.datetime_takeoff),
       a5.agreed_value, a5.total_seats, a5.total_seats * CAST($5 AS double precision),
       a6.actual_distance, {_FLIGHT_TIME_FR},
       a5.delivery_date, a5.lease_type, a5.lease_dry_wet, a5.operational_lessor
FROM array5 a5
JOIN array6 a6
       ON a6.reg = a5.registration AND date_trunc('month', a6.first_seen) = a5.period_month
LEFT JOIN LATERAL {o_geo} o ON true
LEFT JOIN LATERAL {d_geo} d ON true
"""


def _log(msg: str) -> None:
    print(msg, flush=True)


async def build_history(operator: str | None = None, registrations: list[str] | None = None,
                        *, as_of: date | None = None) -> int:
    """Assemble forecast.acys_actuals for operator and/or registrations (UNION). acys_actuals
    accumulates (only this scope is deleted+rebuilt). Returns rows inserted."""
    if not operator and not registrations:
        raise ValueError("provide operator and/or registrations")
    as_of = as_of or date.today()

    # assemble params: $1=start $2=as_of $3=month $4=day $5=pax_factor, then scope $6..
    a5, dele, scope_args, del_args = [], [], [], []
    n, dn = 5, 0
    if operator:
        n += 1; a5.append(f'ca."Operator" = ${n}'); scope_args.append(operator)
        dn += 1; dele.append(f'"Operator" = ${dn}'); del_args.append(operator)
    if registrations:
        n += 1; a5.append(f'ca."Registration" = ANY(${n})'); scope_args.append(list(registrations))
        dn += 1; dele.append(f'"Registration" = ANY(${dn})'); del_args.append(list(registrations))
    a5_where = "(" + " OR ".join(a5) + ")"
    del_sql = "DELETE FROM forecast.acys_actuals WHERE (" + " OR ".join(dele) + ")"

    async with DB(statement_timeout_ms=0) as db:
        await db.conn.execute(del_sql, *del_args)                              # accumulate: this scope only
        tag = await db.conn.execute(_assemble_sql(a5_where),
                                    HISTORY_START, as_of, as_of.month, as_of.day, PAX_LOAD_FACTOR,
                                    *scope_args)
        inserted = int(tag.rsplit(" ", 1)[-1]) if tag else 0
    _log(f"[forecast.build] operator={operator} regs={registrations} as_of={as_of} -> acys_actuals +{inserted}")
    return inserted


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="Assemble forecast.acys_actuals (operator and/or registrations).")
    p.add_argument("--operator", help='Cirium "Operator" value, e.g. "Avianca"')
    p.add_argument("--registrations", help="comma-separated registration list")
    p.add_argument("--as-of", type=date.fromisoformat, default=None,
                   help="request date YYYY-MM-DD (Contract Year anchor + history end); default today")
    a = p.parse_args(argv)
    if not a.operator and not a.registrations:
        p.error("provide --operator and/or --registrations")
    return a


def main(argv=None) -> None:
    a = _parse_args(argv)
    regs = [r.strip() for r in a.registrations.split(",") if r.strip()] if a.registrations else None
    asyncio.run(build_history(operator=a.operator, registrations=regs, as_of=a.as_of))


if __name__ == "__main__":
    main()

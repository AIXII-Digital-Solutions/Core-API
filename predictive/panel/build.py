"""Assemble forecast.history_1 (steps 5-7) — standalone harness mirror of the production path
(external-worker API/ForecastAPI/panel.py, which the POST /forecast endpoint drives). Prefer the
endpoint; this CLI writes the SAME shared forecast.* tables.

Modes: by --operator OR by --registrations. history_1 ACCUMULATES (only THIS scope is deleted+rebuilt,
other operators/tails stay). See the endpoint/worker for the authoritative docstring.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date

from predictive.db import DB

HISTORY_START = date(2023, 7, 1)

# Contract Year: fiscal window (anchored at the request date's month/day = $3/$4) containing the
# flight date, labelled by its START year; NULL when there's no flight.
_CONTRACT_YEAR = """CASE WHEN a6.flight_dt IS NULL THEN NULL ELSE
    'CY' || (extract(year from a6.flight_dt)::int - CASE
        WHEN extract(month from a6.flight_dt)::int < $3
          OR (extract(month from a6.flight_dt)::int = $3 AND extract(day from a6.flight_dt)::int < $4)
        THEN 1 ELSE 0 END)::text
END"""


def _assemble_sql(a5_where: str) -> str:
    # $1=start_date, $2=as_of, $3=anchor_month, $4=anchor_day, $5=scope
    return f"""
INSERT INTO forecast.history_1
    ("Registration","Period","Date","Time Departed","Time Landed",
     "IATA Origin","IATA Destination","IATA Destination Actual",
     "Operator","Master Series","Manufacturer","Aircraft Sub Series","Primary Usage",
     "Contract Year","Circle Distance","Flight Time")
WITH array5 AS (
    SELECT DISTINCT ON (ca."Registration", to_date(r.period,'MM-YYYY'))
           ca."Registration" AS registration, r.period AS period,
           to_date(r.period,'MM-YYYY') AS period_month,
           ca."Operator" AS operator, ca."Master Series" AS master_series,
           ca."Manufacturer" AS manufacturer, ca."Aircraft Sub Series" AS sub_series,
           ca."Primary Usage" AS primary_usage
    FROM cirium.ciriumaircrafts ca
    JOIN cirium.aircraftrevision r ON r.id = ca.revision_id
    WHERE {a5_where}
      AND to_date(r.period,'MM-YYYY') >= $1
    ORDER BY ca."Registration", to_date(r.period,'MM-YYYY'), ca.revision_id DESC
),
array6 AS (
    SELECT f.reg, f.datetime_takeoff, f.datetime_landed,
           f.orig_iata, f.dest_iata, f.dest_iata_actual, f.circle_distance,
           coalesce(f.datetime_takeoff, f.first_seen) AS flight_dt
    FROM flightradar.flightsummary f
    WHERE f.reg IN (SELECT registration FROM array5)
      AND coalesce(f.datetime_takeoff, f.first_seen) >= $1
      AND coalesce(f.datetime_takeoff, f.first_seen) <  $2
)
SELECT a5.registration, a5.period, CAST(a6.flight_dt AS date),
       a6.datetime_takeoff, a6.datetime_landed,
       a6.orig_iata, a6.dest_iata, a6.dest_iata_actual,
       a5.operator, a5.master_series, a5.manufacturer, a5.sub_series, a5.primary_usage,
       {_CONTRACT_YEAR}, a6.circle_distance, (a6.datetime_landed - a6.datetime_takeoff)
FROM array5 a5
LEFT JOIN array6 a6
       ON a6.reg = a5.registration AND date_trunc('month', a6.flight_dt) = a5.period_month
"""


def _log(msg: str) -> None:
    print(msg, flush=True)


async def build_history(operator: str | None = None, registrations: list[str] | None = None,
                        *, as_of: date | None = None) -> int:
    """Assemble forecast.history_1 for operator and/or registrations (UNION). history_1 accumulates
    (only this scope is deleted+rebuilt). Returns rows inserted."""
    if not operator and not registrations:
        raise ValueError("provide operator and/or registrations")
    as_of = as_of or date.today()

    # assemble params: $1=start_date $2=as_of $3=anchor_month $4=anchor_day, then scope $5..
    a5, dele, scope_args, del_args = [], [], [], []
    n, dn = 4, 0
    if operator:
        n += 1; a5.append(f'ca."Operator" = ${n}'); scope_args.append(operator)
        dn += 1; dele.append(f'"Operator" = ${dn}'); del_args.append(operator)
    if registrations:
        n += 1; a5.append(f'ca."Registration" = ANY(${n})'); scope_args.append(list(registrations))
        dn += 1; dele.append(f'"Registration" = ANY(${dn})'); del_args.append(list(registrations))
    a5_where = "(" + " OR ".join(a5) + ")"
    del_sql = "DELETE FROM forecast.history_1 WHERE (" + " OR ".join(dele) + ")"

    async with DB(statement_timeout_ms=0) as db:
        await db.conn.execute(del_sql, *del_args)                              # accumulate: this scope only
        tag = await db.conn.execute(_assemble_sql(a5_where),
                                    HISTORY_START, as_of, as_of.month, as_of.day, *scope_args)
        inserted = int(tag.rsplit(" ", 1)[-1]) if tag else 0
    _log(f"[forecast.build] operator={operator} regs={registrations} as_of={as_of} -> history_1 +{inserted}")
    return inserted


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="Assemble forecast.history_1 (operator and/or registrations).")
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

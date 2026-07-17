"""Origin/Destination geo indexes on acys_summary_grouped, and removal of index dead weight.

ADD — the origin & destination family on the matview, so the report can filter/slice on geography without a
full scan. Evidence that this is worth it: over 19 days of live use (pg_stat_user_indexes)
ix_acys_grouped_citypairs took 34,429 scans — PowerBI genuinely filters server-side here, it is not a pure
import-mode model.

    "OD City&Country"            2,121 distinct   <- the combined origin&destination label
    "Origin/Destination City&Country"  ~340 each
    "Origin/Destination Country"        ~85 each
    "Origin/Destination City"          ~340 each
    "Origin/Destination Airport Name"  ~360 each
    "IATA Origin/Destination(+Actual)" ~350 each

Deliberately NOT indexed:
  * "Operator" / "Manufacturer" / "Primary Usage" — distinct = 1, an index can never help.
  * the ICAO trio — on flightradar.flightsummary the ICAO columns have 0 scans against 291,700 for IATA;
    nothing in this platform filters on ICAO. Trivial to add later if pg_stat ever shows demand.
"Contract Year" and "Age Group" already have ix_acys_grouped_cy / ix_acys_grouped_agegroup.

NOTE ON OWNERSHIP: `forecast_grouped_route_cols._ROUTE_INDEXES` is the SOURCE OF TRUTH for the matview's
indexes — DROP MATERIALIZED VIEW takes its indexes with it, so anything missing from that list vanishes on the
next rebuild of the view chain. The same set is listed there. This migration exists only to get them onto the
ALREADY-BUILT matview without forcing a full (and slow, CONCURRENTLY-index-rebuilding) chain re-apply. Keep
the two lists in sync.

DROP — indexes with 0 scans across the whole 19-day sample, i.e. pure write amplification:
  * ix_acys_actuals_cy        97 MB  — acys_actuals is DELETEd + re-INSERTed (~300k rows) every request and
  * ix_acys_forecast_cy      3.1 MB    nothing ever filters those row-level tables by Contract Year.
  * ix_acys_fc_coeff_operator 16 kB  — redundant: ix_acys_fc_coeff_op_sf already leads with "Operator".

All CONCURRENTLY (live report + a 3 GB table); CONCURRENTLY cannot run inside a transaction, hence the
autocommit block, and every statement is IF [NOT] EXISTS so a partial run is re-runnable.

Revision ID: forecast_geo_indexes
Revises: forecast_perf_indexes
Create Date: 2026-07-17
"""
from alembic import op

revision = "forecast_geo_indexes"
down_revision = "forecast_perf_indexes"
branch_labels = None
depends_on = None

_MV = "forecast.acys_summary_grouped"

# index name -> indexed column. MUST mirror forecast_grouped_route_cols._ROUTE_INDEXES (minus the two pair
# indexes, which that migration already created and which are live).
_GEO_INDEXES = {
    "ix_acys_grouped_od":            '"OD City&Country"',
    "ix_acys_grouped_o_citycountry": '"Origin City&Country"',
    "ix_acys_grouped_d_citycountry": '"Destination City&Country"',
    "ix_acys_grouped_o_country":     '"Origin Country"',
    "ix_acys_grouped_d_country":     '"Destination Country"',
    "ix_acys_grouped_o_city":        '"Origin City"',
    "ix_acys_grouped_d_city":        '"Destination City"',
    "ix_acys_grouped_o_airport":     '"Origin Airport Name"',
    "ix_acys_grouped_d_airport":     '"Destination Airport Name"',
    "ix_acys_grouped_iata_o":        '"IATA Origin"',
    "ix_acys_grouped_iata_d":        '"IATA Destination"',
    "ix_acys_grouped_iata_da":       '"IATA Destination Actual"',
}

# index name -> schema it lives in (0 scans over the 19-day sample)
_DEAD_INDEXES = {
    "ix_acys_actuals_cy": "forecast",
    "ix_acys_forecast_cy": "forecast",
    "ix_acys_fc_coeff_operator": "forecast",
}


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, col in _GEO_INDEXES.items():
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {_MV} ({col})")
        for name, schema in _DEAD_INDEXES.items():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema}.{name}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in _GEO_INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS forecast.{name}")
        # restore the dropped ones (they were useless, but downgrade must be faithful)
        op.execute('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_acys_actuals_cy '
                   'ON forecast.acys_actuals ("Contract Year")')
        op.execute('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_acys_forecast_cy '
                   'ON forecast.acys_forecast ("Contract Year")')
        op.execute('CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_acys_fc_coeff_operator '
                   'ON forecast.acys_forecast_coefficients ("Operator")')

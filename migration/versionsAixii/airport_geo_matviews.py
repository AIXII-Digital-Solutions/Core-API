"""flightradar.airport_geo_by_iata / _by_icao — the airport geography resolution, done ONCE.

ForecastAPI/panel.py `_geo_lookup` resolves one airport with a 4-way UNION + sort + array_agg, and it runs
PER ROW: twice (origin + destination) in _assemble_sql over ~301k flights AND twice in _merge_sql over ~566k
rows — ~1.7M resolutions of the same ~350 airports per request. Measured: 5.4 s of the 26.4 s assemble, plus
its share of the 27.3 s merge.

The resolution is a PER-FIELD priority pick — for each of city / country / airport_name / lat / lon take the
value from the lowest-priority source that has it non-empty:
    1. main.virtual_airport_list by IATA
    2. flightradar.airports        by IATA
    3. main.airports               by IATA
    4. main.airports               by ICAO
Priority 4 — the only ICAO-keyed source — is LAST. That is what makes the split exact: "first non-null over
1,2,3,4" is identical to "coalesce(first non-null over 1,2,3, the value from 4)". So the pick can be
precomputed per IATA (1-3) and per ICAO (4) and combined with a plain coalesce at read time, which is what
_geo_lookup now does — same output, two index probes instead of a 4-way UNION + sort + aggregate.

The aggregation below is copied verbatim from _geo_lookup (array_agg ORDER BY pri FILTER non-null, take [1]),
only GROUPED BY the key instead of filtered to one key, so a source row with several entries per key resolves
exactly as it did before.

Sources are reference data (~104k rows total: virtual_airport_list 18k, flightradar.airports 1k,
main.airports 85k) that changes rarely, so a MATVIEW is the right shape. panel.py REFRESHes them at the start
of the transform step — cheap, and it means the geography can never go stale behind an updated reference.

NB main.virtual_airport_list and main.airports are the legacy `main` schema, which is scheduled for a rewrite
(see CLAUDE.md — MainBase is intentionally not migrated yet). These matviews add no NEW coupling: _geo_lookup
already read exactly these tables. They will need rebuilding together with core/main.

Revision ID: airport_geo_matviews
Revises: powerbi_group_by_orig_dest
Create Date: 2026-07-17
"""
from alembic import op

revision = "airport_geo_matviews"
down_revision = "powerbi_group_by_orig_dest"
branch_labels = None
depends_on = None

# the per-field "lowest priority wins, per field" pick — identical to _geo_lookup's
_PICK = """
       (array_agg(city         ORDER BY pri) FILTER (WHERE city         IS NOT NULL))[1] AS city,
       (array_agg(country      ORDER BY pri) FILTER (WHERE country      IS NOT NULL))[1] AS country,
       (array_agg(airport_name ORDER BY pri) FILTER (WHERE airport_name IS NOT NULL))[1] AS airport_name,
       (array_agg(lat          ORDER BY pri) FILTER (WHERE lat          IS NOT NULL))[1] AS lat,
       (array_agg(lon          ORDER BY pri) FILTER (WHERE lon          IS NOT NULL))[1] AS lon
"""

_BY_IATA = f"""
CREATE MATERIALIZED VIEW flightradar.airport_geo_by_iata AS
SELECT iata,
{_PICK}
FROM (
    SELECT nullif("IATA Code",'') AS iata, nullif("City",'') AS city, nullif("Country",'') AS country,
           nullif("Airport Name",'') AS airport_name, "Latitude" AS lat, "Longitude" AS lon, 1 AS pri
      FROM main.virtual_airport_list
    UNION ALL
    SELECT nullif(iata,''), nullif(city,''), nullif(country_name,''), nullif(name,''), lat, lon, 2
      FROM flightradar.airports
    UNION ALL
    SELECT nullif(iata,''), nullif(city,''), nullif(country,''), nullif(name,''), latitude, longitude, 3
      FROM main.airports
) s
WHERE iata IS NOT NULL
GROUP BY iata
"""

_BY_ICAO = f"""
CREATE MATERIALIZED VIEW flightradar.airport_geo_by_icao AS
SELECT icao,
{_PICK}
FROM (
    SELECT nullif(icao,'') AS icao, nullif(city,'') AS city, nullif(country,'') AS country,
           nullif(name,'') AS airport_name, latitude AS lat, longitude AS lon, 4 AS pri
      FROM main.airports
) s
WHERE icao IS NOT NULL
GROUP BY icao
"""

# UNIQUE so the lookup is a single index probe — and so a future REFRESH ... CONCURRENTLY stays possible.
_INDEXES = [
    "CREATE UNIQUE INDEX ix_airport_geo_by_iata_key ON flightradar.airport_geo_by_iata (iata)",
    "CREATE UNIQUE INDEX ix_airport_geo_by_icao_key ON flightradar.airport_geo_by_icao (icao)",
]

_GRANTS = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['grp_aixii_read','grp_aviation_write'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('GRANT SELECT ON flightradar.airport_geo_by_iata, flightradar.airport_geo_by_icao TO %I', r);
    END IF;
  END LOOP;
END $$;
"""

# A REFRESH runs the matview's body as its OWNER, so the owner needs SELECT on every SOURCE — not just the
# refreshing role. main.airports and flightradar.airports were already readable by grp_aviation_write;
# main.virtual_airport_list was not (only svc_external_worker had a direct grant), so the first REFRESH from
# the worker died with "permission denied for table virtual_airport_list" even though the worker itself can
# read it. Granting the OWNER role is what matters here.
_SOURCE_GRANTS = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write')
     AND EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='main' AND tablename='virtual_airport_list') THEN
    EXECUTE 'GRANT USAGE ON SCHEMA main TO grp_aviation_write';
    EXECUTE 'GRANT SELECT ON main.virtual_airport_list TO grp_aviation_write';
  END IF;
END $$;
"""

# REFRESH MATERIALIZED VIEW requires OWNERSHIP — it is not a privilege GRANT can hand out. external-worker
# refreshes both at the start of its transform step, so they must be owned by a role it is a member of, or it
# dies with "permission denied for materialized view". Same reason forecast.acys_summary_grouped is handed to
# grp_aviation_write in forecast_grouped_route_cols. Guarded so a cluster without the role still migrates.
_OWNER = """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
    EXECUTE 'ALTER MATERIALIZED VIEW flightradar.airport_geo_by_iata OWNER TO grp_aviation_write';
    EXECUTE 'ALTER MATERIALIZED VIEW flightradar.airport_geo_by_icao OWNER TO grp_aviation_write';
  END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(_SOURCE_GRANTS)   # the owner must be able to read the sources BEFORE it ever refreshes
    op.execute("DROP MATERIALIZED VIEW IF EXISTS flightradar.airport_geo_by_iata")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS flightradar.airport_geo_by_icao")
    op.execute(_BY_IATA)
    op.execute(_BY_ICAO)
    for ix in _INDEXES:
        op.execute(ix)
    op.execute(_OWNER)
    op.execute(_GRANTS)


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS flightradar.airport_geo_by_iata")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS flightradar.airport_geo_by_icao")

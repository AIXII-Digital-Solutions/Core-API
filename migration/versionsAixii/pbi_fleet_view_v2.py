"""powerbi.last_seen_fleet, rebuilt on the split fleet

The view is now driven by the FLEET, not by whatever FR24 once returned: one row per aircraft in
cirium.asg_commercial + cirium.non_asg_insured_commercial, with the last known position joined onto
it. Two consequences worth knowing:

  * a tail that left the fleet disappears from the report (it used to linger forever, because
    flightradar.livepositions keeps every position it ever wrote);
  * a fleet aircraft FR24 has never reported is present, with empty position columns, instead of
    being silently absent.

Identity comes from the matviews themselves — `Airline` is the api.airlines name they matched,
`Operator` / `Master Series` / `Aircraft Sub Series` are that Cirium row's own values — so the report
and the fleet definition can never drift apart. ASG/Other is simply which matview the tail came from.

FLOWN PATH. The DAX walked back over all of a tail's positions and cut the last flight at the first
gap over an hour (`(ThisTs - PrevTs) > 1/24`), then joined `lat,lon,created_at` with `;`. Same rule
here, in one pass: a window function labels each gap, the last flight starts at the newest point whose
gap exceeds an hour (or at the very first point), and everything from there is aggregated. Two columns
come out of the single scan — with the timestamp and without it.

Revision ID: pbi_fleet_view_v2
Revises: asg_split_insured_fleet
Create Date: 2026-09-17
"""
from alembic import op

revision = "pbi_fleet_view_v2"
down_revision = "asg_split_insured_fleet"
branch_labels = None
depends_on = None

_STALE_HOURS = 18          # (NOW() - last seen) >= 0.75 day in the source DAX
_STATIONARY_HOURS = 2      # no fresh data for this long = the aircraft is not moving in our data
_FLIGHT_GAP_HOURS = 1      # a break longer than this starts a new flight (the DAX's 1/24 of a day)
_AIRBORNE_ALT_FT = 9000
_SLOW_KNOTS = 100
_REPORT_TZ = "Asia/Dubai"  # display only; every comparison below is on the raw timestamp

_REG_KEY = "upper(regexp_replace({col}, '[^A-Za-z0-9]', '', 'g'))"
# lat/lon the way the DAX formatted them: up to six decimals, no trailing zeros.
_COORD = "trim_scale(round({col}::numeric, 6))::text"

VIEW = f"""
CREATE VIEW powerbi.last_seen_fleet AS
WITH fleet AS (
    SELECT "Registration" AS reg, "Airline" AS airline_name, 'ASG'::text AS asg_other,
           "Operator" AS operator, "Master Series" AS master_series,
           "Aircraft Sub Series" AS sub_series, "Status" AS cirium_status
    FROM cirium.asg_commercial
    WHERE is_active AND "Registration" IS NOT NULL
    UNION ALL
    SELECT "Registration", "Airline", 'Other'::text, "Operator", "Master Series",
           "Aircraft Sub Series", "Status"
    FROM cirium.non_asg_insured_commercial
    WHERE is_active AND "Registration" IS NOT NULL
),
fleet_one AS (
    -- one row per tail: Cirium can carry the same airframe twice in a revision (a lease and its
    -- sub-lease), and the report is per aircraft
    SELECT DISTINCT ON ({_REG_KEY.format(col='reg')})
           {_REG_KEY.format(col='reg')} AS reg_key, reg, airline_name, asg_other, operator,
           master_series, sub_series, cirium_status
    FROM fleet
    ORDER BY 1, asg_other, operator NULLS LAST, reg
),
pos AS (
    SELECT {_REG_KEY.format(col='p.reg')} AS reg_key, p.reg AS seen_reg, p."timestamp" AS last_seen,
           p.lat, p.lon, p.alt, p.gspeed,
           nullif(p.orig_icao, '') AS orig_icao, nullif(p.orig_iata, '') AS orig_iata,
           nullif(p.dest_icao, '') AS dest_icao, nullif(p.dest_iata, '') AS dest_iata
    FROM flightradar.current_positions p
    WHERE p.reg IS NOT NULL
)
SELECT
    f.reg                                             AS "Registration",
    f.airline_name                                    AS "Airline Name",
    f.asg_other                                       AS "ASG/Other",
    f.operator                                        AS "Current Operator",
    f.master_series                                   AS "Aircraft Master Series",
    f.sub_series                                      AS "Aircraft Sub-Series",
    (p.last_seen AT TIME ZONE '{_REPORT_TZ}')         AS "Last Time Seen",
    CASE
        WHEN p.last_seen IS NULL THEN NULL
        WHEN p.gspeed < {_SLOW_KNOTS} AND coalesce(p.alt, 0) <= {_AIRBORNE_ALT_FT} THEN 'On the ground'
        WHEN p.last_seen < now() - interval '{_STALE_HOURS} hours' THEN 'On the ground'
        WHEN p.last_seen < now() - interval '{_STATIONARY_HOURS} hours'
             AND near.iata IS NOT DISTINCT FROM p.dest_iata THEN 'On the ground'
        ELSE 'Airborne'
    END                                               AS "Flight Status",
    p.lat                                             AS "Lat",
    p.lon                                             AS "Lon",
    near.iata                                         AS "Nearest Location Code",
    near.city_country                                 AS "Nearest Location City",
    p.gspeed                                          AS "Ground Speed, knots",
    p.alt                                             AS "Altitude, ft",
    o.lat                                             AS "Origin Lat",
    o.lon                                             AS "Origin Lon",
    p.orig_icao                                       AS "Origin Icao Code",
    CASE WHEN o.city IS NOT NULL AND o.country IS NOT NULL
         THEN o.city || ' (' || o.country || ')' END  AS "Origin City & Country",
    d.lat                                             AS "Destination Lat",
    d.lon                                             AS "Destination Lon",
    p.dest_icao                                       AS "Destination Icao Code",
    CASE WHEN d.city IS NOT NULL AND d.country IS NOT NULL
         THEN d.city || ' (' || d.country || ')' END  AS "Destination City & Country",
    path.with_time                                    AS "Flown Path",
    path.without_time                                 AS "Flown Path_Without_timestamp",
    path.points                                       AS "Flown Path Points",
    (p.last_seen < now() - interval '{_STATIONARY_HOURS} hours') AS "Stationary for more than 2h"
FROM fleet_one f
LEFT JOIN pos p ON p.reg_key = f.reg_key
-- nearest airport: the +/-2 degree box is only a cheap pre-filter; when it is empty (an ocean
-- crossing) the second branch scans the whole reference, so the answer is the true nearest either way
LEFT JOIN LATERAL (
    SELECT a.iata,
           CASE WHEN a.city IS NOT NULL AND a.country IS NOT NULL
                THEN a.city || ' (' || a.country || ')' END AS city_country
    FROM (
        SELECT g.iata, g.city, g.country, g.lat, g.lon
        FROM flightradar.airport_geo_by_iata g
        WHERE p.lat IS NOT NULL AND g.lat IS NOT NULL
          AND abs(g.lat - p.lat) <= 2 AND abs(g.lon - p.lon) <= 2
        UNION ALL
        SELECT g.iata, g.city, g.country, g.lat, g.lon
        FROM flightradar.airport_geo_by_iata g
        WHERE p.lat IS NOT NULL AND g.lat IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM flightradar.airport_geo_by_iata b
              WHERE b.lat IS NOT NULL AND abs(b.lat - p.lat) <= 2 AND abs(b.lon - p.lon) <= 2)
    ) a
    ORDER BY 2 * 6371 * asin(sqrt(
        power(sin(radians((a.lat - p.lat) / 2)), 2)
        + cos(radians(p.lat)) * cos(radians(a.lat)) * power(sin(radians((a.lon - p.lon) / 2)), 2)))
    LIMIT 1
) near ON true
-- endpoint geography: the per-field IATA-then-ICAO cascade the forecast uses (panel._geo_lookup)
LEFT JOIN LATERAL (
    SELECT coalesce(i.city, c.city) AS city, coalesce(i.country, c.country) AS country,
           coalesce(i.lat, c.lat) AS lat, coalesce(i.lon, c.lon) AS lon
    FROM (SELECT 1) _one
    LEFT JOIN flightradar.airport_geo_by_iata i ON i.iata = p.orig_iata
    LEFT JOIN flightradar.airport_geo_by_icao c ON c.icao = p.orig_icao
) o ON true
LEFT JOIN LATERAL (
    SELECT coalesce(i.city, c.city) AS city, coalesce(i.country, c.country) AS country,
           coalesce(i.lat, c.lat) AS lat, coalesce(i.lon, c.lon) AS lon
    FROM (SELECT 1) _one
    LEFT JOIN flightradar.airport_geo_by_iata i ON i.iata = p.dest_iata
    LEFT JOIN flightradar.airport_geo_by_icao c ON c.icao = p.dest_icao
) d ON true
-- the last flight, by the DAX's rule: cut at the newest break longer than an hour
LEFT JOIN LATERAL (
    WITH pts AS (
        SELECT l.created_at AS ts, l.lat, l.lon,
               l.created_at - lag(l.created_at) OVER (ORDER BY l.created_at) AS gap
        FROM flightradar.livepositions l
        WHERE l.reg = p.seen_reg AND l.lat IS NOT NULL AND l.lon IS NOT NULL
    ),
    flight_start AS (
        SELECT coalesce(max(ts), '-infinity'::timestamp) AS ts
        FROM pts WHERE gap IS NULL OR gap > interval '{_FLIGHT_GAP_HOURS} hour'
    )
    SELECT string_agg({_COORD.format(col='pts.lat')} || ',' || {_COORD.format(col='pts.lon')} || ','
                      || to_char(pts.ts AT TIME ZONE 'UTC' AT TIME ZONE '{_REPORT_TZ}',
                                 'YYYY-MM-DD HH24:MI:SS'), ';' ORDER BY pts.ts) AS with_time,
           string_agg({_COORD.format(col='pts.lat')} || ',' || {_COORD.format(col='pts.lon')},
                      ';' ORDER BY pts.ts) AS without_time,
           count(*)::int AS points
    FROM pts, flight_start WHERE pts.ts >= flight_start.ts
) path ON true
"""


def upgrade() -> None:
    # dropped, not replaced: CREATE OR REPLACE VIEW cannot add a column in the middle or rename one,
    # and this version inserts "Flown Path_Without_timestamp" next to "Flown Path"
    op.execute("DROP VIEW IF EXISTS powerbi.last_seen_fleet")
    op.execute(VIEW)
    op.execute("COMMENT ON VIEW powerbi.last_seen_fleet IS "
               "'The insured fleet (cirium.asg_commercial + cirium.non_asg_insured_commercial) with "
               "each tail''s last known position, enriched for the PowerBI map.'")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.last_seen_fleet")

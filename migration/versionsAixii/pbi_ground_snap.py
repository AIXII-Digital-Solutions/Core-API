"""powerbi.last_seen_fleet: an aircraft on the ground is drawn at its airport, not where FR24 lost it

An "On the ground" tail used to sit at its last raw FR24 position — often a point on the approach, or
mid-route for a tail whose data went stale in the air — with whatever speed that snapshot carried.
On a map that reads as an aircraft still flying. Now such a tail:

  * gets ground speed 0;
  * is drawn at an AIRPORT, offset inside a 100 m circle so several aircraft at one airport do not
    stack into a single dot;
  * gets that airport point appended to its Flown Path, stamped 30 minutes after its last real point,
    so the drawn track ends where the aircraft is shown.

WHICH AIRPORT. "On the ground" has two different causes, and they need different answers:
  * slow and low (under 100 kt and 9,000 ft) — FR24 is still seeing it on the ground, so we know where
    it is: the NEAREST airport. That is the destination after an arrival, but it is also the ORIGIN for
    an aircraft that has not left yet (a tail at FRA on a FRA->BUD rotation must not be drawn in BUD);
  * everything else (the data went stale, or it sat still near its destination) — we infer it landed
    at its DESTINATION; failing a known destination, the nearest airport.

THE OFFSET is derived from the registration (two slices of its md5 give an angle and a radius, the
radius square-rooted so points spread evenly over the disc). It is therefore stable: the dot does not
jump each time PowerBI refreshes, and two aircraft at one airport always land apart.

The view is written out in full here rather than patched again: the anchor needs the status computed
first, which is a different query shape from the previous revisions.

Revision ID: pbi_ground_snap
Revises: pbi_fleet_seen_only
Create Date: 2026-09-18
"""
from alembic import op

revision = "pbi_ground_snap"
down_revision = "pbi_fleet_seen_only"
branch_labels = None
depends_on = None

_STALE_HOURS = 18          # (NOW() - last seen) >= 0.75 day in the source DAX
_STATIONARY_HOURS = 2      # no fresh data for this long = not moving in our data
_FLIGHT_GAP_HOURS = 1      # a break longer than this starts a new flight (the DAX's 1/24 of a day)
_AIRBORNE_ALT_FT = 9000
_SLOW_KNOTS = 100
_GROUND_OFFSET_M = 100     # radius of the circle a grounded aircraft is drawn inside
_GROUND_TAIL_MIN = 30      # the appended airport point is stamped this long after the last real one
_REPORT_TZ = "Asia/Dubai"  # display only; every comparison is on the raw timestamp

_REG_KEY = "upper(regexp_replace({col}, '[^A-Za-z0-9]', '', 'g'))"
_COORD = "trim_scale(round(({col})::numeric, 6))::text"
_LOCAL = f"to_char(({{col}}) AT TIME ZONE 'UTC' AT TIME ZONE '{_REPORT_TZ}', 'YYYY-MM-DD HH24:MI:SS')"
# a number in [0, 1) from eight hex digits of the registration's md5
_UNIT = "(('x' || substr(md5(b.reg), {start}, 8))::bit(32)::bigint / 4294967296.0)"

VIEW = f"""
CREATE VIEW powerbi.last_seen_fleet AS
WITH fleet AS (
    SELECT "Registration" AS reg, "Airline" AS airline_name, 'ASG'::text AS asg_other,
           "Operator" AS operator, "Master Series" AS master_series, "Aircraft Sub Series" AS sub_series
    FROM cirium.asg_commercial
    WHERE is_active AND "Registration" IS NOT NULL
    UNION ALL
    SELECT "Registration", "Airline", 'Other'::text, "Operator", "Master Series", "Aircraft Sub Series"
    FROM cirium.non_asg_insured_commercial
    WHERE is_active AND "Registration" IS NOT NULL
),
fleet_one AS (
    -- one row per tail: Cirium can carry one airframe twice in a revision (a lease and its sub-lease)
    SELECT DISTINCT ON ({_REG_KEY.format(col='reg')})
           {_REG_KEY.format(col='reg')} AS reg_key, reg, airline_name, asg_other, operator,
           master_series, sub_series
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
),
base AS (
    SELECT f.reg,
           -- the matview's airline match, or the name typed next to a hand-listed registration
           coalesce(f.airline_name, manual.airline) AS airline_name,
           f.asg_other, f.operator, f.master_series, f.sub_series,
           p.last_seen, p.lat, p.lon, p.alt, p.gspeed,
           p.orig_icao, p.orig_iata, p.dest_icao, p.dest_iata,
           near.iata AS near_iata, near.city_country AS near_city, near.lat AS near_lat, near.lon AS near_lon,
           o.lat AS o_lat, o.lon AS o_lon, o.city AS o_city, o.country AS o_country,
           d.lat AS d_lat, d.lon AS d_lon, d.city AS d_city, d.country AS d_country,
           path.with_time, path.without_time, path.points, path.last_ts,
           (p.gspeed < {_SLOW_KNOTS} AND coalesce(p.alt, 0) <= {_AIRBORNE_ALT_FT}) AS slow_and_low
    FROM fleet_one f
    LEFT JOIN api.registration manual ON {_REG_KEY.format(col='manual.reg')} = f.reg_key
    -- only aircraft the live poll has seen at least once (livepositions starts with the poll)
    JOIN pos p ON p.reg_key = f.reg_key
    -- nearest airport: the +/-2 degree box is only a cheap pre-filter; when it is empty (an ocean
    -- crossing) the second branch scans the whole reference, so the answer is the true nearest
    LEFT JOIN LATERAL (
        SELECT a.iata, a.lat, a.lon,
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
                  SELECT 1 FROM flightradar.airport_geo_by_iata x
                  WHERE x.lat IS NOT NULL AND abs(x.lat - p.lat) <= 2 AND abs(x.lon - p.lon) <= 2)
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
    -- the last flight, by the original DAX rule: cut at the newest break longer than an hour
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
                          || {_LOCAL.format(col='pts.ts')}, ';' ORDER BY pts.ts) AS with_time,
               string_agg({_COORD.format(col='pts.lat')} || ',' || {_COORD.format(col='pts.lon')},
                          ';' ORDER BY pts.ts) AS without_time,
               count(*)::int AS points,
               max(pts.ts) AS last_ts
        FROM pts, flight_start WHERE pts.ts >= flight_start.ts
    ) path ON true
),
status AS (
    SELECT b.*,
           CASE
               WHEN b.last_seen IS NULL THEN NULL
               WHEN b.slow_and_low THEN 'On the ground'
               WHEN b.last_seen < now() - interval '{_STALE_HOURS} hours' THEN 'On the ground'
               WHEN b.last_seen < now() - interval '{_STATIONARY_HOURS} hours'
                    AND b.near_iata IS NOT DISTINCT FROM b.dest_iata THEN 'On the ground'
               ELSE 'Airborne'
           END AS flight_status,
           2 * pi() * {_UNIT.format(start=1)}                        AS offset_angle,
           {_GROUND_OFFSET_M} * sqrt({_UNIT.format(start=9)})        AS offset_m
    FROM base b
),
anchored AS (
    -- the airport a grounded aircraft is drawn at (see the module docstring for why two rules)
    SELECT s.*,
           CASE WHEN s.flight_status <> 'On the ground' THEN NULL
                WHEN s.slow_and_low THEN s.near_lat
                ELSE coalesce(s.d_lat, s.near_lat) END AS anchor_lat,
           CASE WHEN s.flight_status <> 'On the ground' THEN NULL
                WHEN s.slow_and_low THEN s.near_lon
                ELSE CASE WHEN s.d_lat IS NOT NULL THEN s.d_lon ELSE s.near_lon END END AS anchor_lon
    FROM status s
),
placed AS (
    SELECT a.*,
           a.anchor_lat + a.offset_m * cos(a.offset_angle) / 111320.0 AS ground_lat,
           a.anchor_lon + a.offset_m * sin(a.offset_angle) / (111320.0 * cos(radians(a.anchor_lat))) AS ground_lon
    FROM anchored a
)
SELECT
    reg                                               AS "Registration",
    airline_name                                      AS "Airline Name",
    asg_other                                         AS "ASG/Other",
    operator                                          AS "Current Operator",
    master_series                                     AS "Aircraft Master Series",
    sub_series                                        AS "Aircraft Sub-Series",
    (last_seen AT TIME ZONE '{_REPORT_TZ}')           AS "Last Time Seen",
    flight_status                                     AS "Flight Status",
    coalesce(ground_lat, lat)                         AS "Lat",
    coalesce(ground_lon, lon)                         AS "Lon",
    near_iata                                         AS "Nearest Location Code",
    near_city                                         AS "Nearest Location City",
    CASE WHEN flight_status = 'On the ground' THEN 0 ELSE gspeed END AS "Ground Speed, knots",
    alt                                               AS "Altitude, ft",
    o_lat                                             AS "Origin Lat",
    o_lon                                             AS "Origin Lon",
    orig_icao                                         AS "Origin Icao Code",
    CASE WHEN o_city IS NOT NULL AND o_country IS NOT NULL
         THEN o_city || ' (' || o_country || ')' END  AS "Origin City & Country",
    d_lat                                             AS "Destination Lat",
    d_lon                                             AS "Destination Lon",
    dest_icao                                         AS "Destination Icao Code",
    CASE WHEN d_city IS NOT NULL AND d_country IS NOT NULL
         THEN d_city || ' (' || d_country || ')' END  AS "Destination City & Country",
    -- a grounded aircraft's track ends at the airport point it is drawn at, 30 minutes after the
    -- last real position
    CASE WHEN ground_lat IS NOT NULL AND with_time IS NOT NULL
         THEN with_time || ';' || {_COORD.format(col='ground_lat')} || ',' || {_COORD.format(col='ground_lon')}
              || ',' || {_LOCAL.format(col=f"last_ts + interval '{_GROUND_TAIL_MIN} minutes'")}
         ELSE with_time END                           AS "Flown Path",
    CASE WHEN ground_lat IS NOT NULL AND without_time IS NOT NULL
         THEN without_time || ';' || {_COORD.format(col='ground_lat')} || ',' || {_COORD.format(col='ground_lon')}
         ELSE without_time END                        AS "Flown Path_Without_timestamp",
    points + CASE WHEN ground_lat IS NOT NULL AND with_time IS NOT NULL THEN 1 ELSE 0 END
                                                      AS "Flown Path Points",
    (last_seen < now() - interval '{_STATIONARY_HOURS} hours') AS "Stationary for more than 2h"
FROM placed
"""


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.last_seen_fleet")
    op.execute(VIEW)
    op.execute("COMMENT ON VIEW powerbi.last_seen_fleet IS "
               "'The insured fleet with each tail''s last known position for the PowerBI map. A grounded "
               "aircraft is drawn at its airport inside a 100 m circle, with speed 0.'")


def downgrade() -> None:
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location(
        "_pbi_fleet_seen_only", pathlib.Path(__file__).with_name("pbi_fleet_seen_only.py"))
    prev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prev)
    op.execute("DROP VIEW IF EXISTS powerbi.last_seen_fleet")
    op.execute(prev.VIEW)

"""powerbi.last_seen_fleet — where every tracked aircraft was last seen, ready for the report

One row per aircraft in flightradar.current_positions (FR24's last known position per tail). Everything
the PowerBI page needs is resolved here rather than in DAX: the airline, the Cirium identity, the nearest
airport, both endpoints' geography, the flown path and the flight status.

WHY IN SQL. The DAX versions recomputed a haversine over the airport table per row on every visual
refresh and re-derived the status from four measures. Here each is a single pass the database can plan:
the nearest airport is one LATERAL over ~1,000 airports, the geography is two index probes into the
airport_geo matviews the forecast already maintains, and the status is one CASE.

THE PIECES, and where each comes from:
  * Airline Name — api.airlines matched against the Cirium Operator / Manager / Owner, longest name
    first, exactly as the acys report matches it (a substring match, because Cirium writes
    "Air Arabia Abu Dhabi" where the reference holds "Air Arabia").
  * ASG/Other — the six named airlines are Other, everything else is ASG (the product's own list).
  * Current Operator / Master Series / Sub-Series — cirium.registrations, the latest Cirium row
    per registration (see the CTE for why not cirium.latest_revision).
  * Nearest Location — the closest airport by great-circle distance. The DAX pre-filtered to a +/-2
    degree box for speed; that box is kept as a CHEAP FILTER, not as the answer: a tail over an ocean
    with no airport inside it falls through to the whole table, so the answer is always the true nearest.
  * Origin / Destination geography — flightradar.airport_geo_by_iata coalesced onto airport_geo_by_icao,
    the same per-field priority cascade the forecast uses (see panel._geo_lookup).
  * Flown Path — the LAST flight's positions as `lat;lon,lat;lon`, oldest first, cut at any 2-hour
    gap so a reused fr24_id cannot glue two flights together. Kept for every tail however long
    ago it flew: this is the aircraft's last known track, not only a live one.
  * Stationary for more than 2h / Flight Status — see the CASE below.

THE 18-HOUR WINDOW. The source DAX read `(NOW() - [Last time seen]) >= 0.75` under a comment saying
"1 day = 24h". 0.75 of a day is EIGHTEEN hours, and the formula is what runs, so 18 is what is
implemented. Change _STALE_HOURS if 24 was the intent.

Revision ID: pbi_last_seen_fleet
Revises: acys_by_reg_key_operator
Create Date: 2026-09-17
"""
from alembic import op

revision = "pbi_last_seen_fleet"
down_revision = "acys_by_reg_key_operator"
branch_labels = None
depends_on = None

_STALE_HOURS = 18        # (NOW() - last seen) >= 0.75 day in the source DAX
_STATIONARY_HOURS = 2    # no fresh data for this long = the aircraft is not moving in our data
_AIRBORNE_ALT_FT = 9000  # below this, a slow aircraft is on the ground whatever else says
_SLOW_KNOTS = 100
_REPORT_TZ = 'Asia/Dubai'   # the timezone the report is read in; display only

_OTHER_AIRLINES = "'SCAT Airlines', 'Sundair', 'Trade Air', 'HelloJets', 'Levu AirCargo', 'FitsAir'"
_REG_KEY = "upper(regexp_replace({col}, '[^A-Za-z0-9]', '', 'g'))"

VIEW = f"""
CREATE OR REPLACE VIEW powerbi.last_seen_fleet AS
WITH pos AS (
    SELECT p.reg, p.fr24_id, p."timestamp" AS last_seen, p.lat, p.lon, p.alt, p.gspeed,
           nullif(p.orig_icao, '') AS orig_icao, nullif(p.orig_iata, '') AS orig_iata,
           nullif(p.dest_icao, '') AS dest_icao, nullif(p.dest_iata, '') AS dest_iata
    FROM flightradar.current_positions p
    WHERE p.reg IS NOT NULL
),
cirium_ident AS (
    -- The tail's identity from Cirium. NOT cirium.latest_revision: that view is the single NEWEST
    -- revision per plan type, so a tail absent from that one file has no operator at all — 43 of the
    -- 192 tracked tails, and with them their airline, because the airline is matched through the
    -- operator. cirium.registrations is the platform's own "latest row per registration ACROSS
    -- revisions", which covers every tail we track; the series come from that same row.
    SELECT r.registration_norm AS reg_key, r.operator, r.status,
           d."Manager" AS manager, d."Owner" AS owner,
           d."Master Series" AS master_series, d."Aircraft Sub Series" AS sub_series
    FROM cirium.registrations r
    LEFT JOIN LATERAL (
        SELECT c."Manager", c."Owner", c."Master Series", c."Aircraft Sub Series"
        FROM cirium.ciriumaircrafts c
        WHERE c."Registration" = r.registration
        -- newest revision first; inside one revision the sub-lease row names the operator that FLIES
        -- the aircraft, which is the one this report is about
        ORDER BY c.revision_id DESC, (c."Lease Type" = 'Sub Lease') DESC, c.id DESC
        LIMIT 1
    ) d ON true
)
SELECT
    p.reg                                             AS "Registration",
    air.airline_name                                  AS "Airline Name",
    CASE WHEN air.airline_name IN ({_OTHER_AIRLINES}) THEN 'Other' ELSE 'ASG' END AS "ASG/Other",
    cir.operator                                      AS "Current Operator",
    cir.master_series                                 AS "Aircraft Master Series",
    cir.sub_series                                    AS "Aircraft Sub-Series",
    -- Dubai local time, which is what the report is read in. Everything the CASE below compares is
    -- still the raw timestamptz, so the status does not depend on the display zone.
    (p.last_seen AT TIME ZONE '{_REPORT_TZ}')             AS "Last Time Seen",
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
    path.flown_path                                   AS "Flown Path",
    path.path_points                                  AS "Flown Path Points",
    (p.last_seen < now() - interval '{_STATIONARY_HOURS} hours') AS "Stationary for more than 2h"
FROM pos p
LEFT JOIN cirium_ident cir ON cir.reg_key = {_REG_KEY.format(col='p.reg')}
-- the airline reference, matched the way the acys report matches it: longest name first, so a short
-- name cannot shadow the longer one that actually appears in the Cirium string
LEFT JOIN LATERAL (
    SELECT al.airline_name
    FROM api.airlines al
    WHERE cir.operator ILIKE '%' || al.airline_name || '%'
       OR cir.manager  ILIKE '%' || al.airline_name || '%'
       OR cir.owner    ILIKE '%' || al.airline_name || '%'
    ORDER BY length(al.airline_name) DESC, al.airline_name
    LIMIT 1
) air ON true
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
LEFT JOIN LATERAL (
    -- The CURRENT flight's track. fr24_id alone is not enough: FR24 reuses an id, and 1,976 of them
    -- already carry points hours or days apart, which glued two separate flights into one path. So the
    -- track is the CONTIGUOUS run ending at the last known position — walking back while consecutive
    -- points are less than 2 hours apart, which is shorter than any turnaround.
    WITH pts AS (
        SELECT l."timestamp" AS ts, l.lat, l.lon,
               l."timestamp" - lag(l."timestamp") OVER (ORDER BY l."timestamp") AS gap
        FROM flightradar.livepositions l
        WHERE l.reg = p.reg AND l.fr24_id = p.fr24_id AND l."timestamp" <= p.last_seen
          AND l.lat IS NOT NULL AND l.lon IS NOT NULL
    ),
    run_start AS (
        SELECT coalesce(max(ts), '-infinity'::timestamptz) AS ts FROM pts WHERE gap > interval '2 hours'
    )
    SELECT string_agg(pts.lat || ';' || pts.lon, ',' ORDER BY pts.ts) AS flown_path,
           count(*)::int AS path_points
    FROM pts, run_start WHERE pts.ts >= run_start.ts
) path ON true
"""


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS powerbi")
    op.execute(VIEW)
    op.execute("COMMENT ON VIEW powerbi.last_seen_fleet IS "
               "'Last known position per tracked tail, enriched for the PowerBI fleet map. "
               "One row per aircraft in flightradar.current_positions.'")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS powerbi.last_seen_fleet")

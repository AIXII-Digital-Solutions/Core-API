-- af_base: precondition-filtered, registration-matched raw flight base (brief sec. 7.2).
-- One row per FR24 completed flight that survives the three preconditions:
--   (i)  in-service only      -> tail's current Cirium Status = 'In Service'
--   (ii) operator-segment     -> carrier = tail's current Cirium Operator, and the
--                                flight is clipped to >= "Operator Delivery Date"
--                                (tenure start). NOTE: Cirium has no historical
--                                snapshots before 2025-11, so date-accurate operator
--                                history is approximated by the current operator's
--                                delivery date. Tails with no delivery date keep their
--                                whole window (signed upward bias).
--  (iii) RAW flights          -> NO hygiene (ferries/positioning kept, bad flight_time
--                                kept; it is not used by S1-S4). Only null/empty reg
--                                dropped. No dedup (signed: possible FR24 seam doubles).
-- Sub-fleet bands: type from FR24 (clean ICAO codes), seats from Cirium, range derived
-- from the tail's median great-circle haul (Cirium stores no range/cruise -> signed).
-- Both a fine and a coarse cell key are stored so the diagnostic can be re-run at two
-- granularities without rebuilding this (the expensive ~7M-row) matview.

DROP MATERIALIZED VIEW IF EXISTS api.af_base CASCADE;

CREATE MATERIALIZED VIEW api.af_base AS
WITH cur AS (   -- latest Cirium revision per registration (the "snapshot")
    SELECT DISTINCT ON (c."Registration")
        c."Registration"           AS reg,
        c."Operator"               AS op_name,
        NULLIF(btrim(c."Operator ICAO"), '') AS op_icao,
        c."Operator Delivery Date" AS op_deliv,
        c."Status"                 AS status,
        c."Number of Seats"        AS seats
    FROM cirium.ciriumaircrafts c
    WHERE c."Registration" IS NOT NULL AND btrim(c."Registration") <> ''
    ORDER BY c."Registration", c.revision_id DESC
),
flt AS (   -- matched + preconditioned raw flights
    SELECT
        f.reg,
        NULLIF(btrim(f.flight), '')    AS flight,
        NULLIF(btrim(f.orig_icao), '') AS orig_icao,
        NULLIF(btrim(f.dest_icao), '') AS dest_icao,
        (f.datetime_takeoff AT TIME ZONE 'UTC')          AS takeoff_utc,
        (f.datetime_takeoff AT TIME ZONE 'UTC')::date     AS takeoff_date,
        f.circle_distance,
        f.flight_time,
        f.type                         AS ac_type,
        cur.op_name, cur.op_icao, cur.seats
    FROM flightradar.flightsummary f
    JOIN cur ON cur.reg = f.reg
    WHERE f.reg IS NOT NULL AND btrim(f.reg) <> ''
      AND f.datetime_takeoff IS NOT NULL
      AND cur.status = 'In Service'                                   -- (i)
      AND (cur.op_deliv IS NULL                                       -- (ii)
           OR (f.datetime_takeoff AT TIME ZONE 'UTC')::date >= cur.op_deliv)
),
tail_haul AS (   -- per-tail median great-circle haul -> range banding
    SELECT reg, percentile_cont(0.5) WITHIN GROUP (ORDER BY circle_distance) AS med_haul
    FROM flt
    WHERE circle_distance IS NOT NULL AND circle_distance > 0
    GROUP BY reg
),
banded AS (
    SELECT
        flt.*,
        COALESCE(flt.op_icao, upper(btrim(flt.op_name))) AS carrier_key,
        COALESCE(flt.ac_type, 'NA')                      AS type_key,
        CASE
            WHEN flt.seats IS NULL THEN 'NA'
            WHEN flt.seats <  20 THEN 'lt20'
            WHEN flt.seats < 100 THEN '20-99'
            WHEN flt.seats < 180 THEN '100-179'
            WHEN flt.seats < 240 THEN '180-239'
            ELSE 'ge240'
        END AS seats_band,
        CASE
            WHEN th.med_haul IS NULL THEN 'NA'
            WHEN th.med_haul <  800 THEN 'regional'
            WHEN th.med_haul < 2500 THEN 'short'
            WHEN th.med_haul < 5000 THEN 'medium'
            ELSE 'long'
        END AS range_band,
        extract(isodow FROM flt.takeoff_utc)::int        AS isodow,
        to_char(flt.takeoff_utc, 'IYYY-IW')              AS iso_yearweek
    FROM flt
    LEFT JOIN tail_haul th ON th.reg = flt.reg
)
SELECT
    b.reg, b.flight, b.orig_icao, b.dest_icao, b.takeoff_date, b.takeoff_utc,
    b.circle_distance, b.flight_time, b.isodow, b.iso_yearweek,
    b.carrier_key,
    b.op_name AS carrier_name,
    b.type_key AS ac_type,
    b.seats_band, b.range_band,
    b.carrier_key || '::' || b.type_key || '|' || b.seats_band || '|' || b.range_band AS cell_fine,
    b.carrier_key || '::' || b.type_key                                               AS cell_coarse
FROM banded b;

CREATE INDEX ix_af_base_cell_fine   ON api.af_base (cell_fine);
CREATE INDEX ix_af_base_cell_coarse ON api.af_base (cell_coarse);
CREATE INDEX ix_af_base_reg         ON api.af_base (reg);

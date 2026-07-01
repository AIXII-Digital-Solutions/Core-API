-- af_cells_{suffix}: the cell dimension (one row per cell at the {cell} granularity).
-- carrier_key / ac_type are constant within a cell (the key includes them); for the
-- coarse granularity seats_band/range_band vary and are reported as a representative max().
DROP MATERIALIZED VIEW IF EXISTS api.af_cells_{suffix} CASCADE;

CREATE MATERIALIZED VIEW api.af_cells_{suffix} AS
SELECT
    {cell}                       AS cell,
    max(carrier_key)             AS carrier_key,
    max(carrier_name)            AS carrier_name,
    max(ac_type)                 AS ac_type,
    max(seats_band)              AS seats_band,
    max(range_band)              AS range_band,
    count(*)                     AS n_flights,
    count(DISTINCT reg)          AS n_tails,
    count(DISTINCT takeoff_date) AS n_active_days,
    count(*) FILTER (WHERE flight IS NOT NULL) AS n_flights_with_number,
    min(takeoff_date)            AS first_date,
    max(takeoff_date)            AS last_date
FROM api.af_base
GROUP BY {cell};

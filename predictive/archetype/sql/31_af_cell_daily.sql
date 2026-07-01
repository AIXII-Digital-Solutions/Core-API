-- af_cell_daily_{suffix}: daily departures per cell (feeds S2 in Python).
-- Only non-zero days are stored; the S2 computation reindexes to the full date span
-- and zero-fills before MA28 detrend + day-of-week regression.
DROP MATERIALIZED VIEW IF EXISTS api.af_cell_daily_{suffix} CASCADE;

CREATE MATERIALIZED VIEW api.af_cell_daily_{suffix} AS
SELECT
    {cell}        AS cell,
    takeoff_date,
    max(isodow)   AS isodow,           -- constant per date
    count(*)      AS departures
FROM api.af_base
GROUP BY {cell}, takeoff_date;

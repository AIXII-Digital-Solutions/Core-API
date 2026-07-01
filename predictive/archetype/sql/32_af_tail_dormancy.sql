-- af_tail_dormancy: per-tail active vs in-service span (feeds S3 in Python).
-- A tail belongs to exactly one cell at each granularity (carrier+type+bands fixed),
-- so both cell keys are carried and S3 (median over tails of active/in-service) is
-- computed per granularity in Python.
--   active_days   = distinct calendar days with >= 1 departure (A_a)
--   first/last_active = the tail's observed flight span
--   op_deliv      = "Operator Delivery Date" (tenure start) to clip in-service start
-- D_a (in-service days) is reconstructed in Python as
--   max(cell_window_start, op_deliv) .. cell_window_end
-- where cell_window is the cell's observed span -> neutralises late ADS-B coverage of a
-- carrier while still capturing within-cell dormancy. Signed: historical storage gaps
-- before 2025-11 are invisible, so D_a may be slightly over-counted (S3 slightly low).
DROP MATERIALIZED VIEW IF EXISTS api.af_tail_dormancy CASCADE;

CREATE MATERIALIZED VIEW api.af_tail_dormancy AS
WITH tail AS (
    SELECT
        cell_fine, cell_coarse, carrier_key, reg,
        count(DISTINCT takeoff_date) AS active_days,
        min(takeoff_date)            AS first_active,
        max(takeoff_date)            AS last_active
    FROM api.af_base
    GROUP BY cell_fine, cell_coarse, carrier_key, reg
),
deliv AS (
    SELECT DISTINCT ON (c."Registration")
        c."Registration"           AS reg,
        c."Operator Delivery Date" AS op_deliv
    FROM cirium.ciriumaircrafts c
    WHERE c."Registration" IS NOT NULL AND btrim(c."Registration") <> ''
    ORDER BY c."Registration", c.revision_id DESC
)
SELECT t.*, d.op_deliv
FROM tail t
LEFT JOIN deliv d ON d.reg = t.reg;

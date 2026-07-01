-- af_s1_{suffix}: S1 flight recurrence (brief sec. 7.3).
-- Key k = (flight number, orig, dest). W_k = number of distinct ISO weeks k flew.
-- S1 = share of flights whose key flew in >= 4 distinct weeks. LCC ~ 1, ad-hoc ~ 0.
-- Flights with no flight number cannot form a key -> excluded from the ratio; the
-- excluded share is reported (s1_den_flights vs cell n_flights) because FR24 flight-
-- number quality is the documented S1 trap -> cross-check with S2/S4.
DROP MATERIALIZED VIEW IF EXISTS api.af_s1_{suffix} CASCADE;

CREATE MATERIALIZED VIEW api.af_s1_{suffix} AS
WITH keyagg AS (
    SELECT {cell} AS cell, flight, orig_icao, dest_icao,
           count(*)                      AS n_flights,
           count(DISTINCT iso_yearweek)  AS w_k
    FROM api.af_base
    WHERE flight IS NOT NULL AND orig_icao IS NOT NULL AND dest_icao IS NOT NULL
    GROUP BY {cell}, flight, orig_icao, dest_icao
)
SELECT
    cell,
    (sum(n_flights) FILTER (WHERE w_k >= 4))::double precision
        / NULLIF(sum(n_flights), 0)        AS s1,
    sum(n_flights)                          AS s1_den_flights,
    sum(n_flights) FILTER (WHERE w_k >= 4)  AS s1_num_flights,
    count(*)                                AS n_keys
FROM keyagg
GROUP BY cell;

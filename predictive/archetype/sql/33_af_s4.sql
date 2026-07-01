-- af_s4_{suffix}: S4 network dispersion (brief sec. 7.3).
-- Undirected city-pair p = {orig,dest}; n_p = flights on p. Singleton share
-- sigma = |{p: n_p=1}| / |P_c|; S4 = 1 - sigma (higher = denser, more repeated routes).
-- Secondary: normalized entropy H / ln|P_c| reported alongside.
-- Signed trap: short presence (ACMI) mechanically inflates singletons -> spuriously
-- on-demand; this widens within-cell spread, not a real signal.
DROP MATERIALIZED VIEW IF EXISTS api.af_s4_{suffix} CASCADE;

CREATE MATERIALIZED VIEW api.af_s4_{suffix} AS
WITH pairs AS (
    SELECT {cell} AS cell,
           least(orig_icao, dest_icao)    AS a,
           greatest(orig_icao, dest_icao) AS b,
           count(*) AS n_p
    FROM api.af_base
    WHERE orig_icao IS NOT NULL AND dest_icao IS NOT NULL
      AND orig_icao <> dest_icao
    GROUP BY {cell}, least(orig_icao, dest_icao), greatest(orig_icao, dest_icao)
),
tot AS (
    SELECT cell,
           sum(n_p)                         AS tot_flights,
           count(*)                         AS n_pairs,
           count(*) FILTER (WHERE n_p = 1)  AS n_singletons
    FROM pairs
    GROUP BY cell
),
ent AS (
    SELECT p.cell,
           -sum( (p.n_p::double precision / t.tot_flights)
                 * ln(p.n_p::double precision / t.tot_flights) ) AS h
    FROM pairs p JOIN tot t USING (cell)
    GROUP BY p.cell
)
SELECT
    t.cell,
    t.n_pairs,
    t.n_singletons,
    t.tot_flights AS n_flights_paired,
    1.0 - t.n_singletons::double precision / NULLIF(t.n_pairs, 0) AS s4,
    CASE WHEN t.n_pairs > 1 THEN e.h / ln(t.n_pairs) ELSE 0 END   AS s4_entropy_norm
FROM tot t JOIN ent e USING (cell);

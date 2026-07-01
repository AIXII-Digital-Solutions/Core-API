# Archetype layer — distribution-shape verdict

> Brief sec. 7-8. This single output configures the Anchor (step 2) and Pooling (step 3). It is a diagnostic on the ~8M-flight FR24 panel, not a production service.

## Panel

- base flights kept (matched, in-service, operator-clipped, raw): **6,143,719**
- tails: **3194** (no operator-delivery date: 327)
- cells (fine = carrier x type x seats x range): **623** (431 complete-case, used in PCA)
- operator-delivery clip removed 609,117 / 6,755,840 matched in-service flights (= 0.0902)
- departures/day quantiles across cells: {0.1: 1.0, 0.25: 1.33, 0.5: 2.24, 0.75: 7.95, 0.9: 25.92}
- regular-cargo reference present: ['Aerotranscargo', 'Allied Air Cargo', 'Avianca Cargo', 'European Air Transport', 'Express Air Cargo', 'FedEx', 'Kalitta Air', 'LATAM Cargo Chile']

## PCA (unsupervised first look)

- explained variance: PC1 = **60.7%**, PC2 = **21.7%**, PC3 = 12.5%, PC4 = 5.1%
- PC1 loadings (expect all positive = regularity axis): s1=+0.59, s2=+0.32, s3=+0.55, s4=+0.50
- PC2 loadings: s1=-0.06, s2=+0.92, s3=-0.15, s4=-0.35

## Shape of PC1

- KDE prominent modes: **2** at PC1 ~ [-1.83, 1.23]
- bimodality coefficient: **0.681** (> 0.555 hints multimodal; uniform = 0.33; the figure is the arbiter)

## Reference-label validation (PC1 ordering)

| category | expected rank | mean PC1 | n cells | mean S1/S2/S3/S4 |
|---|---|---|---|---|
| regional | 0.9 | +1.20 | 9 | 0.869/0.269/0.688/0.779 |
| fsc | 1.0 | +1.17 | 140 | 0.904/0.118/0.784/0.845 |
| lcc | 0.95 | +0.94 | 30 | 0.854/0.052/0.806/0.821 |
| charter_acmi | 0.25 | +0.86 | 3 | 0.891/0.059/0.728/0.807 |
| cargo_regular | 0.85 | -0.29 | 1 | 0.949/0.0/0.027/0.778 |
| business_fractional | 0.15 | -1.64 | 11 | 0.033/0.034/0.523/0.268 |

- Spearman(expected regularity rank, observed mean PC1) = **0.77** (near +1 ⇒ PC1 really is the schedule↔on-demand axis)

## PC2 — what the second axis actually is (brief sec. 7.6)

- PC2 is dominated by **s2** (loading +0.92); the others are s1=-0.06, s3=-0.15, s4=-0.35.
- Interpretation: with s2=S2 driving it, PC2 is a *weekly-cadence shape* axis (strong day-of-week rhythm high vs daily-uniform low) **within** the regular cluster — NOT the fractional intensity-vs-routes dissociation the brief hypothesised.
- fractional vs regular separation: on PC1 = **2.77**, on PC2 = **0.40** ⇒ fractional/on-demand resolves on **PC1** (the litmus lands on PC1 here, not a second axis).
- VistaJet anchor: PC1=-1.19, PC2=+0.40, S1=0.02 S3=0.66 (high-ish S3, ~0 S1/S4 ⇒ sits at the on-demand end of PC1, only mildly off on PC2).

## Mode composition & low-confidence check (brief sec. 7.8)

- all cells: 2 KDE modes, BC=0.681 (431 cells).
- high-confidence cells only: 2 KDE modes, BC=0.716 (296 cells). Bimodality SURVIVES the exposure filter ⇒ the on-demand mode is real, not small-cell spread.
- valley at PC1≈-0.30: left/on-demand mode = 167 cells (70% low-confidence), right/schedule mode = 264 cells (7% low-confidence).

## Analyst notes — anomalies raised

- **ACMI looks regular** (charter_acmi mean PC1 = +0.86, n=3): long-term ACMI/wet-lease operators (e.g. Avion Express) fly *other carriers'* schedules, so behaviourally they read as schedule-regular. This corroborates the brief's premise that axis-1 (cargo/ACMI/VIP) is NOT separable from behaviour and must come from Cirium — it is not a defect of the signatures.
- **Cargo under-represented in complete-case**: only 1 of 4 cargo cells survived to the PCA (rest low-confidence / short series), so the scheduled-cargo corner is not yet validated; treat its position as provisional.
- **S2 saturates on daily operations** (why it split onto PC2): a truly regular sub-fleet flying the same banks 7 days/week has LOW day-of-week R² (every weekday looks alike), so 'higher S2 = more regular' is non-monotone for high-frequency carriers (lcc mean S2 = 0.05 < fsc 0.12 < regional 0.27). S2 measures weekly *shape*, not regularity per se — step 2 should consume PC2/S2 as the weekly-seasonality term, not as a second regularity score.

## Robustness (coarse granularity carrier x type)

- coarse KDE modes: 2; bimodality coeff = 0.653; cells = 355. Shape verdict should agree with fine.

## Read

- **PC1 shape:** DISCRETE modes (schedule vs on-demand) — 2 KDE modes, BC=0.681; survives high-confidence filter (2 modes).
- **PC2:** real (21.7% variance) but it is a **s2-driven weekly-cadence axis** (rhythmic vs daily-uniform) inside the regular cluster — NOT a separate on-demand dimension. The schedule↔on-demand split is on PC1.
- **Downstream (anchor, step 2):** treat PC1 as discrete buckets at the antimode (~the valley) — bucket-specific anchor tightness; the on-demand bucket needs its own (low-recurrence) intensity model.
- **Downstream (pooling, step 3):** pool within bucket; carry PC2 as a second pooling coordinate for the seasonal/weekly decomposition (it is the weekly-cadence shape, which the anchor's seasonality term consumes).

> This read is threshold-computed; the KDE/scatter PNGs are the primary evidence (brief sec. 7.5). Confirm visually before locking the design.

## Signed limitations

1. Cirium has no historical snapshots before 2025-11 → operator/status on a historical date is approximated by the current operator + delivery-date clip; 327 tails have no delivery date (kept whole window, upward bias).
2. ACMI inseparable → short presence inflates S4 singletons (e.g. Avion Express); on-demand tilt is partly an artifact, signed not signal.
3. Range/cruise not in Cirium → range band derived from observed median haul (sub-fleet proxy).
4. Raw flights, no dedup (brief sec. 7.2 iii) → possible FR24 seam doubles inflate counts.
5. Coherence ≠ accuracy (brief principle); the truth test is LOCO coverage at step 4.
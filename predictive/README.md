# predictive/ — airline activity forecasting engine (calibration & diagnostics)

Standalone implementation of the engine described in
`docs/airline forecast handoff prompt.md`. Build order is **Archetype → Anchor →
Pooling → Validation**; only the **Archetype** layer (brief §7–8) is built here.

This is the *calibration harness*: it reads the platform DB directly and answers the
one question that configures everything downstream — **are operational archetypes
discrete modes or a continuum, and is a second axis (PC2) needed?** The production
compute eventually belongs in `external-worker` (the consumer of the
`predictive_utilisation` ARQ job); this package is where the design is decided on data.

## What it does (archetype layer)

1. **Panel** — builds `api.af_base`: every FR24 completed flight (`flightradar.flightsummary`)
   matched to its Cirium fleet row by **registration**, with the three preconditions
   (brief §7.2): in-service tails only, clipped to the current operator's tenure
   (`"Operator Delivery Date"`), **raw** flights (no hygiene, no dedup).
2. **Cell** = `carrier × sub-fleet` (`carrier × type × seats-band × range-band`), plus a
   coarse `carrier × type` granularity for a robustness check.
3. **Signatures S1–S4** (all oriented so higher = more schedule-regular):
   - **S1** recurrence — share of flights whose `(flight, orig, dest)` key flew in ≥4 ISO weeks.
   - **S2** weekly periodicity — adjusted R² of day-of-week dummies on MA28-detrended daily departures.
   - **S3** dormancy — median over tails of `active_days / in-service_days`.
   - **S4** dispersion — `1 − singleton-pair fraction` (plus normalized entropy).
4. **Score** — orient → z-score → PCA (numpy SVD) → **PC1** (regularity axis) + **PC2**.
5. **Diagnostic** — `output/pc1_kde_fine.png` (KDE of PC1) and `output/pc1_pc2_scatter.png`
   (2D scatter, marker size ∝ √flights, colored by reference archetype), plus
   `output/archetype_verdict.md` — the shape verdict.

## Run

```bash
# from repo root, with the venv that has pandas+matplotlib:
G:/Projects/Core-API/.venv/Scripts/python.exe -m predictive.archetype.diagnose
# reuse already-built matviews (iterate on the Python only):
G:/Projects/Core-API/.venv/Scripts/python.exe -m predictive.archetype.diagnose --no-rebuild
```

Credentials come from the repo-root `.env` (prod `aixii`, where the ~8M panel lives).
The run creates `api.af_*` materialized views on that DB (the `api` schema is the
sanctioned scratch space for this work) and writes all artifacts to `predictive/output/`.

`pip install -r predictive/requirements-ds.txt` if the venv lacks pandas/matplotlib.

## Signed limitations (carried into the verdict)

- Cirium has **no historical snapshots before 2025-11** → operator/status on a historical
  date is approximated by the *current* operator + delivery-date clip (some tails lack the
  date and keep their whole window → upward bias).
- ACMI is inseparable on this data → short presence inflates S4 singletons (signed, not signal).
- Range/cruise aren't stored in Cirium → range band is derived from observed median haul.
- Raw flights, no dedup → possible FR24 coverage-seam doubles.
- **Coherence ≠ accuracy** — the truth test is LOCO coverage at the validation step.

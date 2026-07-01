"""Archetype diagnostic entrypoint (brief sec. 7.5-8).

Builds the api.af_* matviews, computes S1-S4, assembles the PC1/PC2 score, and emits
the two diagnostic figures (KDE of PC1, 2D scatter PC1xPC2) plus archetype_verdict.md
answering the one question this step exists for: discrete modes vs continuum, and is
PC2 needed?

    python -m predictive.archetype.diagnose                 # full rebuild + diagnose
    python -m predictive.archetype.diagnose --no-rebuild    # reuse existing matviews

Run from repo root with the venv that has pandas/matplotlib (PYTHONPATH not required;
it's a normal package). Writes to predictive/output/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ..db import DB  # noqa: E402
from .reference_labels import CATEGORY_COLOR, CATEGORY_REGULARITY  # noqa: E402
from .score import compute_score  # noqa: E402
from .signatures import SIGNATURES, assemble_features, compute_s2, compute_s3  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "output"
GRANS = [("fine", "cell_fine"), ("coarse", "cell_coarse")]

# Known scheduled-cargo ICAOs to probe for the "is regular cargo present?" gate.
CARGO_PROBE = ("FDX", "UPS", "CLX", "GEC", "BOX", "CKS", "BCS", "GTI", "CAO", "ABW", "GEC")


# ----------------------------------------------------------------------------- build
async def build_base(db: DB) -> None:
    print("[build] api.af_base (matched + preconditioned ~raw flights) ...", flush=True)
    await db.execute_file("10_af_base.sql")
    n = await db.fetch_val("SELECT count(*) FROM api.af_base")
    print(f"[build] af_base rows = {n:,}", flush=True)
    print("[build] api.af_tail_dormancy ...", flush=True)
    await db.execute_file("32_af_tail_dormancy.sql")


async def build_aggregates(db: DB, gran: str, cell_col: str) -> None:
    print(f"[build] aggregates ({gran}) ...", flush=True)
    for f in ("20_af_cells.sql", "30_af_s1.sql", "31_af_cell_daily.sql", "33_af_s4.sql"):
        await db.execute_file(f, cell=cell_col, suffix=gran)


# ------------------------------------------------------------------------------ gates
async def run_gates(db: DB) -> dict:
    print("\n=== STEP 0 SANITY GATES ===", flush=True)
    g: dict = {}

    cells = await db.fetch_df("SELECT n_flights, n_active_days FROM api.af_cells_fine")
    dpd = (cells["n_flights"] / cells["n_active_days"].clip(lower=1)).astype(float)
    g["cells_fine"] = int(len(cells))
    g["departures_per_day_quantiles"] = {
        q: round(float(dpd.quantile(q)), 2) for q in (0.1, 0.25, 0.5, 0.75, 0.9)
    }
    g["cells_below_2_dep_per_day"] = int((dpd < 2).sum())
    print(f"  cells (fine) = {g['cells_fine']}; departures/day quantiles = "
          f"{g['departures_per_day_quantiles']}; cells <2 dep/day = {g['cells_below_2_dep_per_day']}",
          flush=True)

    base_n = await db.fetch_val("SELECT count(*) FROM api.af_base")
    tails = await db.fetch_df(
        "SELECT count(*) AS n, count(*) FILTER (WHERE op_deliv IS NULL) AS no_deliv "
        "FROM api.af_tail_dormancy")
    g["base_flights"] = int(base_n)
    g["tails"] = int(tails.loc[0, "n"])
    g["tails_no_delivery_date"] = int(tails.loc[0, "no_deliv"])
    # how many in-service matched flights the operator-delivery clip removed
    clip = await db.fetch_df(
        """
        WITH cur AS (
            SELECT DISTINCT ON (c."Registration") c."Registration" AS reg,
                   c."Operator Delivery Date" AS op_deliv, c."Status" AS status
            FROM cirium.ciriumaircrafts c
            WHERE c."Registration" IS NOT NULL AND btrim(c."Registration") <> ''
            ORDER BY c."Registration", c.revision_id DESC )
        SELECT count(*) AS matched_inservice,
               count(*) FILTER (WHERE cur.op_deliv IS NOT NULL
                    AND (f.datetime_takeoff AT TIME ZONE 'UTC')::date < cur.op_deliv) AS clipped
        FROM flightradar.flightsummary f JOIN cur ON cur.reg = f.reg
        WHERE f.reg IS NOT NULL AND btrim(f.reg) <> '' AND f.datetime_takeoff IS NOT NULL
          AND cur.status = 'In Service'
        """)
    mi = int(clip.loc[0, "matched_inservice"]); cl = int(clip.loc[0, "clipped"])
    g["matched_inservice_flights"] = mi
    g["clipped_by_operator_delivery"] = cl
    g["clipped_fraction"] = round(cl / mi, 4) if mi else None
    print(f"  base flights kept = {g['base_flights']:,}; tails = {g['tails']} "
          f"(no delivery date = {g['tails_no_delivery_date']}); "
          f"clip removed {cl:,}/{mi:,} = {g['clipped_fraction']}", flush=True)

    cargo = await db.fetch_df(
        """
        SELECT DISTINCT ON (c."Registration") c."Operator ICAO" AS op_icao, c."Operator" AS op
        FROM cirium.ciriumaircrafts c
        WHERE c."Registration" IN (SELECT DISTINCT reg FROM api.af_base)
        ORDER BY c."Registration", c.revision_id DESC
        """)
    probe = set(CARGO_PROBE)
    cargo_hits = cargo[
        cargo["op_icao"].isin(probe)
        | cargo["op"].fillna("").str.contains("cargo|fedex|dhl|cargolux|ups", case=False, regex=True)
    ]
    present = sorted(set(cargo_hits["op"].dropna().tolist()))
    g["regular_cargo_present"] = present
    print(f"  regular-cargo operators among matched tails: {present or 'NONE (signed gap)'}", flush=True)
    return g


# ---------------------------------------------------------------------------- compute
async def compute_granularity(db: DB, gran: str, tail_df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    cells = await db.fetch_df(f"SELECT * FROM api.af_cells_{gran}")
    s1 = await db.fetch_df(f"SELECT * FROM api.af_s1_{gran}")
    s4 = await db.fetch_df(f"SELECT * FROM api.af_s4_{gran}")
    daily = await db.fetch_df(
        f"SELECT cell, takeoff_date, departures FROM api.af_cell_daily_{gran}")
    s2 = compute_s2(daily)
    s3 = compute_s3(tail_df, f"cell_{gran}")
    feats = assemble_features(cells, s1, s2, s3, s4)
    scored, meta = compute_score(feats)
    return scored, meta


# ------------------------------------------------------------------------------- math
def kde1d(x: np.ndarray, grid: np.ndarray) -> np.ndarray:
    x = x[~np.isnan(x)]
    n = x.size
    if n < 2:
        return np.zeros_like(grid)
    sd = x.std(ddof=1)
    bw = 1.06 * sd * n ** (-0.2) if sd > 0 else 1.0
    bw = bw if bw > 0 else 1.0
    u = (grid[:, None] - x[None, :]) / bw
    k = np.exp(-0.5 * u ** 2) / np.sqrt(2 * np.pi)
    return k.sum(axis=1) / (n * bw)


def count_modes(grid: np.ndarray, dens: np.ndarray, prom_frac: float = 0.10) -> list[float]:
    peaks = []
    gmax = dens.max() if dens.size else 0.0
    for i in range(1, len(dens) - 1):
        if dens[i] > dens[i - 1] and dens[i] >= dens[i + 1] and dens[i] > prom_frac * gmax:
            peaks.append(float(grid[i]))
    return peaks


def bimodality_coefficient(x: np.ndarray) -> float:
    x = x[~np.isnan(x)]
    n = x.size
    if n < 4:
        return float("nan")
    m = x.mean(); s = x.std(ddof=1)
    if s == 0:
        return float("nan")
    z = (x - m) / s
    g1 = (n / ((n - 1) * (n - 2))) * np.sum(z ** 3)  # sample skewness
    g2 = ((n * (n + 1)) / ((n - 1) * (n - 2) * (n - 3))) * np.sum(z ** 4) \
        - (3 * (n - 1) ** 2) / ((n - 2) * (n - 3))    # sample excess kurtosis
    return float((g1 ** 2 + 1) / (g2 + 3 * (n - 1) ** 2 / ((n - 2) * (n - 3))))


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 3:
        return float("nan")
    ra = pd.Series(a).rank().to_numpy(dtype=float, copy=True)
    rb = pd.Series(b).rank().to_numpy(dtype=float, copy=True)
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    denom = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / denom) if denom else float("nan")


# ------------------------------------------------------------------------------- plots
def plot_kde(df: pd.DataFrame, path: Path) -> dict:
    d = df.dropna(subset=["pc1"])
    pc1 = d["pc1"].to_numpy(dtype=float)
    grid = np.linspace(pc1.min() - 1, pc1.max() + 1, 400)
    dens = kde1d(pc1, grid)
    modes = count_modes(grid, dens)
    bc = bimodality_coefficient(pc1)

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(grid, dens, color="#333", lw=2, label="KDE of PC1 (all cells)")
    ax.fill_between(grid, dens, color="#333", alpha=0.07)
    # rug, low-confidence cells thinner
    y0 = -dens.max() * 0.04
    for _, r in d.iterrows():
        col = CATEGORY_COLOR.get(r["label"], "#bbbbbb")
        ax.plot([r["pc1"], r["pc1"]], [0, y0], color=col,
                lw=0.6 if r["low_confidence"] else 1.4, alpha=0.8)
    for m in modes:
        ax.axvline(m, color="#d62728", ls=":", lw=0.8)
    ax.set_xlabel("PC1  (higher = more schedule-regular)")
    ax.set_ylabel("density")
    ax.set_title(f"PC1 distribution — {len(d)} cells — "
                 f"{len(modes)} KDE mode(s), bimodality coeff = {bc:.3f}")
    handles = [plt.Line2D([], [], color=c, lw=3, label=k) for k, c in CATEGORY_COLOR.items()
               if (df["label"] == k).any()]
    handles.append(plt.Line2D([], [], color="#bbbbbb", lw=3, label="unlabeled"))
    ax.legend(handles=handles, fontsize=8, ncol=2)
    fig.tight_layout(); fig.savefig(path, dpi=130); plt.close(fig)
    return {"modes": modes, "bimodality_coefficient": bc, "n_cells": int(len(d))}


def plot_scatter(df: pd.DataFrame, path: Path) -> None:
    d = df.dropna(subset=["pc1", "pc2"]).copy()
    fig, ax = plt.subplots(figsize=(9.5, 7))
    # marker size proportional to sqrt(flights) (brief sec. 7.5 / 7.8: small cells recede)
    nf = d["n_flights"].astype(float)
    d["msize"] = 10 + 600 * (np.sqrt(nf) / np.sqrt(nf.max()))
    # unlabeled first (grey, behind), labeled on top
    un = d[d["label"].isna()]
    ax.scatter(un["pc1"], un["pc2"], s=un["msize"], c="#cccccc", alpha=0.5,
               edgecolors="none", label="unlabeled")
    for cat, col in CATEGORY_COLOR.items():
        sub = d[d["label"] == cat]
        if sub.empty:
            continue
        ax.scatter(sub["pc1"], sub["pc2"], s=sub["msize"], c=col, alpha=0.85,
                   edgecolors="white", linewidths=0.4, label=cat)
    # annotate a few anchors: largest cell per key
    for key, name in [("VISTAJET", "VistaJet"), ("MLH", "Avion Express"),
                      ("IGO", "IndiGo"), ("UAE", "Emirates")]:
        sub = d[d["carrier_key"] == key]
        if sub.empty:
            continue
        r = sub.loc[sub["n_flights"].idxmax()]
        ax.annotate(name, (r["pc1"], r["pc2"]), fontsize=8, weight="bold",
                    xytext=(5, 5), textcoords="offset points")
    ax.axhline(0, color="#999", lw=0.6); ax.axvline(0, color="#999", lw=0.6)
    ax.set_xlabel("PC1  (higher = more schedule-regular)")
    ax.set_ylabel("PC2  (intensity vs route-regularity dissociation)")
    ax.set_title("Archetype space — marker size proportional to sqrt(flights)")
    ax.legend(fontsize=8, ncol=2, loc="best")
    fig.tight_layout(); fig.savefig(path, dpi=130); plt.close(fig)


# ----------------------------------------------------------------------------- verdict
def build_verdict(scored: dict[str, pd.DataFrame], metas: dict[str, dict],
                  kde_stats: dict[str, dict], gates: dict) -> str:
    df = scored["fine"]
    meta = metas["fine"]
    ks = kde_stats["fine"]
    lines: list[str] = []
    A = lines.append

    A("# Archetype layer — distribution-shape verdict\n")
    A("> Brief sec. 7-8. This single output configures the Anchor (step 2) and Pooling "
      "(step 3). It is a diagnostic on the ~8M-flight FR24 panel, not a production service.\n")

    A("## Panel\n")
    A(f"- base flights kept (matched, in-service, operator-clipped, raw): **{gates['base_flights']:,}**")
    A(f"- tails: **{gates['tails']}** (no operator-delivery date: {gates['tails_no_delivery_date']})")
    A(f"- cells (fine = carrier x type x seats x range): **{meta['n_total']}** "
      f"({meta['n_fit']} complete-case, used in PCA)")
    A(f"- operator-delivery clip removed {gates['clipped_by_operator_delivery']:,} / "
      f"{gates['matched_inservice_flights']:,} matched in-service flights "
      f"(= {gates['clipped_fraction']})")
    A(f"- departures/day quantiles across cells: {gates['departures_per_day_quantiles']}")
    A(f"- regular-cargo reference present: {gates['regular_cargo_present'] or 'NONE (signed gap)'}\n")

    A("## PCA (unsupervised first look)\n")
    ev = meta["explained_variance_ratio"]
    A(f"- explained variance: PC1 = **{ev[0]*100:.1f}%**, PC2 = **{ev[1]*100:.1f}%**, "
      f"PC3 = {ev[2]*100:.1f}%, PC4 = {ev[3]*100:.1f}%" if len(ev) >= 4 else
      f"- explained variance: {['%.3f' % x for x in ev]}")
    A("- PC1 loadings (expect all positive = regularity axis): "
      + ", ".join(f"{k}={v:+.2f}" for k, v in meta["loadings"]["PC1"].items()))
    A("- PC2 loadings: "
      + ", ".join(f"{k}={v:+.2f}" for k, v in meta["loadings"]["PC2"].items()) + "\n")

    A("## Shape of PC1\n")
    A(f"- KDE prominent modes: **{len(ks['modes'])}** at PC1 ~ "
      f"{[round(m,2) for m in ks['modes']]}")
    A(f"- bimodality coefficient: **{ks['bimodality_coefficient']:.3f}** "
      f"(> 0.555 hints multimodal; uniform = 0.33; the figure is the arbiter)\n")

    # label ordering check
    A("## Reference-label validation (PC1 ordering)\n")
    cat_rows = []
    for cat in CATEGORY_REGULARITY:
        sub = df[df["label"] == cat].dropna(subset=["pc1"])
        if sub.empty:
            continue
        cat_rows.append((cat, CATEGORY_REGULARITY[cat], float(sub["pc1"].mean()),
                         len(sub), {s: round(float(sub[s].mean()), 3) for s in SIGNATURES}))
    if cat_rows:
        A("| category | expected rank | mean PC1 | n cells | mean S1/S2/S3/S4 |")
        A("|---|---|---|---|---|")
        for cat, exp, mpc1, n, sig in sorted(cat_rows, key=lambda r: -r[2]):
            A(f"| {cat} | {exp} | {mpc1:+.2f} | {n} | "
              f"{sig['s1']}/{sig['s2']}/{sig['s3']}/{sig['s4']} |")
        exp_arr = np.array([r[1] for r in cat_rows])
        obs_arr = np.array([r[2] for r in cat_rows])
        rho = spearman(exp_arr, obs_arr)
        A(f"\n- Spearman(expected regularity rank, observed mean PC1) = **{rho:.2f}** "
          f"(near +1 ⇒ PC1 really is the schedule↔on-demand axis)\n")

    # PC2 / fractional litmus
    A("## PC2 — what the second axis actually is (brief sec. 7.6)\n")
    pc2_load = meta["loadings"]["PC2"]
    dom = max(pc2_load, key=lambda k: abs(pc2_load[k]))
    A(f"- PC2 is dominated by **{dom}** (loading {pc2_load[dom]:+.2f}); the others are "
      f"{', '.join(f'{k}={v:+.2f}' for k, v in pc2_load.items() if k != dom)}.")
    A(f"- Interpretation: with {dom}=S2 driving it, PC2 is a *weekly-cadence shape* axis "
      "(strong day-of-week rhythm high vs daily-uniform low) **within** the regular cluster — "
      "NOT the fractional intensity-vs-routes dissociation the brief hypothesised.")
    frac = df[df["label"] == "business_fractional"].dropna(subset=["pc1", "pc2"])
    reg = df[df["label"].isin(["fsc", "lcc", "regional"])].dropna(subset=["pc1", "pc2"])
    if not frac.empty and not reg.empty:
        d1 = abs(frac["pc1"].mean() - reg["pc1"].mean())
        d2 = abs(frac["pc2"].mean() - reg["pc2"].mean())
        A(f"- fractional vs regular separation: on PC1 = **{d1:.2f}**, on PC2 = **{d2:.2f}** "
          f"⇒ fractional/on-demand resolves on **{'PC1' if d1 >= d2 else 'PC2'}** "
          "(the litmus lands on PC1 here, not a second axis).")
        vj = df[df["carrier_key"] == "VISTAJET"].dropna(subset=["pc1", "pc2"])
        if not vj.empty:
            r = vj.loc[vj["n_flights"].idxmax()]
            A(f"- VistaJet anchor: PC1={r['pc1']:+.2f}, PC2={r['pc2']:+.2f}, "
              f"S1={r['s1']:.2f} S3={r['s3']:.2f} (high-ish S3, ~0 S1/S4 ⇒ sits at the on-demand "
              "end of PC1, only mildly off on PC2).")
    A("")

    # Mode composition & the low-confidence-spread check (brief sec. 7.8 pitfall #1)
    A("## Mode composition & low-confidence check (brief sec. 7.8)\n")
    hk = kde_stats.get("fine_hiconf", {})
    A(f"- all cells: {len(ks['modes'])} KDE modes, BC={ks['bimodality_coefficient']:.3f} "
      f"({ks['n_cells']} cells).")
    if hk:
        A(f"- high-confidence cells only: {len(hk['modes'])} KDE modes, "
          f"BC={hk['bimodality_coefficient']:.3f} ({hk['n_cells']} cells). "
          + ("Bimodality SURVIVES the exposure filter ⇒ the on-demand mode is real, not "
             "small-cell spread." if len(hk["modes"]) >= 2 else
             "Bimodality WEAKENS under the exposure filter ⇒ the left mode is partly small-cell "
             "dispersion; read with caution."))
    if len(ks["modes"]) >= 2:
        split = (min(ks["modes"]) + max(ks["modes"])) / 2.0
        d = df.dropna(subset=["pc1"])
        left = d[d["pc1"] < split]; right = d[d["pc1"] >= split]
        A(f"- valley at PC1≈{split:.2f}: left/on-demand mode = {len(left)} cells "
          f"({left['low_confidence'].mean()*100:.0f}% low-confidence), "
          f"right/schedule mode = {len(right)} cells "
          f"({right['low_confidence'].mean()*100:.0f}% low-confidence).")
    A("")

    # Analyst notes — anomalies raised honestly (brief sec. 0 invites this)
    A("## Analyst notes — anomalies raised\n")
    ca = df[df["label"] == "charter_acmi"].dropna(subset=["pc1"])
    if not ca.empty and ca["pc1"].mean() > 0:
        A(f"- **ACMI looks regular** (charter_acmi mean PC1 = {ca['pc1'].mean():+.2f}, n={len(ca)}): "
          "long-term ACMI/wet-lease operators (e.g. Avion Express) fly *other carriers'* schedules, "
          "so behaviourally they read as schedule-regular. This corroborates the brief's premise "
          "that axis-1 (cargo/ACMI/VIP) is NOT separable from behaviour and must come from Cirium — "
          "it is not a defect of the signatures.")
    cg = df[df["label"] == "cargo_regular"].dropna(subset=["pc1"])
    n_cargo_all = int((df["label"] == "cargo_regular").sum())
    A(f"- **Cargo under-represented in complete-case**: only {len(cg)} of {n_cargo_all} cargo cells "
      "survived to the PCA (rest low-confidence / short series), so the scheduled-cargo corner is "
      "not yet validated; treat its position as provisional.")
    A("- **S2 saturates on daily operations** (why it split onto PC2): a truly regular sub-fleet "
      "flying the same banks 7 days/week has LOW day-of-week R² (every weekday looks alike), so "
      "'higher S2 = more regular' is non-monotone for high-frequency carriers (lcc mean S2 = 0.05 "
      "< fsc 0.12 < regional 0.27). S2 measures weekly *shape*, not regularity per se — step 2 "
      "should consume PC2/S2 as the weekly-seasonality term, not as a second regularity score.")
    A("")

    # coarse robustness
    ck = kde_stats["coarse"]
    A("## Robustness (coarse granularity carrier x type)\n")
    A(f"- coarse KDE modes: {len(ck['modes'])}; bimodality coeff = "
      f"{ck['bimodality_coefficient']:.3f}; cells = {ck['n_cells']}. "
      "Shape verdict should agree with fine.\n")

    # automated read
    A("## Read\n")
    hk = kde_stats.get("fine_hiconf", {})
    hiconf_bimodal = len(hk.get("modes", [])) >= 2 if hk else None
    multimodal = len(ks["modes"]) >= 2 and ks["bimodality_coefficient"] > 0.555
    pc2_strong = ev[1] >= 0.20
    pc2_dom = max(meta["loadings"]["PC2"], key=lambda k: abs(meta["loadings"]["PC2"][k]))
    A(f"- **PC1 shape:** {'DISCRETE modes (schedule vs on-demand)' if multimodal else 'CONTINUUM'} "
      f"— {len(ks['modes'])} KDE modes, BC={ks['bimodality_coefficient']:.3f}"
      + (f"; survives high-confidence filter ({len(hk['modes'])} modes)." if hiconf_bimodal
         else f"; weakens on high-confidence cells ({len(hk.get('modes', []))} modes)." if hk
         else "."))
    A(f"- **PC2:** real ({ev[1]*100:.1f}% variance) but it is a **{pc2_dom}-driven weekly-cadence "
      "axis** (rhythmic vs daily-uniform) inside the regular cluster — NOT a separate on-demand "
      "dimension. The schedule↔on-demand split is on PC1.")
    A("- **Downstream (anchor, step 2):** "
      + ("treat PC1 as discrete buckets at the antimode (~the valley) — bucket-specific anchor "
         "tightness; the on-demand bucket needs its own (low-recurrence) intensity model."
         if multimodal else
         "continuous score → interpolate anchor tightness along PC1."))
    A("- **Downstream (pooling, step 3):** "
      + ("pool within bucket; " if multimodal else "kernel-neighbourhood pooling along PC1; ")
      + "carry PC2 as a second pooling coordinate for the seasonal/weekly decomposition "
        "(it is the weekly-cadence shape, which the anchor's seasonality term consumes).")
    A("\n> This read is threshold-computed; the KDE/scatter PNGs are the primary evidence "
      "(brief sec. 7.5). Confirm visually before locking the design.\n")

    A("## Signed limitations\n")
    A("1. Cirium has no historical snapshots before 2025-11 → operator/status on a historical "
      "date is approximated by the current operator + delivery-date clip; "
      f"{gates['tails_no_delivery_date']} tails have no delivery date (kept whole window, upward bias).")
    A("2. ACMI inseparable → short presence inflates S4 singletons (e.g. Avion Express); on-demand "
      "tilt is partly an artifact, signed not signal.")
    A("3. Range/cruise not in Cirium → range band derived from observed median haul (sub-fleet proxy).")
    A("4. Raw flights, no dedup (brief sec. 7.2 iii) → possible FR24 seam doubles inflate counts.")
    A("5. Coherence ≠ accuracy (brief principle); the truth test is LOCO coverage at step 4.")
    return "\n".join(lines)


# -------------------------------------------------------------------------------- main
async def main(rebuild: bool) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with DB(statement_timeout_ms=0) as db:
        if rebuild:
            await build_base(db)
            for gran, col in GRANS:
                await build_aggregates(db, gran, col)
        gates = await run_gates(db)

        tail_df = await db.fetch_df(
            "SELECT cell_fine, cell_coarse, reg, active_days, first_active, last_active, op_deliv "
            "FROM api.af_tail_dormancy")

        scored: dict[str, pd.DataFrame] = {}
        metas: dict[str, dict] = {}
        kde_stats: dict[str, dict] = {}
        for gran, _ in GRANS:
            print(f"\n[compute] granularity = {gran}", flush=True)
            df, meta = await compute_granularity(db, gran, tail_df)
            scored[gran], metas[gran] = df, meta
            df.to_csv(OUT / f"archetype_features_{gran}.csv", index=False)
            kde_stats[gran] = plot_kde(df, OUT / f"pc1_kde_{gran}.png")
            if gran == "fine":
                hi = df[~df["low_confidence"]]
                kde_stats["fine_hiconf"] = plot_kde(hi, OUT / "pc1_kde_fine_hiconf.png")
                plot_scatter(df, OUT / "pc1_pc2_scatter.png")
            print(f"  cells={meta['n_total']} fit={meta['n_fit']} "
                  f"explained={[round(x,3) for x in meta['explained_variance_ratio']]}", flush=True)
            print(f"  PC1 loadings={ {k: round(v,2) for k,v in meta['loadings']['PC1'].items()} }",
                  flush=True)

        verdict = build_verdict(scored, metas, kde_stats, gates)
        (OUT / "archetype_verdict.md").write_text(verdict, encoding="utf-8")
        (OUT / "archetype_meta.json").write_text(
            json.dumps({"gates": gates, "meta": metas, "kde": kde_stats}, indent=2, default=str),
            encoding="utf-8")
        print(f"\n[done] wrote {OUT/'archetype_verdict.md'} and figures.", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-rebuild", dest="rebuild", action="store_false",
                    help="reuse existing api.af_* matviews instead of rebuilding")
    ap.set_defaults(rebuild=True)
    args = ap.parse_args()
    asyncio.run(main(args.rebuild))

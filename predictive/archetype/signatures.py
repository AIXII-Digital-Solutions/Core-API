"""Signature assembly (brief sec. 7.3).

S1 (recurrence) and S4 (dispersion) come pre-aggregated from SQL. S2 (weekly
periodicity) and S3 (dormancy) are finished here in numpy/pandas because they need
per-series math (MA28 detrend + day-of-week regression) and a per-tail median that
SQL expresses awkwardly. All four are oriented so higher = more schedule-regular.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .reference_labels import label_for

SIGNATURES = ["s1", "s2", "s3", "s4"]

# Low-confidence exposure gates (brief sec. 7.5 step 2: flag, do NOT drop).
MIN_FLIGHTS = 200
MIN_ACTIVE_DAYS = 60
MIN_S2_DAYS = 60


def compute_s2(daily: pd.DataFrame) -> pd.DataFrame:
    """S2 = adjusted R^2 of day-of-week dummies on MA28-detrended daily departures.

    Detrend removes trend + monthly seasonality (28d centered MA); the residual
    variance explained by the 7 weekday levels is the weekly-periodicity signal.
    Adjusted R^2 guards the small-fleet upward bias of raw R^2 (brief sec. 7.3 trap).
    """
    rows = []
    for cell, g in daily.groupby("cell", sort=False):
        g = g.sort_values("takeoff_date")
        d0, d1 = g["takeoff_date"].min(), g["takeoff_date"].max()
        full = pd.date_range(d0, d1, freq="D")
        s = pd.Series(0.0, index=full)
        s.loc[pd.to_datetime(g["takeoff_date"].to_numpy())] = g["departures"].to_numpy(dtype=float)
        D = s.to_numpy(dtype=float)
        n = D.size
        avg = float(D.mean()) if n else 0.0
        if n < 35:
            rows.append((cell, np.nan, n, avg, "short_series"))
            continue
        ma = pd.Series(D).rolling(28, center=True, min_periods=14).mean().to_numpy()
        resid = D - ma
        mask = ~np.isnan(resid)
        y = resid[mask]
        dow = full.dayofweek.to_numpy()[mask]  # 0..6
        m = y.size
        # design = intercept + 6 weekday dummies (one level dropped)
        X = np.zeros((m, 7))
        X[:, 0] = 1.0
        for k in range(1, 7):
            X[:, k] = (dow == k).astype(float)
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        yhat = X @ beta
        ss_res = float(np.sum((y - yhat) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 0.0 if ss_tot <= 0 else 1.0 - ss_res / ss_tot
        p = 6
        adj = 1.0 - (1.0 - r2) * (m - 1) / (m - p - 1) if (m - p - 1) > 0 else np.nan
        s2 = np.nan if np.isnan(adj) else max(0.0, adj)
        rows.append((cell, s2, m, avg, "ok"))
    return pd.DataFrame(rows, columns=["cell", "s2", "s2_n_days", "avg_departures", "s2_note"])


def compute_s3(tail: pd.DataFrame, cell_col: str) -> pd.DataFrame:
    """S3 = median over tails of active_days / in-service_days (brief sec. 7.3).

    in-service window = [max(cell observed start, operator delivery date), cell observed
    end]. Using the cell's observed span as the reference neutralises late ADS-B coverage
    of a carrier while the operator-delivery clip still shortens a tail that joined late.
    """
    df = tail.copy()
    for c in ("first_active", "last_active", "op_deliv"):
        df[c] = pd.to_datetime(df[c], errors="coerce")
    win = df.groupby(cell_col).agg(cws=("first_active", "min"), cwe=("last_active", "max"))
    df = df.join(win, on=cell_col)
    insvc_start = df[["cws", "op_deliv"]].max(axis=1)  # NaT op_deliv -> cws
    d_a = (df["cwe"] - insvc_start).dt.days + 1
    d_a = d_a.clip(lower=1)
    df["ratio"] = (df["active_days"] / d_a).clip(upper=1.0)
    out = df.groupby(cell_col)["ratio"].median().reset_index()
    out.columns = ["cell", "s3"]
    return out


def assemble_features(
    cells: pd.DataFrame,
    s1: pd.DataFrame,
    s2: pd.DataFrame,
    s3: pd.DataFrame,
    s4: pd.DataFrame,
) -> pd.DataFrame:
    """Merge the cell dimension + four signatures into one per-cell feature table."""
    df = cells.merge(s1[["cell", "s1", "s1_den_flights", "n_keys"]], on="cell", how="left")
    df = df.merge(s2[["cell", "s2", "s2_n_days", "avg_departures", "s2_note"]], on="cell", how="left")
    df = df.merge(s3[["cell", "s3"]], on="cell", how="left")
    df = df.merge(s4[["cell", "s4", "s4_entropy_norm", "n_pairs", "n_singletons"]], on="cell", how="left")

    # S1 NaN => cell has no usable flight-number keys => no recurrence => 0.0 (signed:
    # FR24 flight-number quality, brief sec. 7.3 S1 trap -> cross-check with S2/S4).
    df["s1_imputed"] = df["s1"].isna()
    df["s1"] = df["s1"].fillna(0.0)

    df["label"] = df["carrier_key"].map(label_for)
    df["low_confidence"] = (
        (df["n_flights"] < MIN_FLIGHTS)
        | (df["n_active_days"] < MIN_ACTIVE_DAYS)
        | (df["s2_n_days"].fillna(0) < MIN_S2_DAYS)
    )
    return df

"""Score assembly (brief sec. 7.4): orient -> z-standardize -> PCA(SVD) -> PC1/PC2.

Unsupervised first look. PC1 is expected to be the regularity axis (positive loadings
on all four oriented signatures); PC2 is kept and inspected because the space may be 2D
(brief sec. 7.6 fractional litmus). The supervised projector (LDA/logit on reference
labels) is a deliberate follow-on, run only after the shape verdict.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .signatures import SIGNATURES


def compute_score(features: pd.DataFrame, cols: list[str] | None = None) -> tuple[pd.DataFrame, dict]:
    """Add pc1/pc2 columns (NaN for incomplete cells) and return (df, meta).

    meta has loadings, explained-variance ratios, the per-feature mean/std, and the
    list of cells actually used to fit (complete cases).
    """
    cols = cols or SIGNATURES
    df = features.copy()

    complete = df[cols].notna().all(axis=1)
    fit = df.loc[complete, cols].to_numpy(dtype=float)
    mu = fit.mean(axis=0)
    sd = fit.std(axis=0, ddof=0)
    sd_safe = np.where(sd == 0, 1.0, sd)
    Z = (fit - mu) / sd_safe

    # PCA via SVD on the standardized matrix.
    U, S, Vt = np.linalg.svd(Z, full_matrices=False)
    scores = U * S                       # (n_cells, n_comp)
    explained = (S ** 2) / float(np.sum(S ** 2))
    loadings = Vt                        # rows = components, cols = features

    # Sign-fix PC1 so higher = more regular (align with S3 loading sign), PC2 by its
    # largest-magnitude loading for reproducibility.
    s3_i = cols.index("s3")
    if loadings[0, s3_i] < 0:
        loadings[0] *= -1
        scores[:, 0] *= -1
    if loadings.shape[0] > 1:
        j = int(np.argmax(np.abs(loadings[1])))
        if loadings[1, j] < 0:
            loadings[1] *= -1
            scores[:, 1] *= -1

    df.loc[complete, "pc1"] = scores[:, 0]
    if scores.shape[1] > 1:
        df.loc[complete, "pc2"] = scores[:, 1]
    else:
        df["pc2"] = np.nan

    meta = {
        "cols": cols,
        "n_fit": int(complete.sum()),
        "n_total": int(len(df)),
        "mean": dict(zip(cols, mu.tolist())),
        "std": dict(zip(cols, sd.tolist())),
        "explained_variance_ratio": explained.tolist(),
        "loadings": {f"PC{i+1}": dict(zip(cols, loadings[i].tolist())) for i in range(loadings.shape[0])},
    }
    return df, meta

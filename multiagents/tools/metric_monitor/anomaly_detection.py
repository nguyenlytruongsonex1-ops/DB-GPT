import numpy as np


def detect_anomalies(data, significance_level=0.2):
    """Robust outlier check (modified z-score). Returns (score, flagged).

    A flat series is not flagged. The old KS compared a sorted sample to itself,
    so a constant series scored about 1 and was always treated as abnormal.
    significance_level stays in the signature for existing callers; the cutoff
    is the fixed 3.5 modified-z threshold.
    """
    arr = np.asarray([] if data is None else data, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 4:
        return 0.0, False
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    if mad == 0:
        flagged = bool(np.any(arr != med))
        score = float(np.max(np.abs(arr - med))) if flagged else 0.0
        return score, flagged
    score = float(np.max(0.6745 * np.abs(arr - med) / mad))
    return score, bool(score > 3.5)

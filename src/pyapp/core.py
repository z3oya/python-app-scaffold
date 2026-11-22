"""Business core; compiled to .pyd for distribution."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Summary:
    count: int
    mean: float
    std: float
    peak: float


def moving_average(values: np.ndarray, window: int) -> np.ndarray:
    if window < 1:
        raise ValueError("window must be >= 1")
    if window > values.size:
        raise ValueError("window must not exceed input size")
    n = values.size
    cs = np.cumsum(values, dtype=np.float64)
    out = cs[window - 1 :].copy()
    out[1:] -= cs[: n - window]
    out /= window
    return out


def summarize(values: np.ndarray) -> Summary:
    if values.size == 0:
        raise ValueError("input must be non-empty")
    return Summary(
        count=int(values.size),
        mean=float(values.mean()),
        std=float(values.std()),
        peak=float(values.max()),
    )

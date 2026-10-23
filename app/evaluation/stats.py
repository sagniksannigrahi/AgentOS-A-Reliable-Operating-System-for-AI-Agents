"""Small, dependency-free statistics helpers for benchmark reporting."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float | None, float | None, float | None]:
    """Proportion with a Wilson score 95% interval. Returns (p, low, high); Nones when n == 0."""
    if n == 0:
        return None, None, None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def bootstrap_mean(
    values: Sequence[float], *, seed: int = 0, b: int = 2000
) -> tuple[float | None, float | None, float | None]:
    """Mean with a percentile-bootstrap 95% interval (deterministic given ``seed``)."""
    if not values:
        return None, None, None
    rng = random.Random(seed)
    n = len(values)
    mean = sum(values) / n
    if n == 1:
        return mean, mean, mean
    means = sorted(sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(b))
    return mean, means[int(0.025 * b)], means[int(0.975 * b) - 1]


def percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)

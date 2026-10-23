"""Bounded retries with exponential backoff and full jitter."""

from __future__ import annotations

import random


def backoff_delay(attempt: int, base_s: float, max_s: float, rng: random.Random | None = None) -> float:
    """Delay before retry number ``attempt`` (1-based): uniform(0, min(max, base * 2**(attempt-1)))."""
    cap = min(max_s, base_s * (2 ** max(0, attempt - 1)))
    return (rng or random).uniform(0, cap)

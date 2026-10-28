"""Seeded irregular contribution stream (STRATEGY.md §13.4)."""

from __future__ import annotations

import math
from decimal import Decimal

import numpy as np

from .params import ContributionModel


def contribution_stream(n_days: int, model: ContributionModel, seed: int) -> dict[int, Decimal]:
    """{trading-day index: whole-dollar amount}. Day 0 never gets a contribution
    (the initial capital arrives that day). The same seed always gives the
    same stream, and the stream for a prefix of days does not depend on n_days."""
    rng = np.random.default_rng(seed)
    out: dict[int, Decimal] = {}
    for day in range(1, n_days):
        arrives = rng.random() < model.probability
        draw = rng.lognormal(mean=math.log(model.median), sigma=model.log_sigma)
        if arrives:
            amount = min(model.maximum, max(model.minimum, round(draw)))
            out[day] = Decimal(int(amount))
    return out

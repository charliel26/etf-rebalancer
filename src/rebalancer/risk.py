"""Risk monitoring for the report (STRATEGY.md §12). Never trades.

Volatility and drawdown describe the *current* mix of invested holdings over
recent history: today's value weights applied to each symbol's daily returns.
Cash is excluded. Plain floats are fine here; nothing becomes an order.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import pairwise

from .config import RiskConfig


def holdings_returns(closes: Mapping[str, Sequence[tuple[date, Decimal]]], weights: Mapping[str, float]) -> list[float]:
    """Daily returns of a fixed-weight mix on the dates every weighted symbol has.

    Weights are normalized over the symbols given; symbols without closes make
    the result empty (the caller reports "no data" rather than guessing).
    """
    active = {s: w for s, w in weights.items() if w > 0}
    if not active or any(s not in closes or len(closes[s]) < 2 for s in active):
        return []
    total = sum(active.values())
    series = {s: dict(closes[s]) for s in active}
    common = sorted(set.intersection(*(set(p) for p in series.values())))
    out = []
    for prev, cur in pairwise(common):
        r = 0.0
        for s, w in active.items():
            before, after = float(series[s][prev]), float(series[s][cur])
            if before <= 0:
                return []
            r += (w / total) * (after / before - 1.0)
        out.append(r)
    return out


def ewma_vol(returns: Sequence[float], lam: float, window: int) -> float | None:
    """Annualized EWMA volatility over the last `window` returns, normalized
    weights lambda^k / sum(lambda^k), zero-mean (RiskMetrics). None if < 20 returns."""
    recent = list(returns)[-window:]
    if len(recent) < min(20, window):
        return None
    weights = [lam**k for k in range(len(recent))]
    variance = sum(w * r * r for w, r in zip(weights, reversed(recent), strict=True)) / sum(weights)
    return math.sqrt(variance) * math.sqrt(252)


def drawdown(returns: Sequence[float], lookback: int) -> tuple[float, float] | None:
    """(current drawdown, worst drawdown) over the last `lookback` returns, as negatives."""
    recent = list(returns)[-lookback:]
    if not recent:
        return None
    nav = peak = 1.0
    worst = 0.0
    for r in recent:
        nav *= 1.0 + r
        peak = max(peak, nav)
        worst = min(worst, nav / peak - 1.0)
    return nav / peak - 1.0, worst


@dataclass(frozen=True)
class RiskSummary:
    vol_annualized: float | None
    drawdown_current: float | None
    drawdown_worst: float | None
    satellite_share: float | None
    observations: int
    flags: tuple[str, ...] = field(default=())


def assess(
    position_values: Mapping[str, float],
    closes: Mapping[str, Sequence[tuple[date, Decimal]]],
    satellite_share: float | None,
    config: RiskConfig,
) -> RiskSummary:
    returns = holdings_returns(closes, position_values)
    vol = ewma_vol(returns, float(config.ewma_lambda), config.vol_window_days)
    dd = drawdown(returns, config.drawdown_lookback_days)
    flags = []
    if vol is not None and vol > float(config.vol_flag):
        flags.append("HIGH_VOLATILITY")
    if dd is not None and dd[0] < -float(config.drawdown_flag):
        flags.append("DEEP_DRAWDOWN")
    if satellite_share is not None and satellite_share > float(config.satellite_cap):
        flags.append("SATELLITE_CAP")
    if not returns:
        flags.append("NO_PRICE_HISTORY")
    return RiskSummary(
        vol_annualized=vol,
        drawdown_current=None if dd is None else dd[0],
        drawdown_worst=None if dd is None else dd[1],
        satellite_share=satellite_share,
        observations=len(returns),
        flags=tuple(flags),
    )

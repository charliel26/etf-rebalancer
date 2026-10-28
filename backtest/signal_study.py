"""Does "close below its 8-day EMA" predict returns? (STRATEGY.md §13.6)"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .metrics import newey_west_ols, nw_lag


def ema(prices: Sequence[float], span: int) -> np.ndarray:
    """E_t = a P_t + (1 - a) E_{t-1}, a = 2 / (span + 1), E_0 = P_0."""
    alpha = 2.0 / (span + 1.0)
    out = np.empty(len(prices))
    out[0] = prices[0]
    for i in range(1, len(prices)):
        out[i] = alpha * prices[i] + (1 - alpha) * out[i - 1]
    return out


def below_ema(prices: Sequence[float], span: int) -> np.ndarray:
    p = np.asarray(prices, dtype=float)
    return p < ema(p, span)


@dataclass(frozen=True)
class SignalRow:
    symbol: str
    horizon: int
    start: str  # "signal close" or "execution close"
    fire_rate: float
    mean_signal: float
    mean_other: float
    mean_all: float
    difference: float
    t_difference: float
    t_conditional: float
    observations: int
    lags: int


def forward_returns(prices: np.ndarray, horizon: int, offset: int) -> np.ndarray:
    """R_t = P_{t+offset+h} / P_{t+offset} - 1; NaN where the window runs past the end."""
    out = np.full(len(prices), np.nan)
    last = len(prices) - offset - horizon
    if last > 0:
        out[:last] = prices[offset + horizon : offset + horizon + last] / prices[offset : offset + last] - 1.0
    return out


def study(
    symbol: str, prices: Sequence[float], signal: np.ndarray, horizons: Sequence[int], warmup: int
) -> list[SignalRow]:
    p = np.asarray(prices, dtype=float)
    rows = []
    for horizon in horizons:
        for offset, label in ((0, "signal close"), (1, "execution close")):
            fwd = forward_returns(p, horizon, offset)
            keep = ~np.isnan(fwd)
            keep[:warmup] = False
            y, s = fwd[keep], signal[keep].astype(float)
            lags = nw_lag(horizon, len(y))
            beta, cov = newey_west_ols(y, s, lags)
            se_difference = float(np.sqrt(cov[1, 1]))
            # Conditional mean a + b: Var = Var(a) + Var(b) + 2 Cov(a, b), all from the same NW fit.
            cond_mean = beta[0] + beta[1]
            cond_se = float(np.sqrt(max(cov[0, 0] + cov[1, 1] + 2 * cov[0, 1], 0.0)))
            rows.append(
                SignalRow(
                    symbol=symbol,
                    horizon=horizon,
                    start=label,
                    fire_rate=float(s.mean()),
                    mean_signal=float(cond_mean),
                    mean_other=float(beta[0]),
                    mean_all=float(y.mean()),
                    difference=float(beta[1]),
                    t_difference=float(beta[1] / se_difference) if se_difference > 0 else float("nan"),
                    t_conditional=float(cond_mean / cond_se) if cond_se > 0 else float("nan"),
                    observations=len(y),
                    lags=lags,
                )
            )
    return rows

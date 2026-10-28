"""Performance metrics (STRATEGY.md §13.5). Floats: these are reports, not orders."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

TRADING_DAYS = 252


def twr_returns(values: Sequence[float], flows: Sequence[float]) -> np.ndarray:
    """r_t = (A_t - F_t) / A_{t-1} - 1 with external flows at end of day.
    Element 0 is 0 (no prior value). Days that start from zero value return 0."""
    a = np.asarray(values, dtype=float)
    f = np.asarray(flows, dtype=float)
    out = np.zeros(len(a))
    prev = a[:-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        r = (a[1:] - f[1:]) / prev - 1.0
    out[1:] = np.where(prev > 0, r, 0.0)
    return out


def cagr(returns: np.ndarray, start: date, end: date) -> float:
    days = (end - start).days
    growth = float(np.prod(1.0 + returns[1:]))
    return growth ** (365.25 / days) - 1.0 if days > 0 and growth > 0 else float("nan")


def annualized_vol(returns: np.ndarray) -> float:
    r = returns[1:]
    return float(np.std(r, ddof=1) * math.sqrt(TRADING_DAYS)) if len(r) > 1 else float("nan")


def sharpe(returns: np.ndarray, rf: np.ndarray) -> float:
    excess = returns[1:] - rf[1:]
    sd = np.std(excess, ddof=1) if len(excess) > 1 else 0.0
    # A constant series has sd ~1e-19 from rounding, not 0; treat it as undefined.
    return float(math.sqrt(TRADING_DAYS) * np.mean(excess) / sd) if sd > 1e-12 else float("nan")


@dataclass(frozen=True)
class Drawdown:
    max_drawdown: float
    peak: date | None
    trough: date | None
    recovery: date | None
    longest_underwater_days: int
    longest_unrecovered: bool


def drawdown_stats(returns: np.ndarray, dates: Sequence[date]) -> Drawdown:
    nav = np.cumprod(1.0 + returns)
    peaks = np.maximum.accumulate(nav)
    dd = nav / peaks - 1.0
    trough_i = int(np.argmin(dd))
    if dd[trough_i] >= 0:
        return Drawdown(0.0, None, None, None, 0, False)
    peak_i = int(np.argmax(nav[: trough_i + 1]))
    recovery_i = next((i for i in range(trough_i, len(nav)) if nav[i] >= nav[peak_i]), None)
    longest = run = 0
    for value in dd:
        run = run + 1 if value < 0 else 0
        longest = max(longest, run)
    unrecovered = run > 0 and run == longest
    return Drawdown(
        max_drawdown=float(dd[trough_i]),
        peak=dates[peak_i],
        trough=dates[trough_i],
        recovery=None if recovery_i is None else dates[recovery_i],
        longest_underwater_days=longest,
        longest_unrecovered=unrecovered,
    )


def xirr(flows: Sequence[tuple[date, float]]) -> float:
    """Annual money-weighted return: the rate r with sum(cf / (1 + r)^(days/365.25)) = 0.
    Flows: contributions negative (money in), final value positive. Bisection."""
    t0 = flows[0][0]

    def npv(rate: float) -> float:
        return sum(cf / (1.0 + rate) ** ((d - t0).days / 365.25) for d, cf in flows)

    low, high = -0.99, 10.0
    if npv(low) * npv(high) > 0:
        return float("nan")
    for _ in range(200):
        mid = (low + high) / 2
        if npv(low) * npv(mid) <= 0:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def cash_stats(
    cash: Sequence[float], values: Sequence[float], invested_returns: np.ndarray, cash_returns: np.ndarray
) -> tuple[float, float, int]:
    """(average cash weight, annual return cost of cash drag, days with cash > 1% of value)."""
    v = np.asarray(values, dtype=float)
    c = np.asarray(cash, dtype=float)
    weight = np.where(v > 0, c / np.where(v > 0, v, 1.0), 0.0)
    drag = float(TRADING_DAYS * np.mean(weight[:-1] * (invested_returns[1:] - cash_returns[1:]))) if len(v) > 1 else 0.0
    return float(np.mean(weight)), drag, int(np.sum(weight > 0.01))


def newey_west_ols(y: np.ndarray, x: np.ndarray, lags: int) -> tuple[np.ndarray, np.ndarray]:
    """OLS of y on [1, x] with a Newey-West (Bartlett) covariance.
    Returns (coefficients [a, b], 2x2 covariance matrix)."""
    X = np.column_stack([np.ones(len(x)), x])
    XtX_inv = np.linalg.inv(X.T @ X)
    beta = XtX_inv @ X.T @ y
    u = y - X @ beta
    Xu = X * u[:, None]
    S = Xu.T @ Xu
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1.0)
        G = Xu[lag:].T @ Xu[:-lag]
        S += w * (G + G.T)
    return beta, XtX_inv @ S @ XtX_inv


def nw_lag(horizon: int, n: int) -> int:
    """max(h - 1, floor(4 (T / 100)^(2/9)))."""
    return max(horizon - 1, math.floor(4 * (n / 100) ** (2 / 9)))

"""Pure portfolio arithmetic for the core sleeve (STRATEGY.md §4-§6).

Every function takes plain mappings of symbol -> Decimal and returns new ones.
No I/O, no clock, no randomness. Amounts are exact Decimals here; rounding to
cents happens in the planner, which knows the order limits.

Notation follows STRATEGY.md: V_i fund values, V their sum, w*_i targets,
C deployable cash.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

ZERO = Decimal(0)
ONE = Decimal(1)
# Tolerance for comparing amounts that are equal in exact arithmetic but carry
# 28-digit rounding noise (fund values = shares * price with repeating shares).
# A billionth of a dollar is far below a cent.
EPS = Decimal("1e-9")

Values = Mapping[str, Decimal]
Targets = Mapping[str, Decimal]


def _validate(values: Values, targets: Targets) -> None:
    if set(values) != set(targets):
        raise ValueError(f"values and targets must cover the same funds: {sorted(values)} vs {sorted(targets)}")
    if any(w <= 0 for w in targets.values()):
        raise ValueError("target weights must be positive")
    if sum(targets.values(), ZERO) != ONE:
        raise ValueError("target weights must sum to 1")
    if any(v < 0 for v in values.values()):
        raise ValueError("fund values must not be negative")


def core_value(values: Values) -> Decimal:
    return sum(values.values(), ZERO)


def weights(values: Values) -> dict[str, Decimal]:
    total = core_value(values)
    if total <= 0:
        raise ValueError("weights are undefined for an empty core")
    return {s: v / total for s, v in sorted(values.items())}


def band_edges(target: Decimal, band_width: Decimal) -> tuple[Decimal, Decimal]:
    """Lower and upper weight edges L_i = (1 - delta) w*_i and H_i = (1 + delta) w*_i."""
    return (ONE - band_width) * target, (ONE + band_width) * target


def breaches(values: Values, targets: Targets, band_width: Decimal) -> dict[str, str]:
    """Funds whose relative deviation |w - w*| / w* is strictly above the band.

    Compared in value terms (V_i against edge * V) so no division is needed.
    Returns {} for an empty core.
    """
    _validate(values, targets)
    total = core_value(values)
    out: dict[str, str] = {}
    if total == 0:
        return out
    for symbol in sorted(targets):
        low, high = band_edges(targets[symbol], band_width)
        if values[symbol] > high * total:
            out[symbol] = "high"
        elif values[symbol] < low * total:
            out[symbol] = "low"
    return out


def relative_deviation(values: Values, targets: Targets) -> dict[str, Decimal]:
    """(w_i - w*_i) / w*_i for every fund. Raises for an empty core."""
    _validate(values, targets)
    current = weights(values)
    return {s: (current[s] - targets[s]) / targets[s] for s in sorted(targets)}


# ---------------------------------------------------------------- contribution


@dataclass(frozen=True)
class Contribution:
    """Result of the contribution rule.

    deployed: how much of the cash is invested. Equals the cash unless a fund
        is blocked or capped (see contribution_rule).
    amounts:  exact buy per fund; sums to `deployed`.
    gaps:     s_i at the core actually produced, V + deployed.
    """

    deployed: Decimal
    amounts: dict[str, Decimal]
    gaps: dict[str, Decimal]


def contribution_gaps(values: Values, targets: Targets, cash: Decimal) -> dict[str, Decimal]:
    """s_i = max(0, w*_i (V + C) - V_i)."""
    _validate(values, targets)
    total = core_value(values) + cash
    return {s: max(ZERO, targets[s] * total - values[s]) for s in sorted(targets)}


def _water_fill(
    amount: Decimal, weights: Mapping[str, Decimal], caps: Mapping[str, Decimal | None]
) -> dict[str, Decimal]:
    """Split `amount` in proportion to `weights`, never giving a fund more than
    its cap; whatever a capped fund cannot take is re-split among the others.
    The caller guarantees sum of min(cap, weight) >= amount, so it all fits."""
    fixed: dict[str, Decimal] = {}
    free = [s for s in sorted(weights) if weights[s] > 0]
    trial: dict[str, Decimal] = {}
    while free:
        remaining = amount - sum(fixed.values(), ZERO)
        weight_sum = sum((weights[s] for s in free), ZERO)
        trial = {s: remaining * weights[s] / weight_sum for s in free}
        over = [s for s in free if caps[s] is not None and trial[s] > caps[s]]
        if not over:
            break
        for s in over:
            fixed[s] = caps[s]
        free = [s for s in free if s not in over]
    return {s: fixed.get(s, trial.get(s, ZERO)) for s in sorted(weights)}


def contribution_rule(
    values: Values,
    targets: Targets,
    cash: Decimal,
    blocked: frozenset[str] = frozenset(),
    max_buy: Decimal | None = None,
) -> Contribution:
    """b_i = C * s_i / sum_j s_j (STRATEGY.md §5), with per-fund limits.

    A fund in `blocked` (wash-sale window, not tradable) can take nothing and
    no fund can take more than `max_buy` (the order-size limit). A limited
    fund's share is never pushed into other funds: the rule deploys the largest
    d <= C such that every fund ends at or below its target relative to the
    core actually produced, V + d. The rest of the cash waits for the next run.
    With no blocked fund and no binding limit this is exactly the spec's
    formula and deploys all of C.

    absorb(d) = sum_i min(cap_i, max(0, w*_i (V + d) - V_i)) and h(d) = absorb(d) - d.
    h(0) >= 0 and h is continuous, piecewise linear and non-increasing (slope
    = sum of weights of uncapped funds with a positive gap - 1 <= 0), so
    {d : h(d) >= 0} is an interval [0, d*]. Without limits, h(C) = sum_j s_j - C >= 0.
    """
    _validate(values, targets)
    if cash < 0:
        raise ValueError("cash must not be negative")
    if max_buy is not None and max_buy < 0:
        raise ValueError("max_buy must not be negative")
    symbols = sorted(targets)
    total = core_value(values)
    cap: dict[str, Decimal | None] = {s: ZERO if s in blocked else max_buy for s in symbols}

    def gap(s: str, d: Decimal) -> Decimal:
        return max(ZERO, targets[s] * (total + d) - values[s])

    def take(s: str, d: Decimal) -> Decimal:
        limit = cap[s]
        return gap(s, d) if limit is None else min(limit, gap(s, d))

    def h(d: Decimal) -> Decimal:
        return sum((take(s, d) for s in symbols), ZERO) - d

    if cash == 0:
        deployed = ZERO
        amounts = {s: ZERO for s in symbols}
    elif h(cash) >= -EPS:
        deployed = cash
        amounts = _water_fill(cash, {s: gap(s, cash) for s in symbols}, cap)
    else:
        # Breakpoints: where a gap turns positive (t_i) and where it reaches its cap (u_i).
        points = {ZERO, cash}
        for s in symbols:
            points.add(values[s] / targets[s] - total)
            limit = cap[s]
            if limit is not None:
                points.add((limit + values[s]) / targets[s] - total)
        ordered = sorted(p for p in points if ZERO <= p <= cash)
        # h is linear between breakpoints and may be flat at zero before it turns
        # negative; the first segment that ends clearly below zero holds d*.
        low, high = next((lo, hi) for lo, hi in pairwise(ordered) if h(hi) < -EPS)
        h_low, h_high = h(low), h(high)
        deployed = min(cash, max(ZERO, low + h_low * (high - low) / (h_low - h_high)))
        amounts = {s: take(s, deployed) for s in symbols}
    return Contribution(deployed, amounts, {s: gap(s, deployed) for s in symbols})


def withdrawal_rule(values: Values, targets: Targets, amount: Decimal) -> dict[str, Decimal]:
    """Raise `amount` in cash without selling any fund below its post-withdrawal
    target: u_i = max(0, V_i - w*_i (V - W)), x_i = W u_i / sum_j u_j."""
    _validate(values, targets)
    total = core_value(values)
    if amount < 0 or amount > total:
        raise ValueError(f"withdrawal must be between 0 and the core value {total}, got {amount}")
    symbols = sorted(targets)
    if amount == 0:
        return {s: ZERO for s in symbols}
    excess = {s: max(ZERO, values[s] - targets[s] * (total - amount)) for s in symbols}
    excess_sum = sum(excess.values(), ZERO)
    return {s: amount * excess[s] / excess_sum for s in symbols}


# ---------------------------------------------------------------- rebalancing


def full_rebalance(values: Values, targets: Targets) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Sells e_j = max(0, V_j - w*_j V) and buys d_i = max(0, w*_i V - V_i)."""
    _validate(values, targets)
    total = core_value(values)
    symbols = sorted(targets)
    sells = {s: max(ZERO, values[s] - targets[s] * total) for s in symbols}
    buys = {s: max(ZERO, targets[s] * total - values[s]) for s in symbols}
    return sells, buys


@dataclass(frozen=True)
class BandEdgeSells:
    sells: dict[str, Decimal]
    x_over: Decimal
    x_under: Decimal
    total_deficit: Decimal


def band_edge_sells(values: Values, targets: Targets, band_width: Decimal) -> BandEdgeSells:
    """Smallest total sale after which, once the proceeds are redeployed by the
    contribution rule, every fund is inside its band (STRATEGY.md §6.5).

    X_over  = sum over funds above their upper edge of (V_j - H_j V)
    X_under = D * max over funds below their lower edge of (L_i V - V_i) / d_i
    X       = max(X_over, X_under)

    Funds above their upper edge first sell down to that edge; the remainder
    comes from every fund still above target, pro rata to remaining excess.
    """
    _validate(values, targets)
    symbols = sorted(targets)
    total = core_value(values)
    target_value = {s: targets[s] * total for s in symbols}
    deficit = {s: max(ZERO, target_value[s] - values[s]) for s in symbols}
    total_deficit = sum(deficit.values(), ZERO)

    over_min: dict[str, Decimal] = {}
    under_ratio: list[Decimal] = []
    for s in symbols:
        low, high = band_edges(targets[s], band_width)
        over_min[s] = values[s] - high * total if values[s] > high * total else ZERO
        if values[s] < low * total:
            under_ratio.append((low * total - values[s]) / deficit[s])

    x_over = sum(over_min.values(), ZERO)
    x_under = total_deficit * max(under_ratio) if under_ratio else ZERO
    rest = max(x_over, x_under) - x_over

    sells = dict(over_min)
    if rest > 0:
        remaining = {s: max(ZERO, values[s] - over_min[s] - target_value[s]) for s in symbols}
        remaining_sum = sum(remaining.values(), ZERO)
        for s in symbols:
            sells[s] += rest * remaining[s] / remaining_sum
    return BandEdgeSells(sells=sells, x_over=x_over, x_under=x_under, total_deficit=total_deficit)


def apply_trades(
    values: Values,
    buys: Mapping[str, Decimal] | None = None,
    sells: Mapping[str, Decimal] | None = None,
) -> dict[str, Decimal]:
    """Values after the given buys and sells. Rejects unknown funds and oversells."""
    out = dict(values)
    for symbol, amount in (buys or {}).items():
        if symbol not in out:
            raise ValueError(f"unknown fund {symbol}")
        out[symbol] += amount
    for symbol, amount in (sells or {}).items():
        if symbol not in out:
            raise ValueError(f"unknown fund {symbol}")
        if amount > out[symbol]:
            raise ValueError(f"cannot sell {amount} of {symbol}, only {out[symbol]} held")
        out[symbol] -= amount
    return out

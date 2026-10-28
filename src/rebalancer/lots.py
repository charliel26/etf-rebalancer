"""Tax-lot selection and the wash-sale guard (STRATEGY.md §7).

Pure functions: dates are arguments, nothing reads the clock.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from .models import Side, TaxLot, TradeRecord

ZERO = Decimal(0)
WASH_SALE_DAYS = 30
LOT_METHODS = ("fifo", "tax_aware")


# ---------------------------------------------------------------- holding period


def add_one_year(day: date) -> date:
    """Same calendar day next year; 29 February maps to 28 February."""
    try:
        return day.replace(year=day.year + 1)
    except ValueError:
        return date(day.year + 1, 2, 28)


def is_long_term(acquired: date, sale_date: date) -> bool:
    """Held more than one year (IRS Pub. 550): sold after the one-year anniversary."""
    return sale_date > add_one_year(acquired)


# ---------------------------------------------------------------- ordering


def _bucket(lot: TaxLot, price: Decimal, sale_date: date) -> int:
    """0 short-term loss or flat, 1 long-term loss or flat, 2 long-term gain, 3 short-term gain."""
    long_term = is_long_term(lot.acquired, sale_date)
    if price - lot.cost_per_share <= 0:
        return 1 if long_term else 0
    return 2 if long_term else 3


def order_lots(lots: Iterable[TaxLot], price: Decimal, sale_date: date, method: str) -> list[TaxLot]:
    """Relief order. fifo: oldest first. tax_aware: STRATEGY.md §7.2 buckets,
    largest loss / smallest gain per share first inside each bucket.
    Ties: older lot, then lot id."""
    if method == "fifo":
        return sorted(lots, key=lambda lot: (lot.acquired, lot.lot_id))
    if method == "tax_aware":
        return sorted(
            lots,
            key=lambda lot: (
                _bucket(lot, price, sale_date),
                price - lot.cost_per_share,
                lot.acquired,
                lot.lot_id,
            ),
        )
    raise ValueError(f"unknown lot method {method!r}; expected one of {LOT_METHODS}")


# ---------------------------------------------------------------- selection


@dataclass(frozen=True)
class LotPick:
    lot: TaxLot
    shares: Decimal
    gain: Decimal
    long_term: bool


@dataclass(frozen=True)
class LotSelection:
    picks: tuple[LotPick, ...]
    shares_requested: Decimal
    shares_selected: Decimal
    st_gain: Decimal
    lt_gain: Decimal
    blocked: tuple[tuple[str, str], ...]

    @property
    def unfilled(self) -> Decimal:
        return self.shares_requested - self.shares_selected

    @property
    def had_loss(self) -> bool:
        return any(pick.gain < 0 for pick in self.picks)


def _ineligible_reason(
    lot: TaxLot, price: Decimal, sale_date: date, recent_buy: bool, min_holding_days: int
) -> str | None:
    if (sale_date - lot.acquired).days < min_holding_days:
        return "MIN_HOLDING"
    if recent_buy and price < lot.cost_per_share:
        return "WASH_SALE"
    return None


def select_lots(
    lots: Sequence[TaxLot],
    shares: Decimal,
    price: Decimal,
    sale_date: date,
    method: str,
    *,
    recent_buy: bool,
    min_holding_days: int,
) -> LotSelection:
    """Choose which lots a sale of `shares` relieves.

    A lot is ineligible if it is younger than `min_holding_days`, or if it would
    realize a loss while `recent_buy` is set (wash-sale rule W1). tax_aware
    skips ineligible lots; fifo cannot skip (the broker relieves the oldest lot
    first), so it stops at the first ineligible lot.
    """
    if shares < 0:
        raise ValueError("shares must not be negative")
    if price <= 0:
        raise ValueError("price must be positive")
    picks: list[LotPick] = []
    blocked: list[tuple[str, str]] = []
    remaining = shares
    for lot in order_lots(lots, price, sale_date, method):
        if remaining <= 0:
            break
        reason = _ineligible_reason(lot, price, sale_date, recent_buy, min_holding_days)
        if reason is not None:
            blocked.append((lot.lot_id, reason))
            if method == "fifo":
                break
            continue
        take = min(lot.shares, remaining)
        long_term = is_long_term(lot.acquired, sale_date)
        picks.append(
            LotPick(lot=lot, shares=take, gain=take * (price - lot.cost_per_share), long_term=long_term)
        )
        remaining -= take
    st_gain = sum((p.gain for p in picks if not p.long_term), ZERO)
    lt_gain = sum((p.gain for p in picks if p.long_term), ZERO)
    return LotSelection(
        picks=tuple(picks),
        shares_requested=shares,
        shares_selected=shares - remaining,
        st_gain=st_gain,
        lt_gain=lt_gain,
        blocked=tuple(blocked),
    )


def remove_sold(lots: Sequence[TaxLot], picks: Iterable[LotPick]) -> tuple[TaxLot, ...]:
    """Lots left after the picks are sold; emptied lots are dropped, order kept."""
    sold: dict[str, Decimal] = {}
    for pick in picks:
        sold[pick.lot.lot_id] = sold.get(pick.lot.lot_id, ZERO) + pick.shares
    known = {lot.lot_id for lot in lots}
    unknown = set(sold) - known
    if unknown:
        raise ValueError(f"picks refer to unknown lots {sorted(unknown)}")
    out: list[TaxLot] = []
    for lot in lots:
        left = lot.shares - sold.get(lot.lot_id, ZERO)
        if left < 0:
            raise ValueError(f"lot {lot.lot_id} oversold by {-left}")
        if left > 0:
            out.append(replace(lot, shares=left))
    return tuple(out)


# ---------------------------------------------------------------- wash-sale guard


def group_of(symbol: str, groups: Iterable[frozenset[str]]) -> frozenset[str]:
    for group in groups:
        if symbol in group:
            return group
    return frozenset({symbol})


def within_window(earlier: date, later: date, days: int = WASH_SALE_DAYS) -> bool:
    """True if `earlier` is 0..days calendar days before `later` (both ends inclusive)."""
    return 0 <= (later - earlier).days <= days


def loss_sale_blocked(
    symbol: str, sale_date: date, trades: Iterable[TradeRecord], groups: Iterable[frozenset[str]]
) -> bool:
    """W1: no loss may be realized if the symbol (or its group) was bought in the last 30 days."""
    group = group_of(symbol, groups)
    return any(
        t.side == Side.BUY and t.symbol in group and within_window(t.trade_date, sale_date)
        for t in trades
    )


def buy_blocked(
    symbol: str, buy_date: date, trades: Iterable[TradeRecord], groups: Iterable[frozenset[str]]
) -> bool:
    """W2: no buy if the symbol (or its group) was sold at a loss in the last 30 days.
    Sales with unknown P/L (had_loss None) count as loss sales."""
    group = group_of(symbol, groups)
    return any(
        t.side == Side.SELL
        and t.symbol in group
        and t.had_loss is not False
        and within_window(t.trade_date, buy_date)
        for t in trades
    )

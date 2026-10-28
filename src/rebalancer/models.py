"""Plain data types shared by the planner, the guard and the backtester.

Money and share quantities are `Decimal`. Dates are `datetime.date`; timestamps
are timezone-aware `datetime.datetime`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    as_of: datetime


@dataclass(frozen=True)
class Holding:
    symbol: str
    shares: Decimal


@dataclass(frozen=True)
class TaxLot:
    lot_id: str
    symbol: str
    acquired: date
    shares: Decimal
    cost_per_share: Decimal


@dataclass(frozen=True)
class TradeRecord:
    """A past trade, used by the wash-sale guard.

    `had_loss` is True if any lot was sold at a loss, False if none was, and
    None when unknown (treated as a loss sale, which is the conservative side).
    It is ignored for buys.
    """

    symbol: str
    side: Side
    trade_date: date
    notional: Decimal
    had_loss: bool | None = None
    order_id: str | None = None


@dataclass(frozen=True)
class Tradability:
    symbol: str
    tradable: bool
    fractional: bool


@dataclass(frozen=True)
class Snapshot:
    """Everything the planner needs from the broker, captured at one moment."""

    as_of: datetime
    account_id: str | None
    cash: Decimal
    buying_power: Decimal | None
    holdings: tuple[Holding, ...]
    quotes: tuple[Quote, ...]
    lots: tuple[TaxLot, ...]
    open_order_ids: tuple[str, ...] = ()
    trades: tuple[TradeRecord, ...] = ()
    tradability: tuple[Tradability, ...] = ()
    reported_equity: Decimal | None = None
    closes: dict[str, tuple[tuple[date, Decimal], ...]] = field(default_factory=dict)

    def shares(self, symbol: str) -> Decimal:
        return sum((h.shares for h in self.holdings if h.symbol == symbol), Decimal(0))

    def quote(self, symbol: str) -> Quote | None:
        for quote in self.quotes:
            if quote.symbol == symbol:
                return quote
        return None

    def lots_for(self, symbol: str) -> tuple[TaxLot, ...]:
        return tuple(lot for lot in self.lots if lot.symbol == symbol)

    def tradability_for(self, symbol: str) -> Tradability | None:
        for item in self.tradability:
            if item.symbol == symbol:
                return item
        return None


@dataclass(frozen=True)
class Order:
    line_id: str
    symbol: str
    side: Side
    notional_usd: Decimal
    reason: str


@dataclass(frozen=True)
class Plan:
    """One run's decision. Serialized to plan.json by rebalancer.plan.plan_to_dict."""

    date: date
    run_id: str
    mode: str  # "live" or "dry_run"
    action: str
    requires_approval: bool
    orders: tuple[Order, ...]
    deferred_orders: tuple[Order, ...]
    checks: dict[str, bool]
    flags: tuple[str, ...]
    blockers: tuple[str, ...]
    tax_estimate: dict[str, Any]
    totals: dict[str, Decimal]
    portfolio: dict[str, Any]
    inputs: dict[str, str]
    plan_sha256: str

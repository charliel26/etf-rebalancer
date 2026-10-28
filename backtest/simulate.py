"""Event-driven daily simulation (STRATEGY.md §13.2-13.3).

The core strategies call the live decision code (rebalancer.plan.decide_trades,
rebalancer.engine, rebalancer.lots), so the backtest runs exactly the rules
that trade. Timing: decide at close t with data through t; execute at close
t+1 (or at the next open where one exists). Money is Decimal throughout.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

import numpy as np

from rebalancer import engine, lots
from rebalancer.config import DeploymentSchedule
from rebalancer.models import Side, TaxLot, TradeRecord
from rebalancer.money import CENT, ZERO, floor_cents
from rebalancer.plan import DecisionInputs, decide_trades, deployment_reserve

from .data import MarketData
from .params import BacktestParams
from .signal_study import below_ema
from .taxes import TaxBook

SHARE_Q = Decimal("0.00000001")


@dataclass(frozen=True)
class Strategy:
    name: str
    targets: Mapping[str, Decimal]
    band_width: Decimal | None = None
    annual_reset: bool = False
    ema_signal: bool = False
    lot_method: str = "tax_aware"
    st_cap_fraction: Decimal | None = None
    st_cap_fixed: Decimal | None = None
    series: Mapping[str, str] = field(default_factory=dict)  # target symbol -> market series name
    cash_yield: bool = False
    execution: str = "close"


@dataclass(frozen=True)
class Trade:
    day: date
    symbol: str
    side: str
    notional: Decimal
    st_gain: Decimal
    lt_gain: Decimal


@dataclass
class SimResult:
    strategy: str
    dates: list[date]
    values: list[float]
    flows: list[float]
    cash: list[float]
    invested_returns: np.ndarray
    weights: dict[str, list[float]]
    trades: list[Trade]
    taxes_paid: dict[int, float]
    dividends: float
    contributions: float
    after_tax_terminal: float
    liquidation_tax: float
    irr_flows: list[tuple[date, float]]
    wash_sale_violations: int
    events: Counter


class Simulation:
    def __init__(
        self,
        market: MarketData,
        strategy: Strategy,
        params: BacktestParams,
        contributions: Mapping[int, Decimal],
        start: int,
        end: int,
    ) -> None:
        if not 0 <= start < end < len(market.dates):
            raise ValueError("need 0 <= start < end < number of dates")
        self.m, self.s, self.p = market, strategy, params
        self.contributions = contributions
        self.start, self.end = start, end
        self.symbols = sorted(strategy.targets)
        self.series = {sym: strategy.series.get(sym, sym) for sym in self.symbols}
        self.c = params.cost_rate
        self.cash = ZERO
        self.lots: dict[str, list[TaxLot]] = {sym: [] for sym in self.symbols}
        self.held: dict[str, Decimal] = {sym: ZERO for sym in self.symbols}
        self.receivables: list[tuple[int, Decimal]] = []
        self.recent: deque[TradeRecord] = deque()
        self.trades: list[Trade] = []
        self.book = TaxBook(params.taxes)
        self.next_tax_year = market.dates[start].year
        self.last_reset_year = market.dates[start].year if strategy.annual_reset else None
        self.events: Counter = Counter()
        self.violations = 0
        self.lot_counter = 0
        self.schedule = DeploymentSchedule(market.dates[start], params.initial_capital, params.tranches)
        self.signals = (
            {sym: below_ema(market.tr_index[self.series[sym]], params.ema_span) for sym in self.symbols}
            if strategy.ema_signal
            else {}
        )
        if strategy.execution == "open" and any(
            market.open[self.series[sym]][t] is None for sym in self.symbols for t in range(start + 1, end + 1)
        ):
            raise ValueError("next-open execution needs real opening prices for every day of the window")

    # ------------------------------------------------------------ helpers

    def shares(self, sym: str) -> Decimal:
        return self.held[sym]

    def price(self, sym: str, t: int) -> Decimal:
        return self.m.price[self.series[sym]][t]

    def _prune(self, day: date) -> None:
        while self.recent and (day - self.recent[0].trade_date).days > 31:
            self.recent.popleft()

    def _buy(self, t: int, sym: str, notional: Decimal, price: Decimal) -> None:
        day = self.m.dates[t]
        notional = min(notional, floor_cents(max(self.cash, ZERO)))
        effective = price * (1 + self.c)
        shares = (notional / effective).quantize(SHARE_Q, rounding=ROUND_DOWN)
        if shares <= 0:
            return
        self.cash -= notional
        self.lot_counter += 1
        self.lots[sym].append(TaxLot(f"{sym}-{self.lot_counter}", sym, day, shares, effective))
        self.held[sym] += shares
        if any(
            r.side == Side.SELL and r.symbol == sym and r.had_loss and lots.within_window(r.trade_date, day)
            for r in self.recent
        ):
            self.violations += 1
        self.recent.append(TradeRecord(sym, Side.BUY, day, notional))
        self.trades.append(Trade(day, sym, "buy", notional, ZERO, ZERO))

    def _sell(self, t: int, sym: str, notional: Decimal, price: Decimal) -> None:
        day = self.m.dates[t]
        net = price * (1 - self.c)
        selection = lots.select_lots(
            self.lots[sym],
            notional / price,
            net,
            day,
            self.s.lot_method,
            recent_buy=lots.loss_sale_blocked(sym, day, self.recent, ()),
            min_holding_days=0,
        )
        if selection.unfilled > Decimal("0.000001"):
            self.events["sell_shortfall"] += 1
        if not selection.picks:
            return
        self.lots[sym] = list(lots.remove_sold(self.lots[sym], selection.picks))
        self.held[sym] -= selection.shares_selected
        proceeds = selection.shares_selected * net
        self.cash += proceeds
        self.book.realize(day.year, selection.st_gain, selection.lt_gain)
        if selection.had_loss and any(
            r.side == Side.BUY and r.symbol == sym and lots.within_window(r.trade_date, day) for r in self.recent
        ):
            self.violations += 1
        gross = selection.shares_selected * price
        self.recent.append(TradeRecord(sym, Side.SELL, day, gross, had_loss=selection.had_loss))
        self.trades.append(Trade(day, sym, "sell", gross, selection.st_gain, selection.lt_gain))

    def _reset_due(self, day: date) -> bool:
        if not self.s.annual_reset or self.last_reset_year is None:
            return False
        due = day >= date(day.year, self.p.reset_month, self.p.reset_day) and day.year > self.last_reset_year
        if due:
            self.last_reset_year = day.year
        return due

    # ------------------------------------------------------------ decisions

    def _decide(self, t: int, total_value: Decimal) -> list[tuple[str, str, Decimal]]:
        day = self.m.dates[t]
        prices = {sym: self.price(sym, t) for sym in self.symbols}
        values = {sym: self.shares(sym) * prices[sym] for sym in self.symbols}
        deployable = self.cash - deployment_reserve(self.schedule, day)
        buys: dict[str, Decimal] = {}
        sells: dict[str, Decimal] = {}
        if self.s.ema_signal:
            buys = self._ema_buys(t, values, deployable)
        else:
            cap = self.s.st_cap_fixed
            if self.s.st_cap_fraction is not None:
                cap = self.s.st_cap_fraction * total_value
            decision = decide_trades(
                DecisionInputs(
                    today=day,
                    targets=self.s.targets,
                    values=values,
                    prices=prices,
                    lots=self.lots,
                    deployable_cash=deployable,
                    trades=tuple(self.recent),
                    identical_groups=(),
                    band_width=self.s.band_width,
                    min_cash_to_deploy=self.p.min_cash_to_deploy,
                    min_order=self.p.min_order,
                    max_order=None,
                    daily_notional_left=None,
                    max_st_gain=cap,
                    lot_method=self.s.lot_method,
                    min_holding_days=0,
                    annual_reset=self._reset_due(day),
                )
            )
            buys = {k: v for k, v in decision.buys.items() if v > 0}
            sells = {k: v for k, v in decision.sells.items() if v > 0}
            for flag in decision.flags:
                self.events[flag.split(":")[0]] += 1
            if decision.rebalance != "none" and sells:
                self.events[f"rebalance_{decision.rebalance}"] += 1
        if self.cash < 0:  # tax payment left a shortfall: raise it with the withdrawal rule
            need = (-self.cash / (1 - self.c)).quantize(CENT, rounding=ROUND_CEILING) + CENT
            after = {sym: values[sym] - sells.get(sym, ZERO) for sym in self.symbols}
            available = sum(after.values(), ZERO)
            if available > 0:
                extra = engine.withdrawal_rule(after, self.s.targets, min(need, available))
                for sym, amount in extra.items():
                    if amount > 0:
                        sells[sym] = sells.get(sym, ZERO) + amount.quantize(CENT, rounding=ROUND_CEILING)
                self.events["tax_withdrawal"] += 1
        orders = [(sym, "sell", min(x, values[sym])) for sym, x in sorted(sells.items())]
        orders += [(sym, "buy", b) for sym, b in sorted(buys.items())]
        return orders

    def _ema_buys(self, t: int, values: dict[str, Decimal], deployable: Decimal) -> dict[str, Decimal]:
        """S5: restore each signalled fund to target as far as cash allows."""
        if deployable <= self.p.min_cash_to_deploy:
            return {}
        cash = floor_cents(deployable)
        day = self.m.dates[t]
        signalled = [
            sym
            for sym in self.symbols
            if self.signals[sym][t] and not lots.buy_blocked(sym, day, self.recent, ())  # wash-sale rule W2
        ]
        gaps = engine.contribution_gaps(values, self.s.targets, cash)
        wanted = {sym: gaps[sym] for sym in signalled if gaps[sym] > 0}
        total = sum(wanted.values(), ZERO)
        if total <= 0:
            return {}
        scale = min(Decimal(1), cash / total)
        buys = {sym: floor_cents(g * scale) for sym, g in wanted.items()}
        return {sym: b for sym, b in buys.items() if b >= self.p.min_order}

    # ------------------------------------------------------------ main loop

    def run(self) -> SimResult:
        m, p = self.m, self.p
        values, flows, cash_series, invested = [], [], [], []
        weights: dict[str, list[float]] = {sym: [] for sym in self.symbols}
        taxes_paid: dict[int, float] = {}
        dividends = contributions = ZERO
        irr_flows: list[tuple[date, float]] = []
        pending: list[tuple[str, str, Decimal]] = []
        final_total = ZERO
        for t in range(self.start, self.end + 1):
            day = m.dates[t]
            self._prune(day)
            flow = ZERO
            # Start-of-day holdings: invested return and dividend entitlement (ex-date t).
            prev_value = sum((self.shares(s) * self.price(s, t - 1) for s in self.symbols if t > self.start), ZERO)
            today_value = ZERO
            for sym in self.symbols:
                held = self.shares(sym)
                dist = m.dist[self.series[sym]][t]
                today_value += held * (self.price(sym, t) + dist)
                if dist > 0 and held > 0 and t > self.start:
                    self.receivables.append((t + p.dividend_lag, held * dist))
            invested.append(float(today_value / prev_value - 1) if prev_value > 0 else 0.0)
            # Execute yesterday's decisions.
            for sym, side, amount in pending:
                px = self.price(sym, t) if self.s.execution == "close" else m.open[self.series[sym]][t]
                if side == "sell":
                    self._sell(t, sym, amount, px)
                else:
                    self._buy(t, sym, amount, px)
            pending = []
            # Dividends paid today.
            due = [(i, a) for i, a in self.receivables if i <= t]
            self.receivables = [(i, a) for i, a in self.receivables if i > t]
            for _, amount in due:
                self.cash += amount
                self.book.dividend(day.year, amount)
                dividends += amount
            # Interest on cash.
            if self.s.cash_yield and self.cash > 0:
                interest = (self.cash * Decimal(repr(float(m.rf[t])))).quantize(CENT)
                self.cash += interest
                self.book.interest(day.year, interest)
            # External money in: initial capital on day 0, contributions after.
            incoming = p.initial_capital if t == self.start else self.contributions.get(t, ZERO)
            if incoming > 0:
                self.cash += incoming
                flow += incoming
                contributions += incoming
                irr_flows.append((day, -float(incoming)))
            # Taxes for completed years, paid on the first trading day on/after 15 April.
            while day >= date(self.next_tax_year + 1, p.taxes.pay_month, p.taxes.pay_day):
                tax = self.book.settle(self.next_tax_year)
                self.cash -= tax
                flow -= tax
                taxes_paid[self.next_tax_year] = float(tax)
                self.next_tax_year += 1
            # End of day.
            holdings = {sym: self.shares(sym) * self.price(sym, t) for sym in self.symbols}
            invested_total = sum(holdings.values(), ZERO)
            receivable_total = sum((a for _, a in self.receivables), ZERO)
            total = invested_total + self.cash + receivable_total
            values.append(float(total))
            final_total = total
            flows.append(float(flow))
            cash_series.append(float(self.cash))
            for sym in self.symbols:
                weights[sym].append(float(holdings[sym] / invested_total) if invested_total > 0 else float("nan"))
            if t < self.end:
                pending = self._decide(t, total)

        final_day = m.dates[self.end]
        extra_st = extra_lt = liquidation_cost = ZERO
        for sym in self.symbols:
            price = self.price(sym, self.end)
            for lot in self.lots[sym]:
                gain = lot.shares * (price * (1 - self.c) - lot.cost_per_share)
                if lots.is_long_term(lot.acquired, final_day):
                    extra_lt += gain
                else:
                    extra_st += gain
                liquidation_cost += lot.shares * price * self.c
        outstanding = sum((a for _, a in self.receivables), ZERO)
        self.book.dividend(final_day.year, outstanding)
        liquidation_tax = self.book.preview_liquidation(final_day.year, extra_st, extra_lt)
        after_tax = final_total - liquidation_cost - liquidation_tax
        irr_flows.append((final_day, float(after_tax)))
        return SimResult(
            strategy=self.s.name,
            dates=m.dates[self.start : self.end + 1],
            values=values,
            flows=flows,
            cash=cash_series,
            invested_returns=np.asarray(invested),
            weights=weights,
            trades=self.trades,
            taxes_paid=taxes_paid,
            dividends=float(dividends),
            contributions=float(contributions),
            after_tax_terminal=float(after_tax),
            liquidation_tax=float(liquidation_tax),
            irr_flows=irr_flows,
            wash_sale_violations=self.violations,
            events=self.events,
        )


def simulate(
    market: MarketData,
    strategy: Strategy,
    params: BacktestParams,
    contributions: Mapping[int, Decimal],
    start: int,
    end: int,
) -> SimResult:
    return Simulation(market, strategy, params, contributions, start, end).run()


def contributions_for_window(stream: Mapping[int, Decimal], start: int, end: int) -> dict[int, Decimal]:
    return {t: a for t, a in stream.items() if start < t <= end}

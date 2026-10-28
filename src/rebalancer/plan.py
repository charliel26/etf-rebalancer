"""Build a run's plan from a snapshot and the config (STRATEGY.md §5-§10).

Pure: no I/O and no clock. Dates and timestamps are arguments; the CLI writes
the files. `decide_trades` is the decision core and is shared with the
backtester, so the backtest runs exactly the live rules.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from . import engine, lots
from .config import Config, DeploymentSchedule
from .lots import LotSelection
from .models import Order, Plan, Side, Snapshot, TaxLot, TradeRecord
from .money import CENT, ZERO, allocate_cents, floor_cents

SCHEMA_VERSION = 1
NEW_YORK = ZoneInfo("America/New_York")
LOT_TOLERANCE = Decimal("0.000001")  # shares
SELL_REASONS = {
    "full": "band_rebalance",
    "band_edge": "band_edge_rebalance",
    "annual_reset": "annual_reset",
}


# ---------------------------------------------------------------- deployment schedule


def add_months(day: date, months: int) -> date:
    """Same day `months` later, clipped to the month's last day (31 Jan + 1 = 28/29 Feb)."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def tranche_dates(schedule: DeploymentSchedule) -> list[date]:
    if not schedule.enabled:
        return []
    assert schedule.start_date is not None
    return [add_months(schedule.start_date, k) for k in range(schedule.tranches)]


def deployment_reserve(schedule: DeploymentSchedule, today: date) -> Decimal:
    """Cash still held back on `today`: one tranche per tranche date after today.
    Works for any total; computed as total * pending / tranches to stay exact."""
    if not schedule.enabled:
        return ZERO
    assert schedule.total_usd is not None
    pending = sum(1 for d in tranche_dates(schedule) if d > today)
    return schedule.total_usd * pending / schedule.tranches


def deployment_status(schedule: DeploymentSchedule, today: date) -> list[dict[str, Any]]:
    if not schedule.enabled:
        return []
    assert schedule.total_usd is not None
    per = schedule.total_usd / schedule.tranches
    return [
        {"date": d.isoformat(), "amount_usd": per, "released": d <= today}
        for d in tranche_dates(schedule)
    ]


def annual_reset_due(today: date, month: int, day: int, completed_years: frozenset[int]) -> bool:
    return today >= date(today.year, month, day) and today.year not in completed_years


# ---------------------------------------------------------------- decision core


@dataclass(frozen=True)
class DecisionInputs:
    today: date
    targets: Mapping[str, Decimal]
    values: Mapping[str, Decimal]
    prices: Mapping[str, Decimal]
    lots: Mapping[str, Sequence[TaxLot]]
    deployable_cash: Decimal
    trades: Sequence[TradeRecord]
    identical_groups: tuple[frozenset[str], ...]
    band_width: Decimal | None  # None: never band-rebalance (backtest S2, S6)
    min_cash_to_deploy: Decimal
    min_order: Decimal
    max_order: Decimal | None  # None: no order-size limit (backtest)
    daily_notional_left: Decimal | None  # None: no daily limit (backtest)
    max_st_gain: Decimal | None  # None: never downgrade
    lot_method: str
    min_holding_days: int
    buy_blocked: frozenset[str] = frozenset()
    sell_blocked: frozenset[str] = frozenset()
    annual_reset: bool = False


@dataclass(frozen=True)
class Decision:
    buys: dict[str, Decimal]
    sells: dict[str, Decimal]
    deferred_buys: dict[str, Decimal]
    selections: dict[str, LotSelection]
    contribution_cash: Decimal
    contribution_deployed: Decimal
    contribution_allocated: Decimal
    dropped_buys: Decimal
    projected_total: Decimal
    rebalance: str  # "none", "full", "band_edge" or "annual_reset"
    breaches_now: dict[str, str]
    breaches_projected: dict[str, str]
    st_gain: Decimal
    lt_gain: Decimal
    requires_approval: bool
    flags: tuple[str, ...] = field(default=())

    @property
    def action(self) -> str:
        parts = []
        if any(b > 0 for b in self.buys.values()):
            parts.append("contribution")
        if any(x > 0 for x in self.sells.values()):
            parts.append(SELL_REASONS[self.rebalance])
        return "+".join(parts) or "none"


def _drop_small(amounts: dict[str, Decimal], minimum: Decimal, flags: list[str]) -> Decimal:
    """Zero out positive amounts below the minimum order, in place; return the total dropped."""
    dropped = ZERO
    for symbol, amount in amounts.items():
        if ZERO < amount < minimum:
            flags.append(f"BELOW_MIN_ORDER:{symbol}")
            dropped += amount
            amounts[symbol] = ZERO
    return dropped


def _select(inp: DecisionInputs, symbol: str, amount: Decimal) -> LotSelection:
    price = inp.prices[symbol]
    return lots.select_lots(
        inp.lots.get(symbol, ()),
        amount / price,
        price,
        inp.today,
        inp.lot_method,
        recent_buy=lots.loss_sale_blocked(symbol, inp.today, inp.trades, inp.identical_groups),
        min_holding_days=inp.min_holding_days,
    )


def _sell_legs(
    inp: DecisionInputs, target_sells: Mapping[str, Decimal], contribution_buys: Mapping[str, Decimal]
) -> tuple[dict[str, Decimal], dict[str, Decimal], dict[str, LotSelection], list[str], Decimal]:
    """Turn exact target sales into executable sell lines.

    Nets a sale against a same-day buy of the same fund (cheaper and avoids a
    wash sale), caps each sale by the eligible lots, the order limit and the
    daily budget left after buys, and drops lines below the minimum order.
    Returns (buys, sells, lot selections, flags, dropped buy amount).
    """
    symbols = sorted(inp.targets)
    flags: list[str] = []
    buys = dict(contribution_buys)
    target = dict(target_sells)
    for s in symbols:
        if buys[s] > 0 and target[s] > 0:
            netted = min(buys[s], floor_cents(target[s]))
            buys[s] -= netted
            target[s] -= netted
            flags.append(f"NETTED:{s}")
    dropped = _drop_small(buys, inp.min_order, flags)

    sells = {s: ZERO for s in symbols}
    for s in symbols:
        wanted = floor_cents(target[s])
        if wanted <= 0:
            continue
        if s in inp.sell_blocked:
            flags.append(f"SELL_BLOCKED:{s}")
            continue
        probe = _select(inp, s, wanted)
        amount = wanted
        if probe.unfilled > LOT_TOLERANCE:  # dollars -> shares -> dollars carries ~1e-25 noise
            amount = floor_cents(probe.shares_selected * inp.prices[s])
            flags.append(f"SELL_CONSTRAINED:{s}")
        if inp.max_order is not None and amount > inp.max_order:
            amount = inp.max_order
            flags.append(f"MAX_ORDER_CAP:{s}")
        sells[s] = amount

    if inp.daily_notional_left is not None:
        left = max(ZERO, inp.daily_notional_left - sum(buys.values(), ZERO))
        total = sum(sells.values(), ZERO)
        if total > left:
            flags.append("DAILY_LIMIT_SELLS")
            sells = {s: floor_cents(x * left / total) for s, x in sells.items()}
    _drop_small(sells, inp.min_order, flags)
    selections = {s: _select(inp, s, x) for s, x in sells.items() if x > 0}
    return buys, sells, selections, flags, dropped


def decide_trades(inp: DecisionInputs) -> Decision:
    """The run's trades (STRATEGY.md §6.1 order of operations).

    1. Contribution rule on deployable cash (wash-sale and order limits respected).
    2. Band test on the projected post-contribution values.
    3. If breached (or an annual reset is requested): full rebalance sales,
       downgraded to band-edge if the net short-term gain exceeds the cap.
    4. Sale proceeds are redeployed at the next run; the buy legs are returned
       as deferred buys (they sum exactly to the sales).
    """
    symbols = sorted(inp.targets)
    flags: list[str] = []
    band = inp.band_width
    breaches_now = engine.breaches(inp.values, inp.targets, band) if band is not None else {}

    # 1. Contribution
    buys = {s: ZERO for s in symbols}
    contribution_cash = contribution_deployed = contribution_allocated = dropped = ZERO
    if inp.deployable_cash > inp.min_cash_to_deploy:
        cash = inp.deployable_cash
        if inp.daily_notional_left is not None and cash > inp.daily_notional_left:
            cash = inp.daily_notional_left
            flags.append("DAILY_LIMIT_CONTRIBUTION")
        contribution_cash = floor_cents(cash)
        blocked = set(inp.buy_blocked)
        for s in symbols:
            if lots.buy_blocked(s, inp.today, inp.trades, inp.identical_groups):
                blocked.add(s)
                flags.append(f"WASH_SALE_BUY_BLOCK:{s}")
        contribution = engine.contribution_rule(
            inp.values, inp.targets, contribution_cash, frozenset(blocked), inp.max_order
        )
        contribution_deployed = floor_cents(contribution.deployed)
        if contribution_deployed > 0:
            buys = allocate_cents(contribution.amounts, contribution_deployed)
        contribution_allocated = sum(buys.values(), ZERO)
        if contribution_deployed < contribution_cash:
            flags.append("CASH_WAITING_FOR_LIMITED_FUNDS")
        dropped = _drop_small(buys, inp.min_order, flags)

    # 2. Projected band test
    projected = engine.apply_trades(inp.values, buys=buys)
    projected_total = engine.core_value(projected)
    breaches_projected = engine.breaches(projected, inp.targets, band) if band is not None else {}

    # 3. Sales
    rebalance = "annual_reset" if inp.annual_reset else ("full" if breaches_projected else "none")
    sells = {s: ZERO for s in symbols}
    selections: dict[str, LotSelection] = {}
    requires_approval = False
    if rebalance != "none":
        contribution_buys = dict(buys)
        full_sells, _ = engine.full_rebalance(projected, inp.targets)
        buys, sells, selections, sale_flags, extra = _sell_legs(inp, full_sells, contribution_buys)
        st_gain = sum((x.st_gain for x in selections.values()), ZERO)
        if rebalance == "full" and inp.max_st_gain is not None and st_gain > inp.max_st_gain:
            assert band is not None
            edge = engine.band_edge_sells(projected, inp.targets, band).sells
            buys, sells, selections, sale_flags, extra = _sell_legs(inp, edge, contribution_buys)
            sale_flags.insert(0, "BAND_EDGE_DOWNGRADE")
            rebalance = "band_edge"
            st_gain = sum((x.st_gain for x in selections.values()), ZERO)
            if st_gain > inp.max_st_gain:
                requires_approval = True
                sale_flags.append("ST_CAP_EXCEEDED")
        if rebalance == "annual_reset":
            requires_approval = True
            if inp.max_st_gain is not None and st_gain > inp.max_st_gain:
                sale_flags.append("ST_CAP_EXCEEDED")
        flags.extend(sale_flags)
        dropped += extra

    # 4. Deferred redeployment of sale proceeds
    proceeds = sum(sells.values(), ZERO)
    deferred = {s: ZERO for s in symbols}
    if proceeds > 0:
        after = engine.apply_trades(inp.values, buys=buys, sells=sells)
        redeploy = engine.contribution_rule(after, inp.targets, proceeds)
        deferred = allocate_cents(redeploy.amounts, proceeds)

    return Decision(
        buys=buys,
        sells=sells,
        deferred_buys=deferred,
        selections=selections,
        contribution_cash=contribution_cash,
        contribution_deployed=contribution_deployed,
        contribution_allocated=contribution_allocated,
        dropped_buys=dropped,
        projected_total=projected_total,
        rebalance=rebalance,
        breaches_now=breaches_now,
        breaches_projected=breaches_projected,
        st_gain=sum((x.st_gain for x in selections.values()), ZERO),
        lt_gain=sum((x.lt_gain for x in selections.values()), ZERO),
        requires_approval=requires_approval,
        flags=tuple(flags),
    )


# ---------------------------------------------------------------- live plan


@dataclass(frozen=True)
class PlanContext:
    """Everything build_plan needs besides the snapshot and the config."""

    today: date
    now: datetime  # timezone-aware
    live_requested: bool
    kill_switch: bool = False
    live_run_exists_today: bool = False
    notional_used_today: Decimal = ZERO
    completed_reset_years: frozenset[int] = frozenset()
    annual_reset_requested: bool = False
    snapshot_sha256: str = ""
    config_sha256: str = ""
    code_version: str = ""


def _checks(
    decision: Decision,
    orders: Sequence[Order],
    values: Mapping[str, Decimal],
    inp: DecisionInputs,
    targets: Mapping[str, Decimal],
    max_order: Decimal,
    daily_left: Decimal,
) -> dict[str, bool]:
    """Invariants every plan must satisfy; any False blocks execution.
    Written to return False, never raise, for an order it does not recognize."""
    buys = {o.symbol: o.notional_usd for o in orders if o.side == Side.BUY}
    sells = {o.symbol: o.notional_usd for o in orders if o.side == Side.SELL}
    new_core = sum(values.values(), ZERO) + sum(buys.values(), ZERO)
    tolerance = CENT * len(targets) + decision.dropped_buys
    selections = decision.selections

    def recent_loss_sale(s: str) -> bool:
        recent_buy = lots.loss_sale_blocked(s, inp.today, inp.trades, inp.identical_groups)
        return recent_buy and s in selections and selections[s].had_loss

    return {
        "targets_sum_to_one": sum(targets.values(), ZERO) == 1,
        "orders_whitelisted": all(o.symbol in targets for o in orders),
        "orders_within_size_limits": all(inp.min_order <= o.notional_usd <= max_order for o in orders),
        "daily_notional_within_limit": sum((o.notional_usd for o in orders), ZERO) <= daily_left,
        "contribution_sum_matches": decision.contribution_allocated == decision.contribution_deployed,
        "deferred_buys_equal_sells": sum(decision.deferred_buys.values(), ZERO)
        == sum(decision.sells.values(), ZERO),
        "no_symbol_bought_and_sold": not (set(buys) & set(sells)),
        "sells_covered_by_eligible_lots": all(
            s in selections and selections[s].unfilled <= LOT_TOLERANCE for s in sells
        ),
        "no_buy_above_target": all(
            s in targets and values[s] + b <= targets[s] * new_core + tolerance for s, b in buys.items()
        ),
        "no_sell_below_target": all(
            s in targets and values[s] - x >= targets[s] * decision.projected_total - tolerance
            for s, x in sells.items()
        ),
        "wash_sale_clear": all(
            not lots.buy_blocked(s, inp.today, inp.trades, inp.identical_groups) for s in buys
        )
        and not any(recent_loss_sale(s) for s in sells),
    }


def build_plan(snapshot: Snapshot, config: Config, ctx: PlanContext) -> Plan:
    strategy, limits = config.strategy, config.limits
    targets = strategy.targets
    symbols = sorted(targets)
    blockers: list[str] = []
    flags: list[str] = []

    if ctx.kill_switch:
        blockers.append("KILL_SWITCH")
    if ctx.live_requested and ctx.live_run_exists_today:
        blockers.append("LIVE_RUN_EXISTS_TODAY")
    if ctx.live_requested and strategy.lot_method != "fifo":
        blockers.append("LOT_METHOD_NOT_SUPPORTED_LIVE")
    if snapshot.open_order_ids:
        blockers.append("OPEN_ORDERS")
    if snapshot.as_of.astimezone(NEW_YORK).date() != ctx.today:
        blockers.append("SNAPSHOT_NOT_FROM_TODAY")
    if snapshot.cash < 0:
        blockers.append("NEGATIVE_CASH")

    prices: dict[str, Decimal] = {}
    for s in symbols:
        quote = snapshot.quote(s)
        if quote is None or quote.price <= 0:
            blockers.append(f"MISSING_QUOTE:{s}")
            continue
        if ctx.now - quote.as_of > limits.quote_max_age:
            blockers.append(f"STALE_QUOTE:{s}")
        prices[s] = quote.price

    # Values of everything held.
    other_values: dict[str, Decimal] = {}
    for holding in snapshot.holdings:
        if holding.symbol in targets or holding.shares == 0:
            continue
        quote = snapshot.quote(holding.symbol)
        if quote is None:
            flags.append(f"UNPRICED:{holding.symbol}")
            continue
        other_values[holding.symbol] = other_values.get(holding.symbol, ZERO) + holding.shares * quote.price
    satellite_value = sum(
        (v for s, v in other_values.items() if s in strategy.satellite_symbols), ZERO
    )
    unmanaged = sorted(s for s in other_values if s not in strategy.satellite_symbols)
    flags.extend(f"UNMANAGED_POSITION:{s}" for s in unmanaged)
    unmanaged_value = sum((other_values[s] for s in unmanaged), ZERO)

    cash = snapshot.cash if snapshot.buying_power is None else min(snapshot.cash, snapshot.buying_power)
    cash = max(cash, ZERO)
    reserve = strategy.cash_reserve + deployment_reserve(strategy.deployment, ctx.today)
    deployable = cash - reserve
    if deployable < 0:
        flags.append("CASH_BELOW_RESERVE")

    buy_blocked: set[str] = set()
    sell_blocked: set[str] = set()
    for s in symbols:
        info = snapshot.tradability_for(s)
        if info is None or not info.tradable or not info.fractional:
            reason = "TRADABILITY_UNKNOWN" if info is None else "NOT_TRADABLE_IN_DOLLARS"
            flags.append(f"{reason}:{s}")
            buy_blocked.add(s)
            sell_blocked.add(s)
        lot_shares = sum((x.shares for x in snapshot.lots_for(s)), ZERO)
        if abs(lot_shares - snapshot.shares(s)) > LOT_TOLERANCE:
            flags.append(f"LOT_MISMATCH:{s}")
            sell_blocked.add(s)

    daily_left = max(ZERO, limits.max_daily_notional - ctx.notional_used_today)
    reset_due = annual_reset_due(
        ctx.today, strategy.annual_reset_month, strategy.annual_reset_day, ctx.completed_reset_years
    )
    if ctx.annual_reset_requested and not reset_due:
        flags.append("ANNUAL_RESET_NOT_DUE")

    funds: dict[str, Any] = {}
    decision: Decision | None = None
    orders: list[Order] = []
    deferred: list[Order] = []
    checks: dict[str, bool] = {}
    if len(prices) == len(symbols):
        values = {s: snapshot.shares(s) * prices[s] for s in symbols}
        inp = DecisionInputs(
            today=ctx.today,
            targets=targets,
            values=values,
            prices=prices,
            lots={s: snapshot.lots_for(s) for s in symbols},
            deployable_cash=deployable,
            trades=snapshot.trades,
            identical_groups=strategy.identical_groups,
            band_width=strategy.band_width,
            min_cash_to_deploy=strategy.min_cash_to_deploy,
            min_order=limits.min_order,
            max_order=limits.max_order,
            daily_notional_left=daily_left,
            max_st_gain=limits.max_st_gain,
            lot_method=strategy.lot_method,
            min_holding_days=limits.min_holding_days,
            buy_blocked=frozenset(buy_blocked),
            sell_blocked=frozenset(sell_blocked),
            annual_reset=ctx.annual_reset_requested,
        )
        decision = decide_trades(inp)
        flags.extend(decision.flags)
        sell_reason = SELL_REASONS.get(decision.rebalance, "none")
        for s in symbols:
            if decision.sells[s] > 0:
                orders.append(Order(f"L{len(orders) + 1}", s, Side.SELL, decision.sells[s], sell_reason))
        for s in symbols:
            if decision.buys[s] > 0:
                orders.append(Order(f"L{len(orders) + 1}", s, Side.BUY, decision.buys[s], "contribution"))
        for s in symbols:
            if decision.deferred_buys[s] > 0:
                deferred.append(
                    Order(f"D{len(deferred) + 1}", s, Side.BUY, decision.deferred_buys[s], "redeploy_sale_proceeds")
                )
        checks = _checks(decision, orders, values, inp, targets, limits.max_order, daily_left)
        blockers.extend(f"CHECK_FAILED:{name}" for name, ok in checks.items() if not ok)
        core = engine.core_value(values)
        projected = engine.apply_trades(values, buys=decision.buys)
        for s in symbols:
            low, high = engine.band_edges(targets[s], strategy.band_width)
            funds[s] = {
                "value": values[s],
                "price": prices[s],
                "shares": snapshot.shares(s),
                "weight": values[s] / core if core > 0 else None,
                "projected_weight": projected[s] / decision.projected_total
                if decision.projected_total > 0
                else None,
                "target": targets[s],
                "band_low": low,
                "band_high": high,
                "breach": decision.breaches_now.get(s),
            }

    requires_approval = bool(decision and decision.requires_approval) or (
        limits.approval_for_all_trades and bool(orders)
    )
    mode = "live" if ctx.live_requested and not blockers else "dry_run"
    core_value = sum((f["value"] for f in funds.values()), ZERO)
    total_value = core_value + satellite_value + unmanaged_value + cash
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "date": ctx.today.isoformat(),
        "mode": mode,
        "action": decision.action if decision else "none",
        "requires_approval": requires_approval,
        "orders": [_order_dict(o) for o in orders],
        "deferred_orders": [_order_dict(o) for o in deferred],
        "checks": checks,
        "flags": sorted(set(flags)),
        "blockers": blockers,
        "tax_estimate": _tax_estimate(decision, strategy.lot_method),
        "totals": {
            "buys": sum((o.notional_usd for o in orders if o.side == Side.BUY), ZERO),
            "sells": sum((o.notional_usd for o in orders if o.side == Side.SELL), ZERO),
        },
        "portfolio": {
            "core_value": core_value,
            "satellite_value": satellite_value,
            "unmanaged_value": unmanaged_value,
            "cash": cash,
            "total_value": total_value,
            "satellite_share": satellite_value / total_value if total_value > 0 else None,
            "reserve": reserve,
            "deployable_cash": deployable,
            "contribution_cash": decision.contribution_cash if decision else ZERO,
            "contribution_deployed": decision.contribution_deployed if decision else ZERO,
            "rebalance": decision.rebalance if decision else "none",
            "breaches_projected": decision.breaches_projected if decision else {},
            "funds": funds,
            "deployment": deployment_status(strategy.deployment, ctx.today),
            "annual_reset_due": reset_due,
        },
        "inputs": {
            "snapshot_sha256": ctx.snapshot_sha256,
            "config_sha256": ctx.config_sha256,
            "code_version": ctx.code_version,
            "created_at": ctx.now.isoformat(),
        },
    }
    finished = finalize(body)
    return Plan(
        date=ctx.today,
        run_id=finished["run_id"],
        mode=mode,
        action=finished["action"],
        requires_approval=requires_approval,
        orders=tuple(orders),
        deferred_orders=tuple(deferred),
        checks=checks,
        flags=tuple(finished["flags"]),
        blockers=tuple(blockers),
        tax_estimate=finished["tax_estimate"],
        totals=body["totals"],
        portfolio=finished["portfolio"],
        inputs=finished["inputs"],
        plan_sha256=finished["plan_sha256"],
    )


def _order_dict(order: Order) -> dict[str, Any]:
    return {
        "line_id": order.line_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "notional_usd": order.notional_usd,
        "reason": order.reason,
    }


def _tax_estimate(decision: Decision | None, lot_method: str) -> dict[str, Any]:
    if decision is None:
        return {"lot_method": lot_method, "st_gain_usd": ZERO, "lt_gain_usd": ZERO, "lots": {}}
    return {
        "lot_method": lot_method,
        "st_gain_usd": decision.st_gain.quantize(CENT),
        "lt_gain_usd": decision.lt_gain.quantize(CENT),
        "lots": {
            s: [
                {
                    "lot_id": p.lot.lot_id,
                    "shares": p.shares.quantize(Decimal("0.000001")),
                    "gain_usd": p.gain.quantize(CENT),
                    "long_term": p.long_term,
                }
                for p in sel.picks
            ]
            for s, sel in sorted(decision.selections.items())
        },
    }


# ---------------------------------------------------------------- serialization


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def canonical_json(obj: Any) -> str:
    return json.dumps(_jsonable(obj), sort_keys=True, separators=(",", ":"), allow_nan=False)


def plan_digest(plan: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the plan without its own hash field."""
    content = {k: v for k, v in plan.items() if k != "plan_sha256"}
    return hashlib.sha256(canonical_json(content).encode()).hexdigest()


def finalize(body: Mapping[str, Any]) -> dict[str, Any]:
    """Make a body JSON-ready, give it a run id and seal it with plan_sha256."""
    plain = json.loads(canonical_json(body))
    content = {k: v for k, v in plain.items() if k not in ("run_id", "plan_sha256")}
    short = hashlib.sha256(canonical_json(content).encode()).hexdigest()[:8]
    plain["run_id"] = f"{plain['date']}-{plain['mode']}-{short}"
    plain["plan_sha256"] = plan_digest(plain)
    return plain


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    """The plan.json document (already JSON-ready, sealed)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "date": plan.date.isoformat(),
        "run_id": plan.run_id,
        "mode": plan.mode,
        "action": plan.action,
        "requires_approval": plan.requires_approval,
        "orders": [_jsonable(_order_dict(o)) for o in plan.orders],
        "deferred_orders": [_jsonable(_order_dict(o)) for o in plan.deferred_orders],
        "checks": dict(plan.checks),
        "flags": list(plan.flags),
        "blockers": list(plan.blockers),
        "tax_estimate": plan.tax_estimate,
        "totals": _jsonable(plan.totals),
        "portfolio": plan.portfolio,
        "inputs": plan.inputs,
        "plan_sha256": plan.plan_sha256,
    }


def verify_plan_dict(document: Mapping[str, Any]) -> bool:
    """True if the document's plan_sha256 matches its content."""
    return document.get("plan_sha256") == plan_digest(document)

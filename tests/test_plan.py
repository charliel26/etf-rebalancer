"""Tests for rebalancer.plan. Expected numbers are worked out by hand in comments."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from rebalancer import plan as planmod
from rebalancer.config import DeploymentSchedule
from rebalancer.models import Holding, Order, Side, TaxLot, Tradability, TradeRecord
from rebalancer.plan import (
    DecisionInputs,
    PlanContext,
    add_months,
    annual_reset_due,
    build_plan,
    canonical_json,
    decide_trades,
    deployment_reserve,
    deployment_status,
    finalize,
    plan_to_dict,
    tranche_dates,
    verify_plan_dict,
)
from tests.builders import NOW, OLD, PRICES, TODAY, make_snapshot, on_target_shares, vals
from tests.conftest import TARGETS, D, make_config


def lot_for(symbol, value, cost=None, acquired=OLD, lot_id=None):
    price = PRICES[symbol]
    return TaxLot(
        lot_id=lot_id or f"{symbol}-1",
        symbol=symbol,
        acquired=acquired,
        shares=D(value) / price,
        cost_per_share=price if cost is None else D(cost),
    )


def inputs(values, cash=0, lots_map=None, **overrides):
    if lots_map is None:
        lots_map = {s: (lot_for(s, v),) if v > 0 else () for s, v in values.items()}
    base = {
        "today": TODAY,
        "targets": TARGETS,
        "values": values,
        "prices": PRICES,
        "lots": lots_map,
        "deployable_cash": D(cash),
        "trades": (),
        "identical_groups": (),
        "band_width": D("0.2"),
        "min_cash_to_deploy": D(25),
        "min_order": D(1),
        "max_order": D(1500),
        "daily_notional_left": D(3000),
        "max_st_gain": D(100),
        "lot_method": "fifo",
        "min_holding_days": 0,
    }
    base.update(overrides)
    return DecisionInputs(**base)


def zeros():
    return {s: D(0) for s in TARGETS}


# ---------------------------------------------------------------- schedule helpers


class TestDeploymentSchedule:
    def test_add_months_clips_and_wraps(self):
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
        assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
        assert add_months(date(2026, 3, 31), 0) == date(2026, 3, 31)

    def test_tranche_dates_and_reserve(self):
        schedule = DeploymentSchedule(start_date=date(2026, 1, 31), total_usd=D(8100), tranches=3)
        assert tranche_dates(schedule) == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
        assert deployment_reserve(schedule, date(2026, 1, 30)) == D(8100)
        assert deployment_reserve(schedule, date(2026, 1, 31)) == D(5400)
        assert deployment_reserve(schedule, date(2026, 2, 27)) == D(5400)
        assert deployment_reserve(schedule, date(2026, 2, 28)) == D(2700)
        assert deployment_reserve(schedule, date(2026, 3, 31)) == D(0)

    def test_arbitrary_amount_stays_exact(self):
        schedule = DeploymentSchedule(start_date=TODAY, total_usd=D(100), tranches=3)
        assert deployment_reserve(schedule, TODAY - timedelta(days=1)) == D(100)
        assert deployment_reserve(schedule, TODAY) == D(200) / D(3)

    def test_status(self):
        schedule = DeploymentSchedule(start_date=TODAY, total_usd=D(900), tranches=3)
        status = deployment_status(schedule, TODAY)
        assert [s["released"] for s in status] == [True, False, False]
        assert status[0]["amount_usd"] == D(300)

    def test_disabled(self):
        schedule = DeploymentSchedule(start_date=None, total_usd=D(900), tranches=3)
        assert tranche_dates(schedule) == []
        assert deployment_reserve(schedule, TODAY) == 0
        assert deployment_status(schedule, TODAY) == []

    def test_annual_reset_due(self):
        assert not annual_reset_due(date(2027, 1, 14), 1, 15, frozenset())
        assert annual_reset_due(date(2027, 1, 15), 1, 15, frozenset())
        assert annual_reset_due(date(2027, 6, 1), 1, 15, frozenset({2026}))
        assert not annual_reset_due(date(2027, 6, 1), 1, 15, frozenset({2027}))


# ---------------------------------------------------------------- decide_trades: buying


class TestContribution:
    def test_nothing_to_do(self):
        d = decide_trades(inputs(vals(5000, 2000, 1500, 1500)))
        assert d.action == "none"
        assert d.buys == zeros() and d.sells == zeros() and d.deferred_buys == zeros()

    def test_initial_tranche_buys_target_weights(self):
        d = decide_trades(inputs(vals(), cash=2700))
        assert d.buys == {"AVUV": D(405), "QQQM": D(405), "VTI": D(1350), "VXUS": D(540)}
        assert d.contribution_cash == d.contribution_deployed == d.contribution_allocated == D(2700)
        assert d.action == "contribution"

    def test_threshold_is_strict(self):
        assert decide_trades(inputs(vals(), cash=25)).action == "none"
        assert decide_trades(inputs(vals(), cash="25.01")).action == "contribution"

    def test_daily_limit_caps_contribution(self):
        d = decide_trades(inputs(vals(), cash=5000))
        assert d.contribution_cash == D(3000)
        assert d.buys == {"AVUV": D(450), "QQQM": D(450), "VTI": D(1500), "VXUS": D(600)}
        assert "DAILY_LIMIT_CONTRIBUTION" in d.flags

    def test_order_limit_keeps_target_proportions(self):
        # VTI capped at 1000 -> only 2000 can go in without overweighting the rest.
        d = decide_trades(inputs(vals(), cash=3000, max_order=D(1000)))
        assert d.buys == {"AVUV": D(300), "QQQM": D(300), "VTI": D(1000), "VXUS": D(400)}
        assert d.contribution_deployed == D(2000)
        assert "CASH_WAITING_FOR_LIMITED_FUNDS" in d.flags

    def test_wash_sale_block_on_target_portfolio_holds_cash(self):
        loss_sale = TradeRecord("VXUS", Side.SELL, TODAY - timedelta(days=10), D(500), had_loss=True)
        d = decide_trades(inputs(vals(5000, 2000, 1500, 1500), cash=1000, trades=(loss_sale,)))
        assert "WASH_SALE_BUY_BLOCK:VXUS" in d.flags
        assert d.contribution_deployed == 0
        assert d.buys == zeros()

    def test_wash_sale_block_irrelevant_when_fund_over_target(self):
        loss_sale = TradeRecord("VXUS", Side.SELL, TODAY - timedelta(days=10), D(500), had_loss=True)
        d = decide_trades(inputs(vals(4800, 2200, 1500, 1500), cash=1000, trades=(loss_sale,)))
        assert d.buys == {"AVUV": D(150), "QQQM": D(150), "VTI": D(700), "VXUS": D(0)}

    def test_external_buy_block(self):
        d = decide_trades(inputs(vals(4800, 2200, 1500, 1500), cash=1000, buy_blocked=frozenset({"VTI"})))
        assert d.buys["VTI"] == 0

    def test_tiny_buy_dropped_below_min_order(self):
        # Gaps at 10029.5: VTI 14.75, VXUS 5.9, QQQM 4.425, AVUV 0.925 (sum 26 = C).
        # Cents: 14.75, 5.90, 4.43 (tie on .5 goes to the larger weight), 0.92 -> dropped.
        d = decide_trades(inputs(vals(5000, 2000, 1500, "1503.5"), cash=26))
        assert d.buys == {"AVUV": D(0), "QQQM": D("4.43"), "VTI": D("14.75"), "VXUS": D("5.90")}
        assert d.dropped_buys == D("0.92")
        assert d.contribution_allocated == D(26)
        assert "BELOW_MIN_ORDER:AVUV" in d.flags

    def test_contribution_covers_the_gap(self):
        # VXUS at 15% breaches low now; after buying, no fund breaches -> nothing sold.
        d = decide_trades(inputs(vals(5000, 1500, 1750, 1750), cash=1000))
        assert d.breaches_now == {"VXUS": "low"}
        assert d.breaches_projected == {}
        assert d.buys == {"AVUV": D(0), "QQQM": D(0), "VTI": D("416.67"), "VXUS": D("583.33")}
        assert d.sells == zeros()
        assert d.rebalance == "none"


# ---------------------------------------------------------------- decide_trades: selling

BREACHED = vals(6200, 1600, 1100, 1100)  # VTI 62% > 60%; QQQM, AVUV 11% < 12%


class TestBandRebalance:
    def test_full_rebalance_when_gain_is_small(self):
        d = decide_trades(inputs(BREACHED))
        assert d.rebalance == "full"
        assert d.sells == {"AVUV": D(0), "QQQM": D(0), "VTI": D(1200), "VXUS": D(0)}
        assert d.deferred_buys == {"AVUV": D(400), "QQQM": D(400), "VTI": D(0), "VXUS": D(400)}
        assert d.st_gain == 0 and d.lt_gain == 0
        assert d.action == "band_rebalance"
        assert not d.requires_approval

    def test_downgrade_to_band_edge(self):
        # Full sale: 4 VTI shares x 50 gain = 200 > 100. Band edge: X = max(200, 300) = 300,
        # 1 share x 50 = 50 <= 100. Proceeds go 100/100/100 to the three underweight funds.
        lots_map = {"VTI": (lot_for("VTI", 6200, cost=250, acquired=date(2026, 9, 1)),)}
        d = decide_trades(inputs(BREACHED, lots_map=lots_map))
        assert d.rebalance == "band_edge"
        assert d.sells["VTI"] == D(300)
        assert d.st_gain == D(50)
        assert d.deferred_buys == {"AVUV": D(100), "QQQM": D(100), "VTI": D(0), "VXUS": D(100)}
        assert "BAND_EDGE_DOWNGRADE" in d.flags
        assert not d.requires_approval
        assert d.action == "band_edge_rebalance"

    def test_band_edge_still_over_cap_needs_approval(self):
        lots_map = {"VTI": (lot_for("VTI", 6200, cost=150, acquired=date(2026, 9, 1)),)}
        d = decide_trades(inputs(BREACHED, lots_map=lots_map))
        assert d.rebalance == "band_edge"
        assert d.st_gain == D(150)
        assert d.requires_approval
        assert {"BAND_EDGE_DOWNGRADE", "ST_CAP_EXCEEDED"} <= set(d.flags)

    def test_no_cap_means_no_downgrade(self):
        lots_map = {"VTI": (lot_for("VTI", 6200, cost=100, acquired=date(2026, 9, 1)),)}
        d = decide_trades(
            inputs(BREACHED, lots_map=lots_map, max_st_gain=None, max_order=None, daily_notional_left=None)
        )
        assert d.rebalance == "full"
        assert d.sells["VTI"] == D(1200)

    def test_wash_sale_blocks_fifo_sale(self):
        lots_map = {
            "VTI": (
                lot_for("VTI", 3100, cost=400, acquired=date(2025, 1, 2), lot_id="old-loss"),
                lot_for("VTI", 3100, cost=200, acquired=date(2025, 6, 2), lot_id="newer-gain"),
            )
        }
        recent_buy = TradeRecord("VTI", Side.BUY, TODAY - timedelta(days=5), D(100))
        d = decide_trades(inputs(BREACHED, lots_map=lots_map, trades=(recent_buy,)))
        assert d.sells == zeros()
        assert "SELL_CONSTRAINED:VTI" in d.flags
        assert d.action == "none"

    def test_sell_blocked_fund(self):
        d = decide_trades(inputs(BREACHED, sell_blocked=frozenset({"VTI"})))
        assert d.sells == zeros()
        assert "SELL_BLOCKED:VTI" in d.flags

    def test_sale_capped_by_order_limit(self):
        d = decide_trades(inputs(vals(7000, 1000, 1000, 1000)))
        assert d.sells["VTI"] == D(1500)
        assert "MAX_ORDER_CAP:VTI" in d.flags
        assert sum(d.deferred_buys.values()) == D(1500)

    def test_daily_limit_scales_sales(self):
        d = decide_trades(inputs(BREACHED, daily_notional_left=D(1000)))
        assert d.sells["VTI"] == D(1000)
        assert "DAILY_LIMIT_SELLS" in d.flags

    def test_tiny_sale_dropped(self):
        d = decide_trades(inputs(BREACHED, daily_notional_left=D("0.5")))
        assert d.sells == zeros()
        assert "BELOW_MIN_ORDER:VTI" in d.flags

    def test_no_bands_never_sells(self):
        d = decide_trades(inputs(vals(7000, 1000, 1000, 1000), band_width=None))
        assert d.breaches_now == {} and d.sells == zeros()

    def test_contribution_and_sale_same_run(self):
        # Cash 300 goes to the underweight funds; VTI is still above 60% -> sold.
        d = decide_trades(inputs(vals(7000, 1000, 1000, 1000), cash=300))
        assert sum(d.buys.values()) == D(300)
        assert d.buys["VTI"] == 0
        assert d.sells["VTI"] > 0
        assert d.action == "contribution+band_rebalance"


class TestAnnualReset:
    def test_requires_approval_even_inside_bands(self):
        d = decide_trades(inputs(vals(5500, 1800, 1350, 1350), annual_reset=True))
        assert d.breaches_projected == {}
        assert d.sells["VTI"] == D(500)
        assert d.requires_approval
        assert d.action == "annual_reset"
        assert "ST_CAP_EXCEEDED" not in d.flags

    def test_flags_large_short_term_gain(self):
        lots_map = {"VTI": (lot_for("VTI", 5500, cost=100, acquired=date(2026, 9, 1)),)}
        d = decide_trades(inputs(vals(5500, 1800, 1350, 1350), lots_map=lots_map, annual_reset=True))
        assert "ST_CAP_EXCEEDED" in d.flags
        assert d.rebalance == "annual_reset"  # never downgraded

    def test_without_cap(self):
        lots_map = {"VTI": (lot_for("VTI", 5500, cost=100, acquired=date(2026, 9, 1)),)}
        d = decide_trades(inputs(vals(5500, 1800, 1350, 1350), lots_map=lots_map, annual_reset=True, max_st_gain=None))
        assert "ST_CAP_EXCEEDED" not in d.flags


class TestSellLegs:
    def test_netting_reduces_buy_first(self):
        inp = inputs(BREACHED)
        target = {**zeros(), "VTI": D("10.005")}
        buys = {**zeros(), "VTI": D("5.00")}
        new_buys, sells, _, flags, _ = planmod._sell_legs(inp, target, buys)
        assert new_buys["VTI"] == 0
        assert sells["VTI"] == D("5.00")
        assert "NETTED:VTI" in flags

    def test_netting_reduces_sale_to_nothing(self):
        inp = inputs(BREACHED)
        target = {**zeros(), "VTI": D("10.004")}
        buys = {**zeros(), "VTI": D("20.00")}
        new_buys, sells, selections, _, _ = planmod._sell_legs(inp, target, buys)
        assert new_buys["VTI"] == D("10.00")
        assert sells == zeros() and selections == {}


# ---------------------------------------------------------------- build_plan


def ctx(**overrides):
    base = {"today": TODAY, "now": NOW, "live_requested": False}
    base.update(overrides)
    return PlanContext(**base)


class TestBuildPlan:
    def test_dry_run_plan_is_sealed_and_valid(self, config):
        p = build_plan(make_snapshot(on_target_shares(), cash=1000), config, ctx())
        assert p.mode == "dry_run"
        assert p.run_id.startswith("2026-11-02-dry_run-")
        assert p.blockers == ()
        assert all(p.checks.values()) and len(p.checks) == 11
        assert [o.line_id for o in p.orders] == ["L1", "L2", "L3", "L4"]
        assert p.totals == {"buys": D(1000), "sells": D(0)}
        doc = plan_to_dict(p)
        assert verify_plan_dict(doc)
        assert doc["orders"][0] == {
            "line_id": "L1",
            "symbol": "AVUV",
            "side": "buy",
            "notional_usd": 150.0,
            "reason": "contribution",
        }

    def test_live_mode_when_requested_and_clean(self, config):
        p = build_plan(make_snapshot(on_target_shares(), cash=1000), config, ctx(live_requested=True))
        assert p.mode == "live"
        assert "-live-" in p.run_id

    def test_sells_listed_before_buys_and_deferred_orders(self, config):
        shares = {s: v / PRICES[s] for s, v in vals(7000, 1000, 1000, 1000).items()}
        p = build_plan(make_snapshot(shares, cash=300), config, ctx())
        assert p.orders[0].side == Side.SELL
        assert p.deferred_orders and all(o.reason == "redeploy_sale_proceeds" for o in p.deferred_orders)
        assert sum(o.notional_usd for o in p.deferred_orders) == p.totals["sells"]

    @pytest.mark.parametrize(
        ("snapshot_kwargs", "ctx_kwargs", "blocker"),
        [
            ({}, {"kill_switch": True}, "KILL_SWITCH"),
            ({}, {"trading_window_open": False}, "OUTSIDE_TRADING_WINDOW"),
            ({}, {"live_run_exists_today": True}, "LIVE_RUN_EXISTS_TODAY"),
            ({"open_orders": ("o1",)}, {}, "OPEN_ORDERS"),
            ({"as_of": NOW - timedelta(days=1)}, {}, "SNAPSHOT_NOT_FROM_TODAY"),
            ({"cash": "-5"}, {}, "NEGATIVE_CASH"),
        ],
    )
    def test_blockers_force_dry_run(self, config, snapshot_kwargs, ctx_kwargs, blocker):
        snapshot = make_snapshot(on_target_shares(), **{"cash": "1000", **snapshot_kwargs})
        p = build_plan(snapshot, config, ctx(live_requested=True, **ctx_kwargs))
        assert blocker in p.blockers
        assert p.mode == "dry_run"

    def test_tax_aware_lots_not_supported_live(self):
        config = make_config({"lot_method": "tax_aware"})
        p = build_plan(make_snapshot(on_target_shares(), cash=1000), config, ctx(live_requested=True))
        assert "LOT_METHOD_NOT_SUPPORTED_LIVE" in p.blockers
        dry = build_plan(make_snapshot(on_target_shares(), cash=1000), config, ctx())
        assert dry.blockers == ()

    def test_missing_quote_means_no_orders(self, config):
        quotes = {s: p for s, p in PRICES.items() if s != "VXUS"}
        p = build_plan(make_snapshot(on_target_shares(), cash=1000, quotes=quotes), config, ctx())
        assert "MISSING_QUOTE:VXUS" in p.blockers
        assert p.orders == () and p.checks == {} and p.portfolio["funds"] == {}
        assert p.action == "none"

    def test_stale_quote(self, config):
        later = ctx(now=NOW + timedelta(minutes=16))
        p = build_plan(make_snapshot(on_target_shares(), cash=1000), config, later)
        assert "STALE_QUOTE:VTI" in p.blockers

    def test_satellite_unmanaged_and_unpriced_positions(self):
        config = make_config({"satellite_symbols": ["TSLA"]})
        extra = (Holding("TSLA", D(2)), Holding("XYZ", D(1)), Holding("ABC", D(3)), Holding("ZERO", D(0)))
        quotes = {**PRICES, "TSLA": D(250), "XYZ": D(100)}
        p = build_plan(make_snapshot(on_target_shares(), cash=0, quotes=quotes, extra_holdings=extra), config, ctx())
        assert p.portfolio["satellite_value"] == 500.0
        assert p.portfolio["unmanaged_value"] == 100.0
        assert p.portfolio["total_value"] == 10600.0
        assert p.portfolio["satellite_share"] == pytest.approx(500 / 10600)
        assert "UNMANAGED_POSITION:XYZ" in p.flags and "UNPRICED:ABC" in p.flags

    def test_equity_must_match_positions_and_cash(self, config):
        # 10,000 USD of funds plus 1,000 cash: the broker should report about 11,000.
        live = ctx(live_requested=True)
        close = build_plan(make_snapshot(on_target_shares(), cash=1000, equity="11020"), config, live)
        assert close.mode == "live" and close.portfolio["equity_gap"] == pytest.approx(-20)
        # A position missing from the positions response shows up as a large gap.
        missing = build_plan(make_snapshot(on_target_shares(), cash=1000, equity="16000"), config, live)
        assert "EQUITY_MISMATCH" in missing.blockers and missing.mode == "dry_run"
        assert missing.portfolio["reported_equity"] == 16000.0
        # The 25 USD floor applies to small accounts (2% of 400 is only 8 USD).
        small = build_plan(make_snapshot({}, cash=400, equity="420"), config, ctx())
        assert "EQUITY_MISMATCH" not in small.blockers
        unknown = build_plan(make_snapshot(on_target_shares(), cash=1000), config, ctx())
        assert unknown.portfolio["equity_gap"] is None

    def test_buying_power_caps_cash(self, config):
        p = build_plan(make_snapshot(on_target_shares(), cash=1000, buying_power=400), config, ctx())
        assert p.totals["buys"] == D(400)

    def test_cash_reserve_holds_back_cash(self):
        config = make_config({"cash": {"min_cash_to_deploy_usd": "25", "cash_reserve_usd": "5000"}})
        p = build_plan(make_snapshot(on_target_shares(), cash=1000), config, ctx())
        assert "CASH_BELOW_RESERVE" in p.flags
        assert p.orders == ()

    def test_any_deployment_amount_releases_one_tranche(self):
        config = make_config({"initial_deployment": {"start_date": TODAY, "total_usd": "1234.56", "tranches": 3}})
        p = build_plan(make_snapshot({}, cash="1234.56"), config, ctx())
        # Two of three tranches still held back: deploy 1234.56 / 3 = 411.52.
        assert p.totals["buys"] == D("411.52")
        assert [t["released"] for t in p.portfolio["deployment"]] == [True, False, False]
        assert p.portfolio["funds"]["VTI"]["weight"] is None  # empty core

    def test_tradability_problems_block_trading(self, config):
        tradability = (
            Tradability("VTI", True, False),
            Tradability("VXUS", False, True),
            Tradability("QQQM", True, True),
        )
        p = build_plan(make_snapshot({}, cash=1000, tradability=tradability), config, ctx())
        assert {"NOT_TRADABLE_IN_DOLLARS:VTI", "NOT_TRADABLE_IN_DOLLARS:VXUS", "TRADABILITY_UNKNOWN:AVUV"} <= set(
            p.flags
        )
        assert p.orders == ()  # blocked funds dominate an empty core: nothing can stay on target

    def test_lot_mismatch_blocks_sales(self, config):
        shares = {s: v / PRICES[s] for s, v in vals(7000, 1000, 1000, 1000).items()}
        lots = [TaxLot("VTI-1", "VTI", OLD, shares["VTI"] - 1, PRICES["VTI"])]
        p = build_plan(make_snapshot(shares, lots=lots), config, ctx())
        assert "LOT_MISMATCH:VTI" in p.flags and "SELL_BLOCKED:VTI" in p.flags
        assert p.orders == ()

    def test_annual_reset_due_and_not_due(self, config):
        shares = {s: v / PRICES[s] for s, v in vals(5500, 1800, 1350, 1350).items()}
        due = build_plan(make_snapshot(shares), config, ctx(annual_reset_requested=True))
        assert due.portfolio["annual_reset_due"] is True  # 15 Jan 2026 passed, not done
        assert "ANNUAL_RESET_NOT_DUE" not in due.flags
        assert due.requires_approval and due.action == "annual_reset"
        # Shares 5500/300 repeat, so the true excess is 499.999...; sales floor to cents.
        assert due.totals["sells"] == D("499.99")
        # Regression: dollars -> shares -> dollars noise must not look like a constrained sale.
        assert not any(f.startswith("SELL_CONSTRAINED") for f in due.flags)
        early = build_plan(
            make_snapshot(shares), config, ctx(annual_reset_requested=True, completed_reset_years=frozenset({2026}))
        )
        assert "ANNUAL_RESET_NOT_DUE" in early.flags
        assert early.portfolio["annual_reset_due"] is False

    def test_compliance_approval_for_all_trades(self):
        config = make_config(limit_overrides={"compliance": {"min_holding_days": 30, "approval_for_all_trades": True}})
        with_orders = build_plan(make_snapshot(on_target_shares(), cash=1000), config, ctx())
        without = build_plan(make_snapshot(on_target_shares(), cash=0), config, ctx())
        assert with_orders.requires_approval and not without.requires_approval

    def test_empty_account(self, config):
        p = build_plan(make_snapshot({}, cash=0), config, ctx())
        assert p.portfolio["total_value"] == 0.0
        assert p.portfolio["satellite_share"] is None

    def test_failed_check_blocks(self, config, monkeypatch):
        monkeypatch.setattr(planmod, "_checks", lambda *a, **k: {"orders_whitelisted": False})
        p = build_plan(make_snapshot(on_target_shares(), cash=1000), config, ctx(live_requested=True))
        assert "CHECK_FAILED:orders_whitelisted" in p.blockers
        assert p.mode == "dry_run"


class TestChecksHaveTeeth:
    def _decision_and_orders(self):
        inp = inputs(vals(5000, 2000, 1500, 1500), cash=1000)
        d = decide_trades(inp)
        orders = [Order(f"L{i}", s, Side.BUY, b, "contribution") for i, (s, b) in enumerate(d.buys.items()) if b > 0]
        return inp, d, orders

    def test_all_pass_for_real_plan(self):
        inp, d, orders = self._decision_and_orders()
        checks = planmod._checks(d, orders, inp.values, inp, TARGETS, D(1500), D(3000))
        assert all(checks.values())

    def test_detects_bad_orders_without_raising(self):
        inp, d, orders = self._decision_and_orders()
        bad = [
            *orders,
            Order("L9", "TSLA", Side.BUY, D(2000), "contribution"),
            Order("L10", "GME", Side.SELL, D(5), "band_rebalance"),
        ]
        checks = planmod._checks(d, bad, inp.values, inp, TARGETS, D(1500), D(3000))
        assert not checks["orders_whitelisted"]
        assert not checks["orders_within_size_limits"]
        assert not checks["daily_notional_within_limit"]
        assert not checks["no_buy_above_target"]
        assert not checks["no_sell_below_target"]
        assert not checks["sells_covered_by_eligible_lots"]

    def test_detects_loss_sale_after_recent_buy(self):
        inp = inputs(BREACHED, lots_map={"VTI": (lot_for("VTI", 6200, cost=400),)})
        d = decide_trades(inp)
        sells = [Order("L1", "VTI", Side.SELL, d.sells["VTI"], "band_rebalance")]
        assert d.selections["VTI"].had_loss
        recent = TradeRecord("VTI", Side.BUY, TODAY, D(1))
        checks = planmod._checks(d, sells, inp.values, replace(inp, trades=(recent,)), TARGETS, D(1500), D(3000))
        assert not checks["wash_sale_clear"]

    def test_detects_overbuy_and_mismatch(self):
        inp, d, orders = self._decision_and_orders()
        overbuy = [replace(orders[0], notional_usd=orders[0].notional_usd + 500)]
        tampered = replace(d, contribution_allocated=d.contribution_allocated + 1)
        checks = planmod._checks(tampered, overbuy, inp.values, inp, TARGETS, D(1500), D(3000))
        assert not checks["no_buy_above_target"]
        assert not checks["contribution_sum_matches"]

    def test_detects_wash_sale_buy(self):
        inp, d, orders = self._decision_and_orders()
        loss = TradeRecord(orders[0].symbol, Side.SELL, TODAY, D(1), had_loss=True)
        checks = planmod._checks(d, orders, inp.values, replace(inp, trades=(loss,)), TARGETS, D(1500), D(3000))
        assert not checks["wash_sale_clear"]


# ---------------------------------------------------------------- serialization


class TestSerialization:
    def test_canonical_json(self):
        assert canonical_json({"b": D("1.50"), "a": (1, 2)}) == '{"a":[1,2],"b":1.5}'

    def test_finalize_and_verify(self):
        doc = finalize({"date": "2026-11-02", "mode": "dry_run", "orders": []})
        assert doc["run_id"].startswith("2026-11-02-dry_run-")
        assert verify_plan_dict(doc)
        tampered = {**doc, "orders": [{"symbol": "VTI"}]}
        assert not verify_plan_dict(tampered)

    def test_same_inputs_same_plan(self, config):
        a = build_plan(make_snapshot(on_target_shares(), cash=1000), config, ctx())
        b = build_plan(make_snapshot(on_target_shares(), cash=1000), config, ctx())
        assert plan_to_dict(a) == plan_to_dict(b)
        assert a.plan_sha256 == b.plan_sha256

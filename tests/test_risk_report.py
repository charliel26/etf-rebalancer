import math
from datetime import date, timedelta

import pytest

from rebalancer.config import RiskConfig
from rebalancer.plan import PlanContext, build_plan, plan_to_dict
from rebalancer.report import render_report
from rebalancer.risk import RiskSummary, assess, drawdown, ewma_vol, holdings_returns
from tests.builders import NOW, TODAY, make_snapshot, on_target_shares
from tests.conftest import D, make_config

RISK = RiskConfig(
    ewma_lambda=D("0.94"),
    vol_window_days=63,
    drawdown_lookback_days=756,
    vol_flag=D("0.20"),
    drawdown_flag=D("0.25"),
    satellite_cap=D("0.15"),
)


def series(prices, start=date(2026, 1, 1)):
    return tuple((start + timedelta(days=i), D(p)) for i, p in enumerate(prices))


class TestReturns:
    def test_weighted_mix_on_common_dates(self):
        closes = {"A": series([100, 110, 99]), "B": series([50, 50, 55])}
        returns = holdings_returns(closes, {"A": 1.0, "B": 3.0})
        assert returns == pytest.approx([0.25 * 0.10 + 0.75 * 0.0, 0.25 * (-0.1) + 0.75 * 0.10])

    def test_missing_or_bad_data(self):
        assert holdings_returns({"A": series([100, 101])}, {"A": 1.0, "B": 1.0}) == []
        assert holdings_returns({"A": series([100])}, {"A": 1.0}) == []
        assert holdings_returns({"A": series([0, 1])}, {"A": 1.0}) == []
        assert holdings_returns({}, {"A": 0.0}) == []


class TestEwma:
    def test_constant_returns_give_sqrt252_times_abs_return(self):
        assert ewma_vol([0.01] * 63, 0.94, 63) == pytest.approx(0.01 * math.sqrt(252))

    def test_matches_hand_formula_and_uses_only_the_window(self):
        returns = [0.03] * 100 + [0.01 * ((-1) ** k) for k in range(63)]
        weights = [0.94**k for k in range(63)]
        expected = math.sqrt(sum(w * 0.0001 for w in weights) / sum(weights)) * math.sqrt(252)
        assert ewma_vol(returns, 0.94, 63) == pytest.approx(expected)

    def test_recent_returns_weigh_more(self):
        calm_then_wild = [0.001] * 40 + [0.03] * 23
        wild_then_calm = [0.03] * 23 + [0.001] * 40
        assert ewma_vol(calm_then_wild, 0.94, 63) > ewma_vol(wild_then_calm, 0.94, 63)

    def test_too_few_observations(self):
        assert ewma_vol([0.01] * 19, 0.94, 63) is None


class TestDrawdown:
    def test_current_and_worst(self):
        current, worst = drawdown([0.10, -0.50, 0.20], 756)
        assert worst == pytest.approx(-0.5)
        assert current == pytest.approx(0.5 * 1.2 - 1)

    def test_lookback_and_empty(self):
        assert drawdown([-0.5, 0.1], 1) == (0.0, 0.0)
        assert drawdown([], 10) is None


class TestAssess:
    def test_flags(self):
        wild = series([100 * (1.05 if i % 2 else 0.95) ** (i % 2) * (0.99**i) for i in range(80)])
        summary = assess({"A": 1.0}, {"A": wild}, 0.2, RISK)
        assert "HIGH_VOLATILITY" in summary.flags and "SATELLITE_CAP" in summary.flags
        crash = series([100] * 30 + [70] * 30)
        assert "DEEP_DRAWDOWN" in assess({"A": 1.0}, {"A": crash}, 0.0, RISK).flags

    def test_no_history(self):
        summary = assess({"A": 1.0}, {}, None, RISK)
        assert summary.flags == ("NO_PRICE_HISTORY",)
        assert summary.vol_annualized is None and summary.drawdown_current is None


class TestReport:
    def test_renders_all_sections(self):
        config = make_config()
        shares = {s: v for s, v in on_target_shares().items()}
        doc = plan_to_dict(
            build_plan(
                make_snapshot(shares, cash=1000), config, PlanContext(today=TODAY, now=NOW, live_requested=False)
            )
        )
        risk = RiskSummary(0.15, -0.05, -0.10, 0.0, 63, ("HIGH_VOLATILITY",))
        text = render_report(doc, risk)
        for fragment in (
            "# Rebalancer report — 2026-11-02",
            "**dry_run**",
            "## Portfolio",
            "| VTI |",
            "## Orders",
            "$500.00",
            "## Tax estimate",
            "## Risk",
            "15.0%",
            "`HIGH_VOLATILITY`",
            "## Checks",
            "PASS `orders_whitelisted`",
            doc["plan_sha256"],
        ):
            assert fragment in text

    def test_renders_blocked_and_deferred_plans(self):
        config = make_config({"initial_deployment": {"start_date": TODAY, "total_usd": "300", "tranches": 3}})
        shares = {"VTI": D(7000) / 300, "VXUS": D(1000) / 60, "QQQM": D(5), "AVUV": D(10)}
        doc = plan_to_dict(
            build_plan(
                make_snapshot(shares, cash=0, open_orders=("x",), equity="99999"),
                config,
                PlanContext(today=TODAY, now=NOW, live_requested=True),
            )
        )
        text = render_report(doc, None)
        assert "## Blockers" in text and "`OPEN_ORDERS`" in text and "`EQUITY_MISMATCH`" in text
        assert "Broker-reported equity $99,999.00" in text
        assert "redeployed at the next run" in text
        assert "Phased deployment" in text and "Annual reset is due" in text
        assert "No risk data." in text

    def test_no_orders(self):
        doc = plan_to_dict(
            build_plan(
                make_snapshot({}, cash=0), make_config(), PlanContext(today=TODAY, now=NOW, live_requested=False)
            )
        )
        assert "No orders." in render_report(doc, None)

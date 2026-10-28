"""Tests for rebalancer.engine: examples from STRATEGY.md plus property tests."""

from decimal import Decimal

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from rebalancer import engine
from tests.conftest import TARGETS, D

TOL = Decimal("1e-12")
SYMBOLS = sorted(TARGETS)


def vals(vti=0, vxus=0, qqqm=0, avuv=0):
    return {"VTI": D(vti), "VXUS": D(vxus), "QQQM": D(qqqm), "AVUV": D(avuv)}


def pct(vti, vxus, qqqm, avuv, scale=100):
    """Portfolio with the given percentage weights and total `scale * 100`."""
    return vals(vti * scale, vxus * scale, qqqm * scale, avuv * scale)


money = st.decimals(min_value=0, max_value=10**7, places=2, allow_nan=False, allow_infinity=False)
portfolios = st.fixed_dictionaries({s: money for s in SYMBOLS})
positive_cash = st.decimals(min_value=Decimal("0.01"), max_value=10**7, places=2)


# ---------------------------------------------------------------- validation


class TestValidation:
    def test_keys_must_match_targets(self):
        with pytest.raises(ValueError, match="same funds"):
            engine.contribution_rule({"VTI": D(1)}, TARGETS, D(10))

    def test_negative_values_rejected(self):
        with pytest.raises(ValueError, match="negative"):
            engine.breaches(vals(vti=-1), TARGETS, D("0.2"))

    def test_targets_must_sum_to_one(self):
        bad = {**TARGETS, "VTI": D("0.49")}
        with pytest.raises(ValueError, match="sum to 1"):
            engine.full_rebalance(vals(1, 1, 1, 1), bad)

    def test_targets_must_be_positive(self):
        bad = {"VTI": D("1.2"), "VXUS": D("-0.2"), "QQQM": D(0), "AVUV": D(0)}
        with pytest.raises(ValueError, match="positive"):
            engine.full_rebalance(vals(1, 1, 1, 1), bad)


# ---------------------------------------------------------------- weights and bands


class TestWeightsAndBands:
    def test_core_value_and_weights(self):
        v = pct(50, 20, 15, 15)
        assert engine.core_value(v) == D(10000)
        assert engine.weights(v) == {"VTI": D("0.5"), "VXUS": D("0.2"), "QQQM": D("0.15"), "AVUV": D("0.15")}

    def test_weights_of_empty_core_raise(self):
        with pytest.raises(ValueError, match="empty"):
            engine.weights(vals())

    def test_band_edges(self):
        assert engine.band_edges(D("0.15"), D("0.20")) == (D("0.12"), D("0.18"))

    def test_no_breach_on_target(self):
        assert engine.breaches(pct(50, 20, 15, 15), TARGETS, D("0.2")) == {}

    def test_empty_core_never_breaches(self):
        assert engine.breaches(vals(), TARGETS, D("0.2")) == {}

    def test_high_and_low_breaches(self):
        # QQQM 19% > 18% (high); AVUV 11% < 12% (low).
        assert engine.breaches(pct(50, 20, 19, 11), TARGETS, D("0.2")) == {"QQQM": "high", "AVUV": "low"}

    def test_exactly_on_edge_is_inside(self):
        # QQQM exactly 18%, AVUV exactly 12%: |dev| == 0.2 is not > 0.2.
        assert engine.breaches(pct(50, 20, 18, 12), TARGETS, D("0.2")) == {}

    def test_relative_deviation(self):
        dev = engine.relative_deviation(pct(50, 20, 18, 12), TARGETS)
        assert dev == {"VTI": D(0), "VXUS": D(0), "QQQM": D("0.2"), "AVUV": D("-0.2")}


# ---------------------------------------------------------------- contribution rule


class TestContributionRule:
    def test_empty_core_buys_target_weights(self):
        result = engine.contribution_rule(vals(), TARGETS, D(2700))
        assert result.deployed == D(2700)
        assert result.amounts == {"VTI": D(1350), "VXUS": D(540), "QQQM": D(405), "AVUV": D(405)}

    def test_spec_formula_with_overweight_fund(self):
        # VTI 1500 is above its post-contribution target 0.5 * 2000 = 1000, so it
        # gets nothing; the gaps 400/300/300 (sum 1000 > C) are scaled by C / 1000.
        v = vals(1500, 0, 0, 0)
        c = D(500)
        gaps = engine.contribution_gaps(v, TARGETS, c)
        assert gaps == {"VTI": D(0), "VXUS": D(400), "QQQM": D(300), "AVUV": D(300)}
        result = engine.contribution_rule(v, TARGETS, c)
        assert result.amounts == {"VTI": D(0), "VXUS": D(200), "QQQM": D(150), "AVUV": D(150)}
        assert sum(result.amounts.values()) == c

    def test_scales_down_when_gaps_exceed_cash(self):
        v = vals(400, 300, 300, 0)  # V = 1000, C = 100 -> targets at 1100
        result = engine.contribution_rule(v, TARGETS, D(100))
        # gaps: VTI 150, VXUS 0, QQQM 0, AVUV 165 (sum 315 >= 100)
        assert result.gaps == {"VTI": D(150), "VXUS": D(0), "QQQM": D(0), "AVUV": D(165)}
        assert result.amounts["VTI"] == D(100) * D(150) / D(315)
        assert result.amounts["AVUV"] == D(100) * D(165) / D(315)

    def test_zero_cash(self):
        result = engine.contribution_rule(vals(1, 1, 1, 1), TARGETS, D(0))
        assert result.deployed == 0
        assert all(a == 0 for a in result.amounts.values())

    def test_negative_cash_rejected(self):
        with pytest.raises(ValueError):
            engine.contribution_rule(vals(), TARGETS, D(-1))

    def test_blocked_fund_on_empty_core_deploys_nothing(self):
        # Buying anything else would push it above target relative to the new core.
        result = engine.contribution_rule(vals(), TARGETS, D(1000), blocked=frozenset({"VTI"}))
        assert result.deployed == 0
        assert all(a == 0 for a in result.amounts.values())

    def test_blocked_fund_at_target_does_not_matter(self):
        v = vals(5000, 1500, 1500, 1500)  # VXUS underweight, VTI exactly at target... V=9500
        free = engine.contribution_rule(v, TARGETS, D(500))
        blocked = engine.contribution_rule(v, TARGETS, D(500), blocked=frozenset({"VTI"}))
        # VTI: target at 10000 is 5000 -> gap 0 either way.
        assert free.gaps["VTI"] == 0
        assert blocked.deployed == D(500)
        assert blocked.amounts == free.amounts

    def test_blocked_only_underweight_fund_deploys_nothing(self):
        # Only VXUS is underweight and it is blocked. Buying VTI, QQQM or AVUV
        # (all above target) can never keep them at or below target: 0.8d - 1200 < d.
        v = vals(4000, 0, 1000, 1000)
        result = engine.contribution_rule(v, TARGETS, D(4000), blocked=frozenset({"VXUS"}))
        assert result.deployed == 0

    def test_blocked_fund_partial_deployment_is_the_fixed_point(self):
        # VXUS blocked; QQQM and AVUV empty; VTI overweight. Deploy d with
        # 0.3 * (1000 + d) = d  =>  d = 300 / 0.7, half to QQQM, half to AVUV.
        v = vals(1000, 0, 0, 0)
        result = engine.contribution_rule(v, TARGETS, D(4000), blocked=frozenset({"VXUS"}))
        assert abs(result.deployed - D(300) / D("0.7")) <= TOL
        assert result.amounts["VTI"] == 0
        assert result.amounts["VXUS"] == 0
        assert abs(result.amounts["QQQM"] - result.deployed / 2) <= TOL
        new_total = sum(v.values()) + result.deployed
        assert abs(result.amounts["QQQM"] / new_total - D("0.15")) <= TOL

    def test_all_blocked(self):
        result = engine.contribution_rule(vals(1, 1, 1, 1), TARGETS, D(10), blocked=frozenset(TARGETS))
        assert result.deployed == 0

    def test_blocked_share_goes_to_others_only_within_their_gaps(self):
        # VXUS blocked but VTI's gap (1500) can absorb all 1000 without passing target.
        v = vals(0, 0, 1000, 1000)
        result = engine.contribution_rule(v, TARGETS, D(1000), blocked=frozenset({"VXUS"}))
        assert result.deployed == D(1000)
        assert result.amounts == {"AVUV": D(0), "QQQM": D(0), "VTI": D(1000), "VXUS": D(0)}

    def test_order_cap_limits_deployment_to_keep_proportions(self):
        # The bug this guards against: capping VTI at 1500 out of a 6000
        # deployment must not leave the other funds above target.
        result = engine.contribution_rule(vals(), TARGETS, D(6000), max_buy=D(1500))
        assert result.deployed == D(3000)
        assert result.amounts == {"AVUV": D(450), "QQQM": D(450), "VTI": D(1500), "VXUS": D(600)}

    def test_non_binding_cap_changes_nothing(self):
        free = engine.contribution_rule(vals(), TARGETS, D(2700))
        capped = engine.contribution_rule(vals(), TARGETS, D(2700), max_buy=D(1500))
        assert capped == free

    def test_water_fill_degenerate_input_returns_caps(self):
        # Callers guarantee sum(min(cap, weight)) >= amount; if violated, every fund
        # is capped and the result simply sums to less than the amount.
        result = engine._water_fill(D(10), {"A": D(1), "B": D(1)}, {"A": D(3), "B": D(3)})
        assert result == {"A": D(3), "B": D(3)}

    def test_negative_cap_rejected(self):
        with pytest.raises(ValueError):
            engine.contribution_rule(vals(), TARGETS, D(10), max_buy=D(-1))

    @settings(max_examples=300)
    @given(
        portfolios,
        positive_cash,
        st.sets(st.sampled_from(SYMBOLS), max_size=3),
        st.one_of(st.none(), st.decimals(min_value=0, max_value=10**6, places=2)),
    )
    def test_limited_contribution_properties(self, v, c, blocked, max_buy):
        result = engine.contribution_rule(v, TARGETS, c, blocked=frozenset(blocked), max_buy=max_buy)
        d = result.deployed
        assert 0 <= d <= c
        assert abs(sum(result.amounts.values()) - d) <= Decimal("1e-9")
        new_total = sum(v.values()) + d
        for s in SYMBOLS:
            b = result.amounts[s]
            assert b >= -TOL
            if s in blocked:
                assert b == 0
            if max_buy is not None:
                assert b <= max_buy + TOL
            if b > 0:  # never above target relative to the core actually produced
                assert v[s] + b <= TARGETS[s] * new_total + Decimal("1e-9")
        if d < c:  # maximal: the limited gaps absorb exactly d
            absorb = Decimal(0)
            for s in SYMBOLS:
                g = max(Decimal(0), TARGETS[s] * new_total - v[s])
                limit = Decimal(0) if s in blocked else max_buy
                absorb += g if limit is None else min(limit, g)
            assert abs(absorb - d) <= Decimal("1e-9")

    @given(portfolios, positive_cash)
    def test_spec_properties(self, v, c):
        gaps = engine.contribution_gaps(v, TARGETS, c)
        result = engine.contribution_rule(v, TARGETS, c)
        assert sum(gaps.values()) >= c - TOL  # P1
        assert result.deployed == c
        assert abs(sum(result.amounts.values()) - c) <= TOL  # P3
        target_total = sum(v.values()) + c
        for s in SYMBOLS:
            b = result.amounts[s]
            assert -TOL <= b <= gaps[s] + TOL  # P2: never above post-contribution target
            before = abs(v[s] - TARGETS[s] * target_total)
            after = abs(v[s] + b - TARGETS[s] * target_total)
            assert after <= before + TOL  # P5: never moves away from target
        if all(v[s] <= TARGETS[s] * target_total for s in SYMBOLS):
            for s in SYMBOLS:  # P4: nothing above target -> buy exactly the gaps
                assert abs(result.amounts[s] - gaps[s]) <= TOL

    @given(portfolios, positive_cash, st.sets(st.sampled_from(SYMBOLS), min_size=1, max_size=3))
    def test_blocked_properties(self, v, c, blocked):
        result = engine.contribution_rule(v, TARGETS, c, blocked=frozenset(blocked))
        d = result.deployed
        assert 0 <= d <= c
        assert abs(sum(result.amounts.values()) - d) <= TOL
        new_total = sum(v.values()) + d
        for s in SYMBOLS:
            if s in blocked:
                assert result.amounts[s] == 0
            assert result.amounts[s] >= -TOL
            # Never above target relative to the core it actually produces (only for funds bought).
            if result.amounts[s] > 0:
                assert v[s] + result.amounts[s] <= TARGETS[s] * new_total + Decimal("1e-9")
        if d < c:
            # Maximal: unblocked gaps at the new core absorb exactly d.
            absorb = sum(
                max(Decimal(0), TARGETS[s] * new_total - v[s]) for s in SYMBOLS if s not in blocked
            )
            assert abs(absorb - d) <= Decimal("1e-9")

    @settings(max_examples=300)
    @given(
        st.lists(st.decimals(min_value=0, max_value=10**6, places=2), min_size=4, max_size=4),
        st.lists(st.decimals(min_value=1, max_value=1000, places=2), min_size=4, max_size=4),
        positive_cash,
        st.one_of(st.none(), st.decimals(min_value=1, max_value=10**6, places=2)),
    )
    def test_noisy_values_from_shares_times_price(self, raw_values, prices, c, max_buy):
        # Live and backtest values are shares * price with repeating-decimal shares,
        # so sums carry ~1e-24 noise. The regression this guards: an on-target
        # portfolio deployed nothing because sum(gaps) came out a hair below C.
        v = {s: (amount / p) * p for s, amount, p in zip(SYMBOLS, raw_values, prices, strict=True)}
        result = engine.contribution_rule(v, TARGETS, c, max_buy=max_buy)
        assert 0 <= result.deployed <= c
        if max_buy is None:
            assert result.deployed == c
        new_total = sum(v.values()) + result.deployed
        for s in SYMBOLS:
            if result.amounts[s] > 0:
                assert v[s] + result.amounts[s] <= TARGETS[s] * new_total + Decimal("1e-8")
            if max_buy is not None:
                assert result.amounts[s] <= max_buy + Decimal("1e-8")

    def test_on_target_noisy_portfolio_deploys_everything(self):
        prices = {"VTI": D(300), "VXUS": D(60), "QQQM": D(200), "AVUV": D(100)}
        v = {s: (amount / prices[s]) * prices[s] for s, amount in vals(5000, 2000, 1500, 1500).items()}
        result = engine.contribution_rule(v, TARGETS, D(1000), max_buy=D(1500))
        assert result.deployed == D(1000)

    def test_input_order_does_not_matter(self):
        v = vals(700, 100, 100, 100)
        reordered = dict(reversed(list(v.items())))
        targets_reordered = dict(reversed(list(TARGETS.items())))
        a = engine.contribution_rule(v, TARGETS, D(500))
        b = engine.contribution_rule(reordered, targets_reordered, D(500))
        assert a.amounts == b.amounts


# ---------------------------------------------------------------- withdrawal rule


class TestWithdrawalRule:
    def test_takes_from_overweight_first(self):
        v = vals(700, 100, 100, 100)  # raise 100: targets at 900 -> VTI 450
        result = engine.withdrawal_rule(v, TARGETS, D(100))
        assert result == {"VTI": D(100), "VXUS": D(0), "QQQM": D(0), "AVUV": D(0)}

    def test_zero_amount(self):
        assert all(x == 0 for x in engine.withdrawal_rule(vals(1, 1, 1, 1), TARGETS, D(0)).values())

    @pytest.mark.parametrize("amount", [D(-1), D(5)])
    def test_rejects_bad_amount(self, amount):
        with pytest.raises(ValueError):
            engine.withdrawal_rule(vals(1, 1, 1, 1), TARGETS, amount)

    @given(portfolios, st.decimals(min_value=0, max_value=1, places=4))
    def test_properties(self, v, fraction):
        total = sum(v.values())
        amount = total * fraction
        result = engine.withdrawal_rule(v, TARGETS, amount)
        assert abs(sum(result.values()) - amount) <= TOL
        for s in SYMBOLS:
            x = result[s]
            assert -TOL <= x <= v[s] + TOL
            if x > 0:  # never sold below its post-withdrawal target
                assert v[s] - x >= TARGETS[s] * (total - amount) - TOL


# ---------------------------------------------------------------- full rebalance


class TestFullRebalance:
    def test_example(self):
        sells, buys = engine.full_rebalance(pct(58, 15, 14, 13), TARGETS)
        assert sells == {"VTI": D(800), "VXUS": D(0), "QQQM": D(0), "AVUV": D(0)}
        assert buys == {"VTI": D(0), "VXUS": D(500), "QQQM": D(100), "AVUV": D(200)}

    def test_empty(self):
        sells, buys = engine.full_rebalance(vals(), TARGETS)
        assert sum(sells.values()) == sum(buys.values()) == 0

    @given(portfolios)
    def test_sells_equal_buys_and_redeploy_reproduces_buys(self, v):
        sells, buys = engine.full_rebalance(v, TARGETS)
        assert abs(sum(sells.values()) - sum(buys.values())) <= TOL
        # STRATEGY.md §6.3: selling today and redeploying proceeds with the
        # contribution rule buys exactly the deficits.
        after_sells = engine.apply_trades(v, sells=sells)
        proceeds = sum(sells.values())
        redeploy = engine.contribution_rule(after_sells, TARGETS, proceeds)
        for s in SYMBOLS:
            assert abs(redeploy.amounts[s] - buys[s]) <= Decimal("1e-9")


# ---------------------------------------------------------------- band edge


class TestBandEdge:
    def test_strategy_example_under_breach(self):
        # VTI 58, VXUS 15 (< 16 edge), QQQM 14, AVUV 13; deficits 5, 1, 2 (D = 8).
        result = engine.band_edge_sells(pct(58, 15, 14, 13, scale=1), TARGETS, D("0.2"))
        assert result.total_deficit == D(8)
        assert result.x_over == 0
        assert result.x_under == D("1.6")  # 8 * (16 - 15) / (20 - 15)
        assert result.sells == {"VTI": D("1.6"), "VXUS": D(0), "QQQM": D(0), "AVUV": D(0)}

    def test_over_breach_sells_to_edge(self):
        # QQQM 20% > 18%: sells 2 to reach the edge; no under-breach.
        result = engine.band_edge_sells(pct(48, 20, 20, 12, scale=1), TARGETS, D("0.2"))
        assert result.x_over == D(2)
        assert result.x_under == 0
        assert result.sells == {"VTI": D(0), "VXUS": D(0), "QQQM": D(2), "AVUV": D(0)}

    def test_both_sides_extra_comes_pro_rata_from_remaining_excess(self):
        # QQQM 19 (over by 1 beyond 18); AVUV 9 (< 12); VTI 52, VXUS 20.
        v = pct(52, 20, 19, 9, scale=1)
        result = engine.band_edge_sells(v, TARGETS, D("0.2"))
        assert result.x_over == D(1)
        # D = 6 (AVUV deficit); ratio (12 - 9) / (15 - 9) = 0.5 -> X_under = 3.
        assert result.x_under == D(3)
        # Remaining 2 split by remaining excess: VTI 2, QQQM 3 (18 - 15) -> 0.8 and 1.2.
        assert result.sells["VTI"] == D("0.8")
        assert result.sells["QQQM"] == D("2.2")

    def test_no_breach_no_sells(self):
        result = engine.band_edge_sells(pct(50, 20, 15, 15, scale=1), TARGETS, D("0.2"))
        assert all(x == 0 for x in result.sells.values())

    def test_empty_core(self):
        result = engine.band_edge_sells(vals(), TARGETS, D("0.2"))
        assert all(x == 0 for x in result.sells.values())

    @settings(max_examples=300)
    @given(portfolios, st.sampled_from([D("0.1"), D("0.2"), D("0.3")]))
    def test_every_fund_in_band_after_redeploy_and_minimal(self, v, band):
        assume(engine.breaches(v, TARGETS, band))
        result = engine.band_edge_sells(v, TARGETS, band)
        sells = result.sells
        total = sum(v.values())
        assert sum(sells.values()) <= result.total_deficit + TOL
        for s in SYMBOLS:
            assert sells[s] >= -TOL
            if sells[s] > 0:  # never sold below target
                assert v[s] - sells[s] >= TARGETS[s] * total - Decimal("1e-9")
        after = engine.apply_trades(v, sells=sells)
        redeploy = engine.contribution_rule(after, TARGETS, sum(sells.values()))
        final = engine.apply_trades(after, buys=redeploy.amounts)
        rel = engine.relative_deviation(final, TARGETS)
        assert max(abs(x) for x in rel.values()) <= band + Decimal("1e-9")
        # Minimality: selling 0.1% less everywhere leaves some fund outside its band.
        smaller = {s: x * Decimal("0.999") for s, x in sells.items()}
        after2 = engine.apply_trades(v, sells=smaller)
        redeploy2 = engine.contribution_rule(after2, TARGETS, sum(smaller.values()))
        final2 = engine.apply_trades(after2, buys=redeploy2.amounts)
        rel2 = engine.relative_deviation(final2, TARGETS)
        assert max(abs(x) for x in rel2.values()) > band


class TestApplyTrades:
    def test_add_and_subtract(self):
        v = vals(10, 10, 10, 10)
        out = engine.apply_trades(v, buys={"VTI": D(5)}, sells={"VXUS": D(3)})
        assert out == vals(15, 7, 10, 10)

    def test_rejects_unknown_symbol_and_oversell(self):
        with pytest.raises(ValueError):
            engine.apply_trades(vals(1, 1, 1, 1), buys={"XYZ": D(1)})
        with pytest.raises(ValueError):
            engine.apply_trades(vals(1, 1, 1, 1), sells={"XYZ": D(1)})
        with pytest.raises(ValueError):
            engine.apply_trades(vals(1, 1, 1, 1), sells={"VTI": D(2)})

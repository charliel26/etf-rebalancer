from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rebalancer import lots
from rebalancer.models import Side, TaxLot, TradeRecord
from tests.conftest import D

SALE = date(2026, 6, 1)


def lot(lot_id, acquired, shares, cost, symbol="VTI"):
    return TaxLot(lot_id=lot_id, symbol=symbol, acquired=acquired, shares=D(shares), cost_per_share=D(cost))


GROUPS = (frozenset({"QQQM", "QQQ"}), frozenset({"VTI", "VTSAX"}))


class TestHoldingPeriod:
    def test_add_one_year(self):
        assert lots.add_one_year(date(2025, 3, 15)) == date(2026, 3, 15)
        assert lots.add_one_year(date(2024, 2, 29)) == date(2025, 2, 28)

    def test_irs_example_more_than_one_year(self):
        # IRS Pub. 550: bought Feb 5, sold Feb 5 next year -> short-term; Feb 6 -> long-term.
        assert not lots.is_long_term(date(2023, 2, 5), date(2024, 2, 5))
        assert lots.is_long_term(date(2023, 2, 5), date(2024, 2, 6))

    def test_leap_day_lot(self):
        assert not lots.is_long_term(date(2024, 2, 29), date(2025, 2, 28))
        assert lots.is_long_term(date(2024, 2, 29), date(2025, 3, 1))


class TestOrdering:
    @pytest.fixture
    def mixed(self):
        return [
            lot("lt_gain_big", date(2020, 1, 1), 1, 50),  # LT gain 50
            lot("st_gain", date(2026, 1, 1), 1, 95),  # ST gain 5
            lot("lt_loss", date(2021, 1, 1), 1, 110),  # LT loss 10
            lot("st_loss_small", date(2026, 2, 1), 1, 101),  # ST loss 1
            lot("st_loss_big", date(2026, 3, 1), 1, 120),  # ST loss 20
            lot("lt_gain_small", date(2022, 1, 1), 1, 90),  # LT gain 10
            lot("st_flat", date(2026, 4, 1), 1, 100),  # ST zero gain
        ]

    def test_tax_aware_order(self, mixed):
        ordered = lots.order_lots(mixed, D(100), SALE, "tax_aware")
        assert [x.lot_id for x in ordered] == [
            "st_loss_big",
            "st_loss_small",
            "st_flat",
            "lt_loss",
            "lt_gain_small",
            "lt_gain_big",
            "st_gain",
        ]

    def test_fifo_order(self, mixed):
        ordered = lots.order_lots(mixed, D(100), SALE, "fifo")
        assert [x.lot_id for x in ordered][:3] == ["lt_gain_big", "lt_loss", "lt_gain_small"]

    def test_ties_by_date_then_id(self):
        same = [lot("b", date(2026, 1, 2), 1, 90), lot("a", date(2026, 1, 2), 1, 90), lot("c", date(2026, 1, 1), 1, 90)]
        assert [x.lot_id for x in lots.order_lots(same, D(100), SALE, "tax_aware")] == ["c", "a", "b"]
        assert [x.lot_id for x in lots.order_lots(same, D(100), SALE, "fifo")] == ["c", "a", "b"]

    def test_unknown_method(self, mixed):
        with pytest.raises(ValueError):
            lots.order_lots(mixed, D(100), SALE, "hifo")


class TestSelectLots:
    def test_partial_last_lot_and_gains(self):
        holdings = [lot("old", date(2020, 1, 1), 2, 50), lot("new", date(2026, 3, 1), 3, 80)]
        sel = lots.select_lots(holdings, D("2.5"), D(100), SALE, "fifo", recent_buy=False, min_holding_days=0)
        assert [(p.lot.lot_id, p.shares) for p in sel.picks] == [("old", D(2)), ("new", D("0.5"))]
        assert sel.lt_gain == D(100)  # 2 * (100 - 50)
        assert sel.st_gain == D(10)  # 0.5 * (100 - 80)
        assert sel.shares_selected == D("2.5")
        assert sel.unfilled == 0
        assert not sel.had_loss

    def test_requesting_more_than_held(self):
        sel = lots.select_lots([lot("a", date(2020, 1, 1), 1, 50)], D(5), D(100), SALE, "fifo",
                               recent_buy=False, min_holding_days=0)
        assert sel.shares_selected == D(1)
        assert sel.unfilled == D(4)

    def test_tax_aware_skips_wash_blocked_loss_lot(self):
        holdings = [lot("loss", date(2026, 5, 1), 1, 120), lot("gain", date(2020, 1, 1), 1, 50)]
        sel = lots.select_lots(holdings, D(1), D(100), SALE, "tax_aware", recent_buy=True, min_holding_days=0)
        assert [p.lot.lot_id for p in sel.picks] == ["gain"]
        assert sel.blocked == (("loss", "WASH_SALE"),)

    def test_fifo_stops_at_blocked_lot(self):
        # Oldest lot is at a loss and a buy happened within 30 days: FIFO cannot skip it.
        holdings = [lot("old_loss", date(2025, 1, 1), 1, 120), lot("new_gain", date(2026, 1, 1), 1, 50)]
        sel = lots.select_lots(holdings, D(2), D(100), SALE, "fifo", recent_buy=True, min_holding_days=0)
        assert sel.picks == ()
        assert sel.unfilled == D(2)
        assert sel.blocked == (("old_loss", "WASH_SALE"),)

    def test_loss_lot_allowed_without_recent_buy(self):
        sel = lots.select_lots([lot("loss", date(2025, 1, 1), 1, 120)], D(1), D(100), SALE, "fifo",
                               recent_buy=False, min_holding_days=0)
        assert sel.lt_gain == D(-20)
        assert sel.had_loss

    def test_zero_gain_lot_is_not_wash_blocked(self):
        sel = lots.select_lots([lot("flat", date(2026, 5, 20), 1, 100)], D(1), D(100), SALE, "fifo",
                               recent_buy=True, min_holding_days=0)
        assert sel.shares_selected == D(1)
        assert not sel.had_loss

    def test_min_holding_days(self):
        holdings = [lot("old", date(2026, 1, 1), 1, 90), lot("young", date(2026, 5, 20), 1, 90)]
        sel = lots.select_lots(holdings, D(2), D(100), SALE, "tax_aware", recent_buy=False, min_holding_days=30)
        assert [p.lot.lot_id for p in sel.picks] == ["old"]
        assert sel.blocked == (("young", "MIN_HOLDING"),)

    def test_zero_shares_requested(self):
        sel = lots.select_lots([lot("a", date(2020, 1, 1), 1, 50)], D(0), D(100), SALE, "fifo",
                               recent_buy=False, min_holding_days=0)
        assert sel.picks == ()

    @pytest.mark.parametrize(("shares", "price"), [(D(-1), D(100)), (D(1), D(0))])
    def test_rejects_bad_input(self, shares, price):
        with pytest.raises(ValueError):
            lots.select_lots([], shares, price, SALE, "fifo", recent_buy=False, min_holding_days=0)

    @given(
        st.lists(
            st.tuples(
                st.dates(min_value=date(2015, 1, 1), max_value=SALE),
                st.decimals(min_value=Decimal("0.000001"), max_value=1000, places=6),
                st.decimals(min_value=1, max_value=1000, places=2),
            ),
            min_size=1,
            max_size=12,
        ),
        st.decimals(min_value=0, max_value=5000, places=6),
        st.sampled_from(["fifo", "tax_aware"]),
    )
    def test_conservation(self, raw, requested, method):
        holdings = [lot(f"L{i}", d, q, c) for i, (d, q, c) in enumerate(raw)]
        price = D(100)
        sel = lots.select_lots(holdings, requested, price, SALE, method, recent_buy=False, min_holding_days=0)
        held = sum(x.shares for x in holdings)
        assert sel.shares_selected == min(requested, held)
        assert sel.shares_selected + sel.unfilled == requested
        by_id = {x.lot_id: x for x in holdings}
        for pick in sel.picks:
            assert 0 < pick.shares <= by_id[pick.lot.lot_id].shares
            assert pick.gain == pick.shares * (price - pick.lot.cost_per_share)
        assert sel.st_gain + sel.lt_gain == sum(p.gain for p in sel.picks)
        remaining = lots.remove_sold(holdings, sel.picks)
        assert sum(x.shares for x in remaining) == held - sel.shares_selected


class TestRemoveSold:
    def test_reduces_and_drops_empty_lots(self):
        holdings = [lot("a", date(2020, 1, 1), 2, 50), lot("b", date(2021, 1, 1), 3, 60)]
        sel = lots.select_lots(holdings, D("2.5"), D(100), SALE, "fifo", recent_buy=False, min_holding_days=0)
        remaining = lots.remove_sold(holdings, sel.picks)
        assert [(x.lot_id, x.shares) for x in remaining] == [("b", D("2.5"))]

    def test_rejects_unknown_or_oversold_pick(self):
        a = lot("a", date(2020, 1, 1), 1, 50)
        with pytest.raises(ValueError):
            lots.remove_sold([a], [lots.LotPick(lot=lot("x", date(2020, 1, 1), 1, 50), shares=D(1), gain=D(0), long_term=True)])
        with pytest.raises(ValueError):
            lots.remove_sold([a], [lots.LotPick(lot=a, shares=D(2), gain=D(0), long_term=True)])


class TestWashSaleGuard:
    def trades(self, *items):
        return [TradeRecord(symbol=s, side=side, trade_date=d, notional=D(100), had_loss=loss) for s, side, d, loss in items]

    def test_group_lookup(self):
        assert lots.group_of("QQQ", GROUPS) == frozenset({"QQQM", "QQQ"})
        assert lots.group_of("AVUV", GROUPS) == frozenset({"AVUV"})

    def test_window_is_inclusive_30_calendar_days(self):
        assert lots.within_window(date(2026, 5, 2), SALE)  # 30 days
        assert not lots.within_window(date(2026, 5, 1), SALE)  # 31 days
        assert lots.within_window(SALE, SALE)
        assert not lots.within_window(date(2026, 6, 2), SALE)  # future

    def test_w1_recent_buy_blocks_loss_sale_across_group(self):
        history = self.trades(("QQQ", Side.BUY, date(2026, 5, 15), None))
        assert lots.loss_sale_blocked("QQQM", SALE, history, GROUPS)
        assert not lots.loss_sale_blocked("VTI", SALE, history, GROUPS)

    def test_w1_old_buy_or_sell_does_not_block(self):
        history = self.trades(("QQQM", Side.BUY, date(2026, 4, 1), None), ("QQQM", Side.SELL, SALE, True))
        assert not lots.loss_sale_blocked("QQQM", SALE, history, GROUPS)

    def test_w2_recent_loss_sale_blocks_buy(self):
        history = self.trades(("VTSAX", Side.SELL, date(2026, 5, 20), True))
        assert lots.buy_blocked("VTI", SALE, history, GROUPS)

    def test_w2_unknown_pnl_counts_as_loss(self):
        history = self.trades(("VTI", Side.SELL, date(2026, 5, 20), None))
        assert lots.buy_blocked("VTI", SALE, history, GROUPS)

    def test_w2_gain_sale_or_buy_does_not_block(self):
        history = self.trades(("VTI", Side.SELL, date(2026, 5, 20), False), ("VTI", Side.BUY, SALE, None))
        assert not lots.buy_blocked("VTI", SALE, history, GROUPS)
        old = self.trades(("VTI", Side.SELL, date(2026, 4, 1), True))
        assert not lots.buy_blocked("VTI", SALE, old, GROUPS)

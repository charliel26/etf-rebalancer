from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from rebalancer.ledger import BrokerOrder, Ledger, LedgerError, _status_for
from rebalancer.models import Side, TradeRecord
from tests.conftest import D

DAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


def plan(run_id="r1", mode="live", day=DAY, orders=None, lots=None, requires_approval=False):
    return {
        "run_id": run_id,
        "date": day.isoformat(),
        "mode": mode,
        "plan_sha256": f"sha-{run_id}",
        "requires_approval": requires_approval,
        "action": "contribution",
        "orders": orders
        if orders is not None
        else [
            {"line_id": "L1", "symbol": "VTI", "side": "sell", "notional_usd": 300.0, "reason": "band_rebalance"},
            {"line_id": "L2", "symbol": "VXUS", "side": "buy", "notional_usd": 200.0, "reason": "contribution"},
        ],
        "tax_estimate": {"lots": lots if lots is not None else {"VTI": [{"gain_usd": -5.0}]}},
    }


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "state" / "ledger.sqlite3") as led:
        yield led


def claim(ledger, symbol="VXUS", side="buy", amount="200", cap="3000", tool_use_id="t1", run_id="r1", now=NOW):
    return ledger.claim_line(
        run_id,
        symbol,
        side,
        D(amount),
        tolerance=D(1),
        daily_cap=D(cap),
        run_date=DAY,
        now=now,
        tool_use_id=tool_use_id,
    )


class TestRuns:
    def test_record_and_read(self, ledger):
        ledger.record_run(plan(), "state/plans/r1.json", NOW)
        run = ledger.live_run_for(DAY)
        assert run.run_id == "r1" and run.plan_path == "state/plans/r1.json" and not run.requires_approval
        assert ledger.run("r1") == run
        assert ledger.run("missing") is None
        lines = ledger.lines("r1")
        assert [(x.line_id, x.status, x.notional_usd) for x in lines] == [
            ("L1", "PENDING", D(300)),
            ("L2", "PENDING", D(200)),
        ]
        assert lines[0].est_had_loss is True and lines[1].est_had_loss is None

    def test_one_live_run_per_date(self, ledger):
        ledger.record_run(plan("r1"), "p1", NOW)
        with pytest.raises(LedgerError):
            ledger.record_run(plan("r2"), "p2", NOW)
        ledger.record_run(plan("d1", mode="dry_run"), "p3", NOW)
        ledger.record_run(plan("d2", mode="dry_run"), "p4", NOW)  # dry runs are unlimited
        ledger.record_run(plan("r3", day=DAY + timedelta(days=1)), "p5", NOW)
        assert ledger.live_run_for(DAY).run_id == "r1"

    def test_line_order_is_numeric(self, ledger):
        orders = [
            {"line_id": f"L{i}", "symbol": "VTI", "side": "buy", "notional_usd": 1.0, "reason": "x"} for i in (10, 2, 1)
        ]
        ledger.record_run(plan(orders=orders, lots={}), "p", NOW)
        assert [x.line_id for x in ledger.lines("r1")] == ["L1", "L2", "L10"]

    def test_failed_insert_rolls_back(self, ledger):
        bad = plan(orders=[{"line_id": "L1", "symbol": "VTI", "side": "hold", "notional_usd": 1.0, "reason": "x"}])
        with pytest.raises(LedgerError):
            ledger.record_run(bad, "p", NOW)
        assert ledger.run("r1") is None


class TestClaims:
    def test_claim_once(self, ledger):
        ledger.record_run(plan(), "p", NOW)
        assert claim(ledger).ok
        again = claim(ledger, tool_use_id="t2")
        assert not again.ok and "matches 0" in again.reason
        assert ledger.notional_used(DAY) == D(200)

    def test_tolerance(self, ledger):
        ledger.record_run(plan(), "p", NOW)
        assert not claim(ledger, amount="201.01").ok
        assert claim(ledger, amount="200.99").ok

    def test_ambiguous_match_refused(self, ledger):
        orders = [
            {"line_id": "L1", "symbol": "VTI", "side": "buy", "notional_usd": 100.0, "reason": "x"},
            {"line_id": "L2", "symbol": "VTI", "side": "buy", "notional_usd": 100.5, "reason": "x"},
        ]
        ledger.record_run(plan(orders=orders, lots={}), "p", NOW)
        result = claim(ledger, symbol="VTI", amount="100.2")
        assert not result.ok and "matches 2" in result.reason

    def test_daily_cap_at_claim_time(self, ledger):
        ledger.record_run(plan(), "p", NOW)
        assert claim(ledger, cap="250").ok  # 200 used
        result = claim(ledger, symbol="VTI", side="sell", amount="300", cap="250", tool_use_id="t2")
        assert not result.ok and "daily notional limit" in result.reason

    def test_dry_run_lines_do_not_count(self, ledger):
        ledger.record_run(plan("d1", mode="dry_run"), "p", NOW)
        assert ledger.notional_used(DAY) == 0


class TestLifecycle:
    def test_submitted_failed_and_rearm(self, ledger):
        ledger.record_run(plan(), "p", NOW)
        claim(ledger, tool_use_id="t1")
        assert ledger.mark_submitted("t1", "ord-1", NOW)
        assert not ledger.mark_submitted("t1", "ord-1", NOW)  # already submitted
        claim(ledger, symbol="VTI", side="sell", amount="300", tool_use_id="t2")
        assert ledger.mark_submit_failed("t2", "network error", NOW)
        assert not ledger.mark_submit_failed("unknown", "x", NOW)
        statuses = {x.line_id: x.status for x in ledger.lines("r1")}
        assert statuses == {"L1": "SUBMIT_FAILED", "L2": "SUBMITTED"}
        ledger.rearm_line("r1", "L1", NOW)
        assert ledger.lines("r1")[0].status == "PENDING"
        with pytest.raises(LedgerError, match="only"):
            ledger.rearm_line("r1", "L2", NOW)
        with pytest.raises(LedgerError, match="no line"):
            ledger.rearm_line("r1", "L9", NOW)
        kinds = [e["kind"] for e in ledger.events("r1")]
        assert "line_claimed" in kinds and "line_rearmed" in kinds
        assert any(e["kind"] == "submit_failed" for e in ledger.events())


def order(order_id, symbol, side, state, notional="200", filled="0", created=NOW, filled_at=None):
    return BrokerOrder(
        order_id, symbol, side, state, None if notional is None else D(notional), D(filled), created, filled_at
    )


class TestReconcile:
    def test_links_by_id_and_by_match(self, ledger):
        ledger.record_run(plan(), "p", NOW)
        claim(ledger, tool_use_id="t1")
        ledger.mark_submitted("t1", "ord-1", NOW)
        claim(ledger, symbol="VTI", side="sell", amount="300", tool_use_id="t2")  # no id recorded
        later = NOW + timedelta(minutes=2)
        changes = ledger.reconcile(
            "r1",
            [
                order("ord-1", "VXUS", "buy", "filled", filled="200", filled_at=later),
                order("ord-2", "VTI", "sell", "filled", notional="300", filled="300"),
            ],
            later,
            tolerance=D(1),
        )
        assert changes == {"L1": "FILLED", "L2": "FILLED"}
        trades = ledger.trades_since(DAY)
        assert {(t.symbol, t.side, t.had_loss) for t in trades} == {("VTI", Side.SELL, True), ("VXUS", Side.BUY, None)}

    def test_not_placed_after_timeout(self, ledger):
        ledger.record_run(plan(), "p", NOW)
        claim(ledger, tool_use_id="t1")
        assert ledger.reconcile("r1", [], NOW + timedelta(minutes=2), tolerance=D(1)) == {}
        assert ledger.reconcile("r1", [], NOW + timedelta(minutes=6), tolerance=D(1)) == {"L2": "NOT_PLACED"}
        ledger.rearm_line("r1", "L2", NOW)

    def test_unlinked_matching_rules(self, ledger):
        ledger.record_run(plan(), "p", NOW)
        claim(ledger, tool_use_id="t1")
        candidates = [
            order("o-early", "VXUS", "buy", "filled", created=NOW - timedelta(minutes=5)),
            order("o-other", "VTI", "buy", "filled"),
            order("o-amount", "VXUS", "buy", "filled", notional="250"),
            order("o-shares", "VXUS", "buy", "filled", notional=None),
            order("o-open", "VXUS", "buy", "queued"),
        ]
        changes = ledger.reconcile("r1", candidates, NOW + timedelta(minutes=1), tolerance=D(1))
        assert changes == {"L2": "SUBMITTED"}
        assert ledger.lines("r1")[1].broker_order_id == "o-open"

    @pytest.mark.parametrize(
        ("state", "filled", "expected"),
        [
            ("filled", "1", "FILLED"),
            ("rejected", "0", "REJECTED"),
            ("failed", "0", "REJECTED"),
            ("cancelled", "0", "CANCELLED"),
            ("canceled", "5", "PARTIAL"),
            ("partially_filled", "5", "PARTIAL"),
            ("confirmed", "0", "SUBMITTED"),
            ("something_new", "0", "SUBMITTED"),
        ],
    )
    def test_status_mapping(self, state, filled, expected):
        assert _status_for(order("x", "VTI", "buy", state, filled=filled)) == expected


class TestApprovalsTradesResets:
    def test_approval_is_per_hash_and_day(self, ledger):
        ledger.record_approval("r1", "sha", DAY, NOW)
        assert ledger.has_approval("r1", "sha", DAY)
        assert not ledger.has_approval("r1", "other", DAY)
        assert not ledger.has_approval("r1", "sha", DAY + timedelta(days=1))

    def test_trades_dedupe_and_window(self, ledger):
        trade = TradeRecord("VTI", Side.SELL, DAY, D(10), had_loss=False, order_id="o1")
        assert ledger.record_trade(trade, "reconcile")
        assert not ledger.record_trade(trade, "reconcile")
        ledger.record_trade(TradeRecord("VTI", Side.BUY, DAY - timedelta(days=60), D(5)), "manual")
        assert [t.order_id for t in ledger.trades_since(DAY - timedelta(days=31))] == ["o1"]
        assert ledger.trades_since(DAY)[0].had_loss is False

    def test_resets(self, ledger):
        assert ledger.completed_reset_years() == frozenset()
        ledger.record_reset(2026, "dismissed", None, NOW)
        ledger.record_reset(2027, "executed", "r9", NOW)
        assert ledger.completed_reset_years() == frozenset({2026, 2027})

    def test_set_line_status_keeps_fill(self, ledger):
        ledger.record_run(plan(), "p", NOW)
        ledger.set_line_status("r1", "L1", "PARTIAL", NOW, filled=Decimal("150"))
        ledger.set_line_status("r1", "L1", "CANCELLED", NOW)
        assert ledger.lines("r1")[0].status == "CANCELLED"

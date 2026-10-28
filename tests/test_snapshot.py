import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from rebalancer.broker import robinhood_raw as rr
from rebalancer.broker.base import BrokerError, CapturedRobinhood
from rebalancer.broker.mcp_payload import decode, find_order_id
from rebalancer.broker.snapshot import (
    SnapshotError,
    load_snapshot,
    save_snapshot,
    snapshot_from_dict,
    snapshot_to_dict,
)
from rebalancer.capture import capture
from rebalancer.models import Order, Side
from tests.builders import NOW, TODAY, make_snapshot, on_target_shares
from tests.conftest import D

SHARES = {"VTI": "16.5", "VXUS": "33", "QQQM": "7.5", "AVUV": "15"}
PRICES = {"VTI": "300.00", "VXUS": "60.00", "QQQM": "200.00", "AVUV": "100.00"}


def mcp(obj):
    return {"content": [{"type": "text", "text": json.dumps(obj)}]}


def capture_tool(state_dir, tool, response, now=NOW, event="PostToolUse", tool_input=None):
    capture(
        {
            "hook_event_name": event,
            "tool_name": f"mcp__robinhood-trading__{tool}",
            "tool_use_id": f"id-{tool}",
            "tool_input": tool_input or {},
            "tool_response": response,
        },
        state_dir,
        now,
    )


def write_raw_session(state_dir, *, orders=None, cash="1000.00", equity=None):
    if equity is None:  # positions at the quoted prices plus cash, as a broker reports it
        equity = str(sum(D(SHARES[s]) * D(PRICES[s]) for s in SHARES) + D(cash))
    capture_tool(
        state_dir,
        "get_portfolio",
        mcp({"account_number": "AGENT1", "cash": cash, "buying_power": cash, "equity": equity}),
    )
    capture_tool(
        state_dir, "get_equity_positions", mcp({"results": [{"symbol": s, "quantity": q} for s, q in SHARES.items()]})
    )
    capture_tool(
        state_dir,
        "get_equity_quotes",
        mcp([{"symbol": s, "last_trade_price": p, "updated_at": "2026-11-02T14:59:00Z"} for s, p in PRICES.items()]),
    )
    capture_tool(
        state_dir,
        "get_equity_tax_lots",
        mcp(
            {
                "lots": [
                    {
                        "symbol": s,
                        "lot_id": f"{s}-A",
                        "quantity": q,
                        "cost_basis": str(D(q) * D(PRICES[s])),
                        "acquired_date": "2024-01-02",
                    }
                    for s, q in SHARES.items()
                ]
            }
        ),
    )
    capture_tool(state_dir, "get_equity_orders", mcp({"results": orders or []}))
    capture_tool(
        state_dir,
        "get_equity_tradability",
        mcp([{"symbol": s, "tradable": True, "fractional_tradability": "tradable"} for s in SHARES]),
    )
    capture_tool(
        state_dir,
        "get_equity_historicals",
        mcp(
            [
                {
                    "symbol": "VTI",
                    "historicals": [
                        {"begins_at": "2026-10-29T00:00:00Z", "close_price": "298.00"},
                        {"begins_at": "2026-10-30T00:00:00Z", "close_price": "300.00"},
                    ],
                }
            ]
        ),
    )


class TestCanonical:
    def test_round_trip(self, tmp_path):
        snap = make_snapshot(on_target_shares(), cash="1000", buying_power="900")
        sha = save_snapshot(snap, tmp_path / "s.json", [{"path": "x", "tool": "y"}])
        loaded, sha2 = load_snapshot(tmp_path / "s.json")
        assert sha == sha2
        assert loaded == snap

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda d: d.update(schema_version=2), "schema_version"),
            (lambda d: d.update(account=[]), "account"),
            (lambda d: d.update(as_of="2026-11-02T10:00:00"), "timezone"),
            (lambda d: d.update(as_of="yesterday"), "ISO"),
            (lambda d: d["account"].update(cash="lots"), "account.cash"),
            (lambda d: d.update(positions=[{"symbol": "VTI", "shares": "-1"}]), ">="),
            (lambda d: d.update(positions="VTI"), "positions"),
            (lambda d: d.update(quotes=[{"symbol": "", "price": "1", "as_of": "2026-11-02T10:00:00+00:00"}]), "symbol"),
            (
                lambda d: d.update(
                    tax_lots=[{"symbol": "VTI", "acquired": "soon", "shares": "1", "cost_per_share": "1"}]
                ),
                "date",
            ),
            (
                lambda d: d.update(trades=[{"symbol": "VTI", "side": "hold", "date": "2026-11-01", "notional": "1"}]),
                "side",
            ),
            (
                lambda d: d.update(
                    trades=[{"symbol": "VTI", "side": "sell", "date": "2026-11-01", "notional": "1", "had_loss": "yes"}]
                ),
                "had_loss",
            ),
            (lambda d: d.update(closes=[]), "closes"),
        ],
    )
    def test_validation(self, mutate, message):
        doc = snapshot_to_dict(make_snapshot(on_target_shares(), cash="1"))
        mutate(doc)
        with pytest.raises(SnapshotError, match=message):
            snapshot_from_dict(doc)

    def test_load_errors(self, tmp_path):
        with pytest.raises(SnapshotError, match="cannot read"):
            load_snapshot(tmp_path / "missing.json")
        (tmp_path / "list.json").write_text("[]")
        with pytest.raises(SnapshotError, match="not a JSON object"):
            load_snapshot(tmp_path / "list.json")


class TestPayload:
    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            ({"structuredContent": {"a": 1}, "content": []}, {"a": 1}),
            ({"content": [{"type": "text", "text": '{"a": '}, {"type": "text", "text": "1}"}]}, {"a": 1}),
            ([{"type": "text", "text": "[1, 2]"}], [1, 2]),
            ('{"a": 2}', {"a": 2}),
            ("plain words", "plain words"),
            ({"a": 3}, {"a": 3}),
            (42, 42),
            ([], []),
        ],
    )
    def test_decode(self, response, expected):
        assert decode(response) == expected

    def test_find_order_id(self):
        assert find_order_id({"id": "abc"}) == "abc"
        assert find_order_id({"order_id": 7}) == "7"
        assert find_order_id({"order": {"id": "nested"}}) == "nested"
        assert find_order_id({"order": None, "status": "ok"}) is None
        assert find_order_id(["id"]) is None


class TestRobinhoodRaw:
    def test_build_snapshot_from_captures(self, tmp_path):
        write_raw_session(tmp_path)
        snap, sources = rr.build_snapshot(tmp_path / "raw" / TODAY.isoformat(), NOW)
        assert snap.cash == D("1000.00") and snap.account_id == "AGENT1"
        assert snap.reported_equity == D("10930.00")
        assert snap.shares("VTI") == D("16.5")
        assert snap.quote("VXUS").price == D("60.00")
        assert {lot.symbol: lot.cost_per_share for lot in snap.lots} == {s: D(p) for s, p in PRICES.items()}
        assert snap.tradability_for("AVUV").fractional
        assert snap.closes["VTI"][-1] == (date(2026, 10, 30), D("300.00"))
        assert len(sources) == 7
        first = sources[0]
        assert first["sha256"] == hashlib.sha256(Path(first["path"]).read_bytes()).hexdigest()

    def test_orders_become_open_ids_and_trades(self, tmp_path):
        orders = [
            {
                "id": "o1",
                "symbol": "VTI",
                "side": "buy",
                "state": "filled",
                "dollar_amount": "100",
                "cumulative_quantity": "0.5",
                "average_price": "200",
                "created_at": "2026-10-20T15:00:00Z",
                "last_transaction_at": "2026-10-20T15:00:05Z",
            },
            {"id": "o2", "symbol": "VXUS", "side": "sell", "state": "queued", "created_at": "2026-11-02T14:00:00Z"},
            {
                "id": "o3",
                "symbol": "AVUV",
                "side": "sell",
                "state": "filled",
                "filled_notional": "40",
                "created_at": "2026-10-23T00:10:00Z",  # 20:10 on 22 Oct in New York
            },
        ]
        write_raw_session(tmp_path, orders=orders)
        snap, _ = rr.build_snapshot(tmp_path / "raw" / TODAY.isoformat(), NOW)
        assert snap.open_order_ids == ("o2",)
        assert [(t.order_id, t.side, t.notional, t.trade_date) for t in snap.trades] == [
            ("o1", Side.BUY, D(100), date(2026, 10, 20)),
            ("o3", Side.SELL, D(40), date(2026, 10, 22)),
        ]
        assert [o.order_id for o in rr.broker_orders(tmp_path / "raw" / TODAY.isoformat())] == ["o1", "o2", "o3"]

    def test_failed_captures_ignored_and_missing_tools_reported(self, tmp_path):
        capture_tool(tmp_path, "get_portfolio", mcp({"cash": "1"}), event="PostToolUseFailure")
        with pytest.raises(SnapshotError, match="missing captured responses"):
            rr.build_snapshot(tmp_path / "raw" / TODAY.isoformat(), NOW)
        with pytest.raises(SnapshotError, match="no captured responses"):
            rr.build_snapshot(tmp_path / "nothing", NOW)
        with pytest.raises(SnapshotError, match="get_equity_orders"):
            rr.broker_orders(tmp_path / "nothing")

    def test_unknown_shapes_fail_loudly(self):
        with pytest.raises(SnapshotError, match="keys were"):
            rr.parse_positions({"results": [{"ticker": "VTI", "amount": "1"}]})
        with pytest.raises(SnapshotError, match="unrecognized response shape"):
            rr.parse_quotes("not json at all")
        with pytest.raises(SnapshotError, match="zero-share"):
            rr.parse_tax_lots([{"symbol": "VTI", "quantity": "0", "cost_basis": "5", "acquired_date": "2025-01-01"}])
        with pytest.raises(SnapshotError, match="must be a list"):
            rr.parse_historicals([{"symbol": "VTI", "historicals": "none"}])
        with pytest.raises(SnapshotError, match="side must be buy or sell"):
            rr.parse_orders(
                [
                    {
                        "id": "x",
                        "symbol": "VTI",
                        "side": "sell_short",
                        "state": "filled",
                        "created_at": "2026-11-02T14:00:00Z",
                    }
                ]
            )

    def test_per_share_cost_and_alternative_spellings(self):
        lots = rr.parse_tax_lots(
            [{"ticker": "VTI", "shares": "2", "cost_per_share": "150", "purchase_date": "2025-01-01"}]
        )
        assert lots[0].cost_per_share == D(150) and lots[0].lot_id == "VTI-0"
        info = rr.parse_tradability({"results": [{"symbol": "VTI", "tradeable": "tradable", "fractional": False}]})
        assert info[0].tradable and not info[0].fractional
        portfolio = rr.parse_portfolio({"cash_balance": "5"})
        assert portfolio == {"account_id": None, "cash": D(5), "buying_power": None, "equity": None}
        orders = rr.parse_orders(
            [
                {
                    "order_id": "x",
                    "symbol": "VTI",
                    "side": "BUY",
                    "status": "Filled",
                    "filled_notional": "10",
                    "submitted_at": "2026-11-02T14:00:00+00:00",
                }
            ]
        )
        assert (orders[0].side, orders[0].state, orders[0].filled_notional) == ("buy", "filled", D(10))

    def test_captured_robinhood_broker(self, tmp_path):
        write_raw_session(tmp_path)
        broker = CapturedRobinhood(tmp_path / "raw" / TODAY.isoformat())
        assert broker.snapshot(NOW).cash == D("1000.00")
        assert broker.orders() == []
        with pytest.raises(BrokerError):
            broker.submit(Order("L1", "VTI", Side.BUY, D(1), "x"), "c1")


def test_raw_capture_ignores_non_json(tmp_path):
    capture_tool(tmp_path, "get_portfolio", mcp({"cash": "1"}), now=datetime(2026, 11, 2, 15, tzinfo=UTC))
    files = list((tmp_path / "raw").rglob("*.json"))
    assert len(files) == 1
    assert Decimal(json.loads(json.loads(files[0].read_text())["tool_response"]["content"][0]["text"])["cash"]) == 1

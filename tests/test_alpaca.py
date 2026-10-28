import json
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from rebalancer.broker import alpaca_paper
from rebalancer.broker.alpaca_paper import PAPER_URL, AlpacaPaperBroker
from rebalancer.broker.base import BrokerError
from rebalancer.lots import Fill, lots_from_fills
from rebalancer.models import Order, Side
from tests.builders import NOW
from tests.conftest import D

CLOSED = [
    {
        "id": "b1",
        "symbol": "VTI",
        "side": "buy",
        "status": "filled",
        "filled_at": "2026-10-01T15:00:00Z",
        "filled_qty": "2",
        "filled_avg_price": "290",
        "created_at": "2026-10-01T14:59:00Z",
        "submitted_at": "2026-10-01T14:59:00Z",
        "notional": "580",
    },
    {
        "id": "s1",
        "symbol": "VTI",
        "side": "sell",
        "status": "filled",
        "filled_at": "2026-10-20T15:00:00Z",
        "filled_qty": "0.5",
        "filled_avg_price": "300",
        "created_at": "2026-10-20T14:59:00Z",
        "submitted_at": "2026-10-20T14:59:00Z",
        "notional": "150",
    },
    {
        "id": "c1",
        "symbol": "AVUV",
        "side": "buy",
        "status": "canceled",
        "filled_at": None,
        "filled_qty": "0",
        "filled_avg_price": None,
        "created_at": "2026-10-21T14:59:00Z",
        "submitted_at": "2026-10-21T14:59:00Z",
        "notional": "50",
    },
]


class FakeTransport:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, None if body is None else json.loads(body)))
        if self.fail_on and self.fail_on in url:
            return 403, {"message": "forbidden"}
        path = url.split(".markets", 1)[1]
        if path == "/v2/account":
            return 200, {"account_number": "PA1", "cash": "1000", "buying_power": "2000", "equity": "1435"}
        if path == "/v2/positions":
            return 200, [{"symbol": "VTI", "qty": "1.5"}]
        if path.startswith("/v2/orders?status=open"):
            return 200, []
        if path.startswith("/v2/orders?status=closed"):
            return 200, CLOSED
        if path.startswith("/v2/stocks/trades/latest"):
            return 200, {"trades": {"VTI": {"p": 290.5, "t": "2026-11-02T14:59:00Z"}}}
        if path.startswith("/v2/assets/"):
            return 200, {"tradable": True, "fractionable": path.endswith("VTI")}
        if method == "POST" and path == "/v2/orders":
            return 200, {"id": "new-1"}
        return 404, {}


def broker(transport=None):
    return AlpacaPaperBroker("key", "secret", ["VTI", "AVUV"], transport=transport or FakeTransport())


def test_paper_only_and_keys_required():
    with pytest.raises(BrokerError, match="paper"):
        AlpacaPaperBroker("k", "s", ["VTI"], transport=FakeTransport(), base_url="https://api.alpaca.markets")
    with pytest.raises(BrokerError, match="APCA"):
        AlpacaPaperBroker("", "s", ["VTI"], transport=FakeTransport())


def test_snapshot_rebuilds_lots_from_fills():
    snap = broker().snapshot(NOW)
    assert (snap.cash, snap.buying_power, snap.account_id) == (D(1000), D(2000), "PA1")
    assert snap.shares("VTI") == D("1.5")
    assert [(lot.lot_id, lot.shares, lot.cost_per_share) for lot in snap.lots] == [("b1", D("1.5"), D(290))]
    assert snap.quote("VTI").price == D("290.5")
    assert snap.tradability_for("VTI").fractional and not snap.tradability_for("AVUV").fractional
    assert [t.order_id for t in snap.trades] == ["b1", "s1"]


def test_submit_sends_a_safe_market_order():
    transport = FakeTransport()
    order_id = broker(transport).submit(Order("L1", "VTI", Side.BUY, D("123.45"), "contribution"), "run-L1")
    assert order_id == "new-1"
    method, url, body = transport.calls[-1]
    assert (method, url) == ("POST", f"{PAPER_URL}/v2/orders")
    assert body == {
        "symbol": "VTI",
        "notional": "123.45",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "extended_hours": False,
        "client_order_id": "run-L1",
    }


class PagedTransport(FakeTransport):
    """Serves closed orders strictly after `after`, at most two per page."""

    def __call__(self, method, url, headers, body):
        if "/v2/orders?status=closed" not in url:
            return super().__call__(method, url, headers, body)
        self.calls.append((method, url, None))
        after = parse_qs(urlsplit(url).query)["after"][0]
        return 200, [o for o in CLOSED if o["submitted_at"] > after][:2]


def test_closed_orders_are_read_page_by_page(monkeypatch):
    monkeypatch.setattr(alpaca_paper, "PAGE", 2)
    transport = PagedTransport()
    snap = broker(transport).snapshot(NOW)
    assert [t.order_id for t in snap.trades] == ["b1", "s1"]
    closed_calls = [url for _, url, _ in transport.calls if "status=closed" in url]
    assert len(closed_calls) == 2 and "2026-10-20T14%3A59%3A00Z" in closed_calls[1]


def test_orders_and_http_errors():
    orders = broker().orders()
    assert [(o.order_id, o.state, o.filled_notional) for o in orders] == [
        ("b1", "filled", D(580)),
        ("s1", "filled", D(150)),
        ("c1", "canceled", D(0)),
    ]
    with pytest.raises(BrokerError, match="403"):
        broker(FakeTransport(fail_on="/v2/account")).snapshot(NOW)


class TestLotsFromFills:
    def test_fifo_relief_and_same_day_order(self):
        fills = [
            Fill("s1", "VTI", Side.SELL, date(2026, 3, 1), D(1), D(110)),
            Fill("b2", "VTI", Side.BUY, date(2026, 2, 1), D(2), D(105)),
            Fill("b1", "VTI", Side.BUY, date(2026, 1, 1), D(1), D(100)),
            Fill("q1", "QQQM", Side.BUY, date(2026, 3, 1), D(1), D(200)),
            Fill("q2", "QQQM", Side.SELL, date(2026, 3, 1), D("0.4"), D(201)),
        ]
        lots = lots_from_fills(fills)
        assert [(x.lot_id, x.shares) for x in lots] == [("b2", D(2)), ("q1", D("0.6"))]

    def test_overselling_is_an_error(self):
        with pytest.raises(ValueError, match="exceeds"):
            lots_from_fills([Fill("s1", "VTI", Side.SELL, date(2026, 1, 1), D(1), D(100))])

"""Alpaca paper-trading adapter: the same plan and guard, no language model.

Paper only: any base URL other than Alpaca's paper endpoint is refused.
Alpaca exposes no tax lots, so lots are rebuilt FIFO from filled orders.
Credentials come from APCA_API_KEY_ID / APCA_API_SECRET_KEY. In the Alpaca
dashboard, set the paper account's margin multiplier to 1 (no margin) and
disable shorting.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ..ledger import BrokerOrder
from ..lots import Fill, lots_from_fills
from ..models import Holding, Order, Quote, Side, Snapshot, Tradability, TradeRecord
from ..money import to_decimal
from .base import Broker, BrokerError

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
NEW_YORK = ZoneInfo("America/New_York")
PAGE = 500  # Alpaca's maximum page size for /v2/orders

Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, Any]]


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class AlpacaPaperBroker(Broker):
    def __init__(
        self,
        key_id: str,
        secret_key: str,
        symbols: Iterable[str],
        transport: Transport = urllib_transport,
        base_url: str = PAPER_URL,
    ) -> None:
        if base_url != PAPER_URL:
            raise BrokerError("the Alpaca adapter only talks to the paper endpoint")
        if not key_id or not secret_key:
            raise BrokerError("set APCA_API_KEY_ID and APCA_API_SECRET_KEY")
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Content-Type": "application/json",
        }
        self._symbols = sorted(symbols)
        self._transport = transport

    def _call(self, method: str, url: str, body: Mapping[str, Any] | None = None) -> Any:
        payload = None if body is None else json.dumps(body).encode()
        status, data = self._transport(method, url, self._headers, payload)
        if status >= 300:
            raise BrokerError(f"Alpaca {method} {url} -> {status}: {data}")
        return data

    def _closed_orders(self, since: datetime) -> list[dict[str, Any]]:
        """Closed orders submitted after `since`, oldest first, all pages."""
        out: list[dict[str, Any]] = []
        after = since.isoformat()
        while True:
            query = urlencode({"status": "closed", "after": after, "limit": PAGE, "direction": "asc"})
            page = self._call("GET", f"{PAPER_URL}/v2/orders?{query}")
            out += page
            if len(page) < PAGE:
                return out
            after = page[-1]["submitted_at"]

    def snapshot(self, now: datetime) -> Snapshot:
        account = self._call("GET", f"{PAPER_URL}/v2/account")
        positions = self._call("GET", f"{PAPER_URL}/v2/positions")
        open_orders = self._call("GET", f"{PAPER_URL}/v2/orders?status=open")
        closed = self._closed_orders(now - timedelta(days=3650))
        symbols = sorted(set(self._symbols) | {p["symbol"] for p in positions})
        latest = self._call("GET", f"{DATA_URL}/v2/stocks/trades/latest?{urlencode({'symbols': ','.join(symbols)})}")
        tradability = []
        for symbol in self._symbols:
            asset = self._call("GET", f"{PAPER_URL}/v2/assets/{symbol}")
            tradability.append(Tradability(symbol, bool(asset.get("tradable")), bool(asset.get("fractionable"))))
        fills = [
            Fill(
                o["id"],
                o["symbol"],
                Side(o["side"]),
                _ts(o["filled_at"]).astimezone(NEW_YORK).date(),
                to_decimal(o["filled_qty"]),
                to_decimal(o["filled_avg_price"]),
            )
            for o in closed
            if o.get("filled_at") and to_decimal(o.get("filled_qty") or "0") > 0
        ]
        return Snapshot(
            as_of=now,
            account_id=str(account.get("account_number")),
            cash=to_decimal(account["cash"]),
            buying_power=to_decimal(account["buying_power"]),
            reported_equity=to_decimal(account["equity"]),
            holdings=tuple(Holding(p["symbol"], to_decimal(p["qty"])) for p in positions),
            quotes=tuple(
                Quote(sym, to_decimal(trade["p"]), _ts(trade["t"])) for sym, trade in latest.get("trades", {}).items()
            ),
            lots=lots_from_fills(fills),
            open_order_ids=tuple(o["id"] for o in open_orders),
            trades=tuple(
                TradeRecord(f.symbol, f.side, f.filled_on, f.shares * f.price, None, f.order_id)
                for f in fills
                if f.filled_on >= (now - timedelta(days=45)).date()
            ),
            tradability=tuple(tradability),
        )

    def submit(self, order: Order, client_order_id: str) -> str:
        body = {
            "symbol": order.symbol,
            "notional": str(order.notional_usd),
            "side": order.side.value,
            "type": "market",
            "time_in_force": "day",
            "extended_hours": False,
            "client_order_id": client_order_id,
        }
        return str(self._call("POST", f"{PAPER_URL}/v2/orders", body)["id"])

    def orders(self) -> list[BrokerOrder]:
        recent = self._closed_orders(datetime.now(UTC) - timedelta(days=7))
        recent += self._call("GET", f"{PAPER_URL}/v2/orders?status=open")
        out = []
        for o in recent:
            qty, price = o.get("filled_qty"), o.get("filled_avg_price")
            filled = Decimal(0) if not qty or not price else to_decimal(qty) * to_decimal(price)
            out.append(
                BrokerOrder(
                    order_id=o["id"],
                    symbol=o["symbol"],
                    side=o["side"],
                    state=o["status"],
                    notional=None if o.get("notional") is None else to_decimal(o["notional"]),
                    filled_notional=filled,
                    created_at=_ts(o["created_at"]),
                    filled_at=_ts(o["filled_at"]) if o.get("filled_at") else None,
                )
            )
        return out

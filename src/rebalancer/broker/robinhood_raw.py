"""Build a Snapshot from Robinhood MCP responses captured by the PostToolUse hook.

PROVISIONAL: Robinhood has not published the response formats of its MCP
tools. Field names below are the likely spellings (based on Robinhood's public
API conventions). Every lookup is explicit and any shape that is not
recognized raises SnapshotError naming the keys it saw, so a format surprise
stops the run instead of producing wrong numbers. Confirm against real
captures before trading live (RUNBOOK.md, "First dry run").
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..ledger import BrokerOrder
from ..models import Holding, Quote, Side, Snapshot, TaxLot, Tradability, TradeRecord
from .mcp_payload import decode
from .snapshot import SnapshotError, _day, _dec, _symbol, _ts

LIST_KEYS = (
    "results",
    "data",
    "items",
    "positions",
    "quotes",
    "lots",
    "tax_lots",
    "orders",
    "historicals",
    "instruments",
    "tradability",
)
NEW_YORK = ZoneInfo("America/New_York")
FILLED_STATES = {"filled", "partially_filled"}
OPEN_STATES = {"queued", "unconfirmed", "confirmed", "pending", "new", "accepted", "partially_filled"}


def _first(item: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in item and item[name] is not None:
            return item[name]
    return None


def _need(item: Mapping[str, Any], names: tuple[str, ...], where: str) -> Any:
    value = _first(item, names)
    if value is None:
        raise SnapshotError(f"{where}: none of {list(names)} present; keys were {sorted(item)}")
    return value


def _records(payload: Any, where: str) -> list[Mapping[str, Any]]:
    """The list of items inside a decoded response."""
    if isinstance(payload, list) and all(isinstance(x, Mapping) for x in payload):
        return payload
    if isinstance(payload, Mapping):
        for key in LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list) and all(isinstance(x, Mapping) for x in value):
                return value
        return [payload]
    raise SnapshotError(f"{where}: unrecognized response shape {type(payload).__name__}")


def load_captures(raw_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Successful captures by tool, oldest first, each with its file path and SHA-256."""
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(raw_dir.glob("*.json")):
        data = path.read_bytes()
        record = json.loads(data)
        if record.get("event") != "PostToolUse":
            continue
        record["_path"] = str(path)
        record["_sha256"] = hashlib.sha256(data).hexdigest()
        by_tool.setdefault(record["tool"], []).append(record)
    return by_tool


def parse_portfolio(payload: Any) -> dict[str, Any]:
    item = _records(payload, "get_portfolio")[0]
    return {
        "account_id": _first(item, ("account_number", "account_id", "account")),
        "cash": _dec(_need(item, ("cash", "cash_balance", "uninvested_cash"), "get_portfolio"), "get_portfolio.cash"),
        "buying_power": None
        if _first(item, ("buying_power",)) is None
        else _dec(item["buying_power"], "get_portfolio.buying_power"),
        "equity": None
        if _first(item, ("equity", "total_equity", "portfolio_value", "total_value")) is None
        else _dec(_first(item, ("equity", "total_equity", "portfolio_value", "total_value")), "get_portfolio.equity"),
    }


def parse_positions(payload: Any) -> list[Holding]:
    out = []
    for i, item in enumerate(_records(payload, "get_equity_positions")):
        where = f"get_equity_positions[{i}]"
        out.append(
            Holding(
                _symbol(_need(item, ("symbol", "ticker"), where), where),
                _dec(_need(item, ("quantity", "shares", "qty"), where), f"{where}.quantity", minimum=Decimal(0)),
            )
        )
    return out


def parse_quotes(payload: Any) -> list[Quote]:
    out = []
    for i, item in enumerate(_records(payload, "get_equity_quotes")):
        where = f"get_equity_quotes[{i}]"
        out.append(
            Quote(
                _symbol(_need(item, ("symbol", "ticker"), where), where),
                _dec(
                    _need(item, ("last_trade_price", "last_price", "price"), where),
                    f"{where}.price",
                    minimum=Decimal(0),
                ),
                _ts(_need(item, ("updated_at", "timestamp", "as_of"), where), f"{where}.updated_at"),
            )
        )
    return out


def parse_tax_lots(payload: Any) -> list[TaxLot]:
    out = []
    for i, item in enumerate(_records(payload, "get_equity_tax_lots")):
        where = f"get_equity_tax_lots[{i}]"
        symbol = _symbol(_need(item, ("symbol", "ticker"), where), where)
        shares = _dec(
            _need(item, ("quantity", "shares", "remaining_quantity"), where), f"{where}.quantity", minimum=Decimal(0)
        )
        per_share = _first(item, ("cost_per_share", "cost_basis_per_share", "unit_cost"))
        if per_share is not None:
            cost = _dec(per_share, f"{where}.cost_per_share", minimum=Decimal(0))
        else:
            total = _dec(_need(item, ("cost_basis", "total_cost"), where), f"{where}.cost_basis", minimum=Decimal(0))
            if shares == 0:
                raise SnapshotError(f"{where}: zero-share lot with a total cost basis")
            cost = total / shares
        out.append(
            TaxLot(
                lot_id=str(_first(item, ("lot_id", "tax_lot_id", "id")) or f"{symbol}-{i}"),
                symbol=symbol,
                acquired=_day(
                    _need(item, ("acquired_date", "purchase_date", "open_date", "acquired_at"), where),
                    f"{where}.acquired",
                ),
                shares=shares,
                cost_per_share=cost,
            )
        )
    return out


def parse_orders(payload: Any) -> list[BrokerOrder]:
    out = []
    for i, item in enumerate(_records(payload, "get_equity_orders")):
        where = f"get_equity_orders[{i}]"
        filled = _first(item, ("filled_notional", "executed_notional"))
        if filled is None:
            qty = _first(item, ("cumulative_quantity", "filled_quantity", "filled_qty"))
            price = _first(item, ("average_price", "filled_avg_price"))
            filled = Decimal(0) if qty is None or price is None else _dec(qty, where) * _dec(price, where)
        notional = _first(item, ("dollar_amount", "notional", "amount"))
        filled_at = _first(item, ("last_transaction_at", "filled_at"))
        side = str(_need(item, ("side",), where)).lower()
        if side not in ("buy", "sell"):
            raise SnapshotError(f"{where}: side must be buy or sell, got {side!r}")
        out.append(
            BrokerOrder(
                order_id=str(_need(item, ("id", "order_id"), where)),
                symbol=_symbol(_need(item, ("symbol", "ticker"), where), where),
                side=side,
                state=str(_need(item, ("state", "status"), where)).lower(),
                notional=None if notional is None else _dec(notional, f"{where}.notional"),
                filled_notional=_dec(filled, f"{where}.filled"),
                created_at=_ts(_need(item, ("created_at", "submitted_at"), where), f"{where}.created_at"),
                filled_at=None if filled_at is None else _ts(filled_at, f"{where}.filled_at"),
            )
        )
    return out


def parse_tradability(payload: Any) -> list[Tradability]:
    out = []
    for i, item in enumerate(_records(payload, "get_equity_tradability")):
        where = f"get_equity_tradability[{i}]"
        tradable = _need(item, ("tradable", "tradeable", "is_tradable", "tradability"), where)
        fractional = _need(item, ("fractional_tradability", "fractional_tradable", "fractional"), where)
        out.append(
            Tradability(
                _symbol(_need(item, ("symbol", "ticker"), where), where),
                tradable is True or str(tradable).lower() in ("true", "tradable"),
                fractional is True or str(fractional).lower() in ("true", "tradable"),
            )
        )
    return out


def parse_historicals(payload: Any) -> dict[str, tuple[tuple[Any, Decimal], ...]]:
    out: dict[str, tuple[tuple[Any, Decimal], ...]] = {}
    for i, item in enumerate(_records(payload, "get_equity_historicals")):
        where = f"get_equity_historicals[{i}]"
        symbol = _symbol(_need(item, ("symbol", "ticker"), where), where)
        points = _need(item, ("historicals", "bars", "data"), where)
        if not isinstance(points, list):
            raise SnapshotError(f"{where}: historicals must be a list")
        out[symbol] = tuple(
            (
                _day(_need(p, ("begins_at", "date", "timestamp"), where), where),
                _dec(_need(p, ("close_price", "close"), where), where, minimum=Decimal(0)),
            )
            for p in points
        )
    return out


def build_snapshot(raw_dir: Path, now: datetime) -> tuple[Snapshot, list[dict[str, str]]]:
    """Assemble today's snapshot from the latest capture of each tool.

    Quotes, lots, tradability and historicals may come from several calls
    (e.g. one per symbol); later captures override earlier ones per symbol.
    """
    if not raw_dir.is_dir():
        raise SnapshotError(f"no captured responses in {raw_dir}")
    captures = load_captures(raw_dir)
    missing = [
        t
        for t in (
            "get_portfolio",
            "get_equity_positions",
            "get_equity_quotes",
            "get_equity_tax_lots",
            "get_equity_orders",
            "get_equity_tradability",
        )
        if t not in captures
    ]
    if missing:
        raise SnapshotError(f"missing captured responses for: {missing}")

    def decoded(record: Mapping[str, Any]) -> Any:
        return decode(record["tool_response"])

    portfolio = parse_portfolio(decoded(captures["get_portfolio"][-1]))
    holdings = parse_positions(decoded(captures["get_equity_positions"][-1]))
    quotes: dict[str, Quote] = {}
    for record in captures["get_equity_quotes"]:
        quotes.update({q.symbol: q for q in parse_quotes(decoded(record))})
    lots_by_symbol: dict[str, list[TaxLot]] = {}
    for record in captures["get_equity_tax_lots"]:
        parsed = parse_tax_lots(decoded(record))
        for symbol in {lot.symbol for lot in parsed}:
            lots_by_symbol[symbol] = [lot for lot in parsed if lot.symbol == symbol]
    tradability: dict[str, Tradability] = {}
    for record in captures["get_equity_tradability"]:
        tradability.update({t.symbol: t for t in parse_tradability(decoded(record))})
    closes: dict[str, Any] = {}
    for record in captures.get("get_equity_historicals", []):
        closes.update(parse_historicals(decoded(record)))
    orders = parse_orders(decoded(captures["get_equity_orders"][-1]))

    snapshot = Snapshot(
        as_of=now,
        account_id=None if portfolio["account_id"] is None else str(portfolio["account_id"]),
        cash=portfolio["cash"],
        buying_power=portfolio["buying_power"],
        reported_equity=portfolio["equity"],
        holdings=tuple(holdings),
        quotes=tuple(quotes.values()),
        lots=tuple(lot for symbol in sorted(lots_by_symbol) for lot in lots_by_symbol[symbol]),
        open_order_ids=tuple(o.order_id for o in orders if o.state in OPEN_STATES),
        trades=tuple(
            TradeRecord(
                o.symbol,
                Side(o.side),
                (o.filled_at or o.created_at).astimezone(NEW_YORK).date(),
                o.filled_notional,
                None,
                o.order_id,
            )
            for o in orders
            if o.state in FILLED_STATES or (o.filled_notional > 0)
        ),
        tradability=tuple(tradability.values()),
        closes=closes,
    )
    sources = [
        {"path": r["_path"], "tool": r["tool"], "sha256": r["_sha256"]}
        for records in captures.values()
        for r in records
    ]
    return snapshot, sources


def broker_orders(raw_dir: Path) -> list[BrokerOrder]:
    """Orders from the latest captured get_equity_orders response (for reconcile)."""
    captures = load_captures(raw_dir) if raw_dir.is_dir() else {}
    if "get_equity_orders" not in captures:
        raise SnapshotError(f"no captured get_equity_orders response in {raw_dir}")
    return parse_orders(decode(captures["get_equity_orders"][-1]["tool_response"]))

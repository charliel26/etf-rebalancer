"""Canonical snapshot.json <-> Snapshot, with strict validation.

The canonical file is the stable contract between brokers and the planner.
Numbers are strings (exact decimals); timestamps must carry a timezone.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..models import Holding, Quote, Side, Snapshot, TaxLot, Tradability, TradeRecord
from ..money import to_decimal

SCHEMA_VERSION = 1


class SnapshotError(ValueError):
    pass


def _dec(value: Any, where: str, *, minimum: Decimal | None = None) -> Decimal:
    try:
        result = to_decimal(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotError(f"{where}: {exc}") from exc
    if minimum is not None and result < minimum:
        raise SnapshotError(f"{where}: must be >= {minimum}, got {result}")
    return result


def _ts(value: Any, where: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotError(f"{where}: not an ISO timestamp: {value!r}") from exc
    if result.tzinfo is None:
        raise SnapshotError(f"{where}: timestamp needs a timezone: {value!r}")
    return result


def _day(value: Any, where: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise SnapshotError(f"{where}: not a date: {value!r}") from exc


def _symbol(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotError(f"{where}: missing symbol")
    return value.strip().upper()


def _items(doc: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    items = doc.get(key, [])
    if not isinstance(items, list) or not all(isinstance(x, Mapping) for x in items):
        raise SnapshotError(f"{key}: expected a list of objects")
    return items


def snapshot_from_dict(doc: Mapping[str, Any]) -> Snapshot:
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError(f"unsupported snapshot schema_version {doc.get('schema_version')!r}")
    account = doc.get("account")
    if not isinstance(account, Mapping):
        raise SnapshotError("account: expected an object")
    buying_power = account.get("buying_power")
    equity = account.get("equity")
    trades = []
    for i, t in enumerate(_items(doc, "trades")):
        side = str(t.get("side", "")).lower()
        if side not in ("buy", "sell"):
            raise SnapshotError(f"trades[{i}].side: expected buy or sell")
        had_loss = t.get("had_loss")
        if had_loss is not None and not isinstance(had_loss, bool):
            raise SnapshotError(f"trades[{i}].had_loss: expected true, false or null")
        trades.append(
            TradeRecord(
                symbol=_symbol(t.get("symbol"), f"trades[{i}]"),
                side=Side(side),
                trade_date=_day(t.get("date"), f"trades[{i}].date"),
                notional=_dec(t.get("notional"), f"trades[{i}].notional", minimum=Decimal(0)),
                had_loss=had_loss,
                order_id=None if t.get("order_id") is None else str(t["order_id"]),
            )
        )
    closes_raw = doc.get("closes", {})
    if not isinstance(closes_raw, Mapping):
        raise SnapshotError("closes: expected an object")
    closes = {
        _symbol(sym, "closes"): tuple(
            (_day(p[0], f"closes.{sym}"), _dec(p[1], f"closes.{sym}", minimum=Decimal(0))) for p in points
        )
        for sym, points in closes_raw.items()
    }
    return Snapshot(
        as_of=_ts(doc.get("as_of"), "as_of"),
        account_id=None if account.get("account_id") is None else str(account["account_id"]),
        cash=_dec(account.get("cash"), "account.cash"),
        buying_power=None if buying_power is None else _dec(buying_power, "account.buying_power"),
        reported_equity=None if equity is None else _dec(equity, "account.equity"),
        holdings=tuple(
            Holding(
                _symbol(h.get("symbol"), f"positions[{i}]"),
                _dec(h.get("shares"), f"positions[{i}].shares", minimum=Decimal(0)),
            )
            for i, h in enumerate(_items(doc, "positions"))
        ),
        quotes=tuple(
            Quote(
                _symbol(q.get("symbol"), f"quotes[{i}]"),
                _dec(q.get("price"), f"quotes[{i}].price", minimum=Decimal(0)),
                _ts(q.get("as_of"), f"quotes[{i}].as_of"),
            )
            for i, q in enumerate(_items(doc, "quotes"))
        ),
        lots=tuple(
            TaxLot(
                lot_id=str(lot.get("lot_id") or f"{lot.get('symbol')}-{i}"),
                symbol=_symbol(lot.get("symbol"), f"tax_lots[{i}]"),
                acquired=_day(lot.get("acquired"), f"tax_lots[{i}].acquired"),
                shares=_dec(lot.get("shares"), f"tax_lots[{i}].shares", minimum=Decimal(0)),
                cost_per_share=_dec(lot.get("cost_per_share"), f"tax_lots[{i}].cost_per_share", minimum=Decimal(0)),
            )
            for i, lot in enumerate(_items(doc, "tax_lots"))
        ),
        open_order_ids=tuple(str(o.get("order_id")) for o in _items(doc, "open_orders")),
        trades=tuple(trades),
        tradability=tuple(
            Tradability(
                _symbol(t.get("symbol"), f"tradability[{i}]"), bool(t.get("tradable")), bool(t.get("fractional"))
            )
            for i, t in enumerate(_items(doc, "tradability"))
        ),
        closes=closes,
    )


def snapshot_to_dict(snapshot: Snapshot, sources: list[Mapping[str, str]] | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": snapshot.as_of.isoformat(),
        "account": {
            "account_id": snapshot.account_id,
            "cash": str(snapshot.cash),
            "buying_power": None if snapshot.buying_power is None else str(snapshot.buying_power),
            "equity": None if snapshot.reported_equity is None else str(snapshot.reported_equity),
        },
        "positions": [{"symbol": h.symbol, "shares": str(h.shares)} for h in snapshot.holdings],
        "quotes": [{"symbol": q.symbol, "price": str(q.price), "as_of": q.as_of.isoformat()} for q in snapshot.quotes],
        "tax_lots": [
            {
                "lot_id": lot.lot_id,
                "symbol": lot.symbol,
                "acquired": lot.acquired.isoformat(),
                "shares": str(lot.shares),
                "cost_per_share": str(lot.cost_per_share),
            }
            for lot in snapshot.lots
        ],
        "open_orders": [{"order_id": o} for o in snapshot.open_order_ids],
        "trades": [
            {
                "symbol": t.symbol,
                "side": t.side.value,
                "date": t.trade_date.isoformat(),
                "notional": str(t.notional),
                "had_loss": t.had_loss,
                "order_id": t.order_id,
            }
            for t in snapshot.trades
        ],
        "tradability": [
            {"symbol": t.symbol, "tradable": t.tradable, "fractional": t.fractional} for t in snapshot.tradability
        ],
        "closes": {s: [[d.isoformat(), str(p)] for d, p in points] for s, points in snapshot.closes.items()},
        "sources": list(sources or []),
    }


def save_snapshot(snapshot: Snapshot, path: Path, sources: list[Mapping[str, str]] | None = None) -> str:
    """Write the canonical file; return its SHA-256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snapshot_to_dict(snapshot, sources), indent=1, sort_keys=True)
    path.write_text(text)
    return hashlib.sha256(text.encode()).hexdigest()


def load_snapshot(path: Path) -> tuple[Snapshot, str]:
    """Read and validate the canonical file; return it and its SHA-256."""
    try:
        text = path.read_text()
        doc = json.loads(text)
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"cannot read snapshot {path}: {exc}") from exc
    if not isinstance(doc, Mapping):
        raise SnapshotError("snapshot is not a JSON object")
    return snapshot_from_dict(doc), hashlib.sha256(text.encode()).hexdigest()

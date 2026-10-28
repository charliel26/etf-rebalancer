"""SQLite ledger: runs, plan lines and their life cycle, approvals, trades, events.

Amounts are stored as Decimal strings. One live run per New York date is
enforced by a unique index. Claiming a line for an order happens in a single
`BEGIN IMMEDIATE` transaction together with the match and the daily-limit
check, so two parallel order calls cannot both pass.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import Side, TradeRecord
from .money import CENT, ZERO, to_decimal

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    run_date TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    plan_sha256 TEXT NOT NULL,
    plan_path TEXT NOT NULL,
    requires_approval INTEGER NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_run_per_date ON runs(run_date) WHERE mode = 'live';
CREATE TABLE IF NOT EXISTS plan_lines (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    line_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    notional_usd TEXT NOT NULL,
    reason TEXT NOT NULL,
    est_had_loss INTEGER,
    status TEXT NOT NULL DEFAULT 'PENDING',
    tool_use_id TEXT,
    broker_order_id TEXT,
    claimed_at TEXT,
    updated_at TEXT,
    filled_notional_usd TEXT,
    PRIMARY KEY (run_id, line_id)
);
CREATE TABLE IF NOT EXISTS approvals (
    run_id TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    approved_on TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    PRIMARY KEY (run_id, plan_sha256)
);
CREATE TABLE IF NOT EXISTS trades (
    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_order_id TEXT UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    trade_date TEXT NOT NULL,
    notional_usd TEXT NOT NULL,
    had_loss INTEGER,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS annual_resets (
    year INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('executed', 'dismissed')),
    run_id TEXT,
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    run_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

# Statuses whose notional counts against the daily limit. Uncertain outcomes
# (CLAIMED, SUBMIT_FAILED) count, which is the conservative side.
COUNTED = ("CLAIMED", "SUBMITTED", "SUBMIT_FAILED", "FILLED", "PARTIAL", "CANCELLED")
REARMABLE = ("NOT_PLACED", "SUBMIT_FAILED", "REJECTED", "CANCELLED")


class LedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunRow:
    run_id: str
    run_date: date
    mode: str
    plan_sha256: str
    plan_path: str
    requires_approval: bool
    action: str


@dataclass(frozen=True)
class LineRow:
    run_id: str
    line_id: str
    symbol: str
    side: str
    notional_usd: Decimal
    reason: str
    status: str
    tool_use_id: str | None
    broker_order_id: str | None
    claimed_at: str | None
    est_had_loss: bool | None


@dataclass(frozen=True)
class ClaimResult:
    ok: bool
    reason: str
    line_id: str | None = None


@dataclass(frozen=True)
class BrokerOrder:
    """A broker order as reported by get_equity_orders (or Alpaca)."""

    order_id: str
    symbol: str
    side: str
    state: str
    notional: Decimal | None
    filled_notional: Decimal
    created_at: datetime
    filled_at: datetime | None = None


class Ledger:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # ------------------------------------------------------------ events

    def log(self, kind: str, detail: Mapping[str, Any] | str, now: datetime, run_id: str | None = None) -> None:
        text = detail if isinstance(detail, str) else json.dumps(detail, sort_keys=True, default=str)
        self._db.execute(
            "INSERT INTO events (at, run_id, kind, detail) VALUES (?, ?, ?, ?)",
            (now.isoformat(), run_id, kind, text),
        )

    def events(self, run_id: str | None = None) -> list[sqlite3.Row]:
        if run_id is None:
            return list(self._db.execute("SELECT * FROM events ORDER BY event_id"))
        return list(self._db.execute("SELECT * FROM events WHERE run_id = ? ORDER BY event_id", (run_id,)))

    # ------------------------------------------------------------ runs and lines

    def record_run(self, plan: Mapping[str, Any], plan_path: str, now: datetime) -> None:
        """Store a plan and its lines. A second live run for the same date is refused."""
        had_loss = {
            symbol: any(item["gain_usd"] < 0 for item in picks)
            for symbol, picks in plan.get("tax_estimate", {}).get("lots", {}).items()
        }
        try:
            with self._tx() as db:
                db.execute(
                    "INSERT INTO runs (run_id, run_date, mode, plan_sha256, plan_path, requires_approval,"
                    " action, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        plan["run_id"],
                        plan["date"],
                        plan["mode"],
                        plan["plan_sha256"],
                        plan_path,
                        int(bool(plan["requires_approval"])),
                        plan["action"],
                        now.isoformat(),
                    ),
                )
                for order in plan["orders"]:
                    loss = had_loss.get(order["symbol"]) if order["side"] == "sell" else None
                    db.execute(
                        "INSERT INTO plan_lines (run_id, line_id, symbol, side, notional_usd, reason,"
                        " est_had_loss, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            plan["run_id"],
                            order["line_id"],
                            order["symbol"],
                            order["side"],
                            str(to_decimal(order["notional_usd"]).quantize(CENT)),
                            order["reason"],
                            None if loss is None else int(loss),
                            now.isoformat(),
                        ),
                    )
                self.log("run_recorded", {"mode": plan["mode"], "orders": len(plan["orders"])}, now, plan["run_id"])
        except sqlite3.IntegrityError as exc:
            raise LedgerError(f"cannot record run {plan['run_id']}: {exc}") from exc

    @staticmethod
    def _run(row: sqlite3.Row) -> RunRow:
        return RunRow(
            run_id=row["run_id"],
            run_date=date.fromisoformat(row["run_date"]),
            mode=row["mode"],
            plan_sha256=row["plan_sha256"],
            plan_path=row["plan_path"],
            requires_approval=bool(row["requires_approval"]),
            action=row["action"],
        )

    def run(self, run_id: str) -> RunRow | None:
        row = self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return None if row is None else self._run(row)

    def live_run_for(self, day: date) -> RunRow | None:
        row = self._db.execute("SELECT * FROM runs WHERE run_date = ? AND mode = 'live'", (day.isoformat(),)).fetchone()
        return None if row is None else self._run(row)

    @staticmethod
    def _line(row: sqlite3.Row) -> LineRow:
        loss = row["est_had_loss"]
        return LineRow(
            run_id=row["run_id"],
            line_id=row["line_id"],
            symbol=row["symbol"],
            side=row["side"],
            notional_usd=Decimal(row["notional_usd"]),
            reason=row["reason"],
            status=row["status"],
            tool_use_id=row["tool_use_id"],
            broker_order_id=row["broker_order_id"],
            claimed_at=row["claimed_at"],
            est_had_loss=None if loss is None else bool(loss),
        )

    def lines(self, run_id: str) -> list[LineRow]:
        rows = self._db.execute(
            "SELECT * FROM plan_lines WHERE run_id = ? ORDER BY CAST(SUBSTR(line_id, 2) AS INTEGER)",
            (run_id,),
        )
        return [self._line(r) for r in rows]

    def _notional_used(self, db: sqlite3.Connection, day: date) -> Decimal:
        placeholders = ",".join("?" for _ in COUNTED)
        rows = db.execute(
            "SELECT l.notional_usd FROM plan_lines l JOIN runs r ON r.run_id = l.run_id"
            f" WHERE r.run_date = ? AND r.mode = 'live' AND l.status IN ({placeholders})",
            (day.isoformat(), *COUNTED),
        )
        return sum((Decimal(r["notional_usd"]) for r in rows), ZERO)

    def notional_used(self, day: date) -> Decimal:
        return self._notional_used(self._db, day)

    def claim_line(
        self,
        run_id: str,
        symbol: str,
        side: str,
        amount: Decimal,
        *,
        tolerance: Decimal,
        daily_cap: Decimal,
        run_date: date,
        now: datetime,
        tool_use_id: str | None,
    ) -> ClaimResult:
        """Atomically match exactly one PENDING line and mark it CLAIMED."""
        with self._tx() as db:
            candidates = [
                r
                for r in db.execute(
                    "SELECT line_id, notional_usd FROM plan_lines"
                    " WHERE run_id = ? AND symbol = ? AND side = ? AND status = 'PENDING'",
                    (run_id, symbol, side),
                )
                if abs(Decimal(r["notional_usd"]) - amount) <= tolerance
            ]
            if len(candidates) != 1:
                return ClaimResult(
                    False,
                    f"order {side} {symbol} {amount} matches {len(candidates)} pending plan lines (need exactly 1)",
                )
            used = self._notional_used(db, run_date)
            if used + amount > daily_cap:
                return ClaimResult(False, f"daily notional limit: {used} used + {amount} > {daily_cap}")
            line_id = candidates[0]["line_id"]
            db.execute(
                "UPDATE plan_lines SET status = 'CLAIMED', claimed_at = ?, updated_at = ?, tool_use_id = ?"
                " WHERE run_id = ? AND line_id = ? AND status = 'PENDING'",
                (now.isoformat(), now.isoformat(), tool_use_id, run_id, line_id),
            )
            self.log("line_claimed", {"line_id": line_id, "amount": str(amount)}, now, run_id)
        return ClaimResult(True, "claimed", line_id)

    def mark_submitted(self, tool_use_id: str, broker_order_id: str | None, now: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE plan_lines SET status = 'SUBMITTED', broker_order_id = ?, updated_at = ?"
            " WHERE tool_use_id = ? AND status = 'CLAIMED'",
            (broker_order_id, now.isoformat(), tool_use_id),
        )
        return cursor.rowcount == 1

    def mark_submit_failed(self, tool_use_id: str, error: str, now: datetime) -> bool:
        cursor = self._db.execute(
            "UPDATE plan_lines SET status = 'SUBMIT_FAILED', updated_at = ?"
            " WHERE tool_use_id = ? AND status = 'CLAIMED'",
            (now.isoformat(), tool_use_id),
        )
        if cursor.rowcount == 1:
            self.log("submit_failed", {"tool_use_id": tool_use_id, "error": error[:500]}, now)
        return cursor.rowcount == 1

    def set_line_status(
        self, run_id: str, line_id: str, status: str, now: datetime, filled: Decimal | None = None
    ) -> None:
        self._db.execute(
            "UPDATE plan_lines SET status = ?, updated_at = ?, filled_notional_usd = COALESCE(?, filled_notional_usd)"
            " WHERE run_id = ? AND line_id = ?",
            (status, now.isoformat(), None if filled is None else str(filled), run_id, line_id),
        )

    def rearm_line(self, run_id: str, line_id: str, now: datetime) -> None:
        line = next((x for x in self.lines(run_id) if x.line_id == line_id), None)
        if line is None:
            raise LedgerError(f"no line {line_id} in run {run_id}")
        if line.status not in REARMABLE:
            raise LedgerError(f"line {line_id} is {line.status}; only {REARMABLE} can be re-armed")
        self._db.execute(
            "UPDATE plan_lines SET status = 'PENDING', tool_use_id = NULL, broker_order_id = NULL,"
            " claimed_at = NULL, updated_at = ? WHERE run_id = ? AND line_id = ?",
            (now.isoformat(), run_id, line_id),
        )
        self.log("line_rearmed", {"line_id": line_id, "previous": line.status}, now, run_id)

    # ------------------------------------------------------------ reconciliation

    def reconcile(
        self,
        run_id: str,
        orders: Iterable[BrokerOrder],
        now: datetime,
        *,
        tolerance: Decimal,
        not_placed_after_minutes: int = 5,
    ) -> dict[str, str]:
        """Update line statuses from broker orders. Returns {line_id: new status}."""
        orders = list(orders)
        by_id = {o.order_id: o for o in orders}
        linked = {line.broker_order_id for line in self.lines(run_id) if line.broker_order_id}
        changes: dict[str, str] = {}
        for line in self.lines(run_id):
            if line.status in ("PENDING", "FILLED", "REJECTED", "NOT_PLACED"):
                continue
            order = by_id.get(line.broker_order_id) if line.broker_order_id else None
            if order is None and line.status in ("CLAIMED", "SUBMIT_FAILED", "SUBMITTED"):
                order = self._find_unlinked(line, orders, linked, tolerance)
                if order is not None:
                    linked.add(order.order_id)
                    self._db.execute(
                        "UPDATE plan_lines SET broker_order_id = ? WHERE run_id = ? AND line_id = ?",
                        (order.order_id, run_id, line.line_id),
                    )
            if order is None:
                claimed = datetime.fromisoformat(line.claimed_at) if line.claimed_at else now
                if line.status == "CLAIMED" and (now - claimed).total_seconds() > 60 * not_placed_after_minutes:
                    changes[line.line_id] = "NOT_PLACED"
                continue
            changes[line.line_id] = _status_for(order)
            if changes[line.line_id] in ("FILLED", "PARTIAL"):
                self.record_trade(
                    TradeRecord(
                        symbol=order.symbol,
                        side=Side(order.side),
                        trade_date=(order.filled_at or order.created_at).date(),
                        notional=order.filled_notional,
                        had_loss=line.est_had_loss if order.side == "sell" else None,
                        order_id=order.order_id,
                    ),
                    source="reconcile",
                )
        for line_id, status in changes.items():
            filled = by_id.get(self._broker_id(run_id, line_id) or "")
            self.set_line_status(run_id, line_id, status, now, filled.filled_notional if filled else None)
        if changes:
            self.log("reconciled", changes, now, run_id)
        return changes

    def _broker_id(self, run_id: str, line_id: str) -> str | None:
        row = self._db.execute(
            "SELECT broker_order_id FROM plan_lines WHERE run_id = ? AND line_id = ?", (run_id, line_id)
        ).fetchone()
        return row["broker_order_id"]

    @staticmethod
    def _find_unlinked(
        line: LineRow, orders: list[BrokerOrder], linked: set[str | None], tolerance: Decimal
    ) -> BrokerOrder | None:
        claimed = datetime.fromisoformat(line.claimed_at) if line.claimed_at else None
        for order in orders:
            if order.order_id in linked or order.symbol != line.symbol or order.side != line.side:
                continue
            if order.notional is None or abs(order.notional - line.notional_usd) > tolerance:
                continue
            if claimed is not None and (order.created_at - claimed).total_seconds() < -60:
                continue
            return order
        return None

    # ------------------------------------------------------------ approvals

    def record_approval(self, run_id: str, plan_sha256: str, approved_on: date, now: datetime) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO approvals (run_id, plan_sha256, approved_on, approved_at) VALUES (?, ?, ?, ?)",
            (run_id, plan_sha256, approved_on.isoformat(), now.isoformat()),
        )
        self.log("approved", {"plan_sha256": plan_sha256}, now, run_id)

    def has_approval(self, run_id: str, plan_sha256: str, on: date) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM approvals WHERE run_id = ? AND plan_sha256 = ? AND approved_on = ?",
            (run_id, plan_sha256, on.isoformat()),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------ trades and resets

    def record_trade(self, trade: TradeRecord, source: str) -> bool:
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO trades (broker_order_id, symbol, side, trade_date, notional_usd, had_loss, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                trade.order_id,
                trade.symbol,
                trade.side.value,
                trade.trade_date.isoformat(),
                str(trade.notional),
                None if trade.had_loss is None else int(trade.had_loss),
                source,
            ),
        )
        return cursor.rowcount == 1

    def trades_since(self, day: date) -> list[TradeRecord]:
        rows = self._db.execute(
            "SELECT * FROM trades WHERE trade_date >= ? ORDER BY trade_date, trade_id", (day.isoformat(),)
        )
        return [
            TradeRecord(
                symbol=r["symbol"],
                side=Side(r["side"]),
                trade_date=date.fromisoformat(r["trade_date"]),
                notional=Decimal(r["notional_usd"]),
                had_loss=None if r["had_loss"] is None else bool(r["had_loss"]),
                order_id=r["broker_order_id"],
            )
            for r in rows
        ]

    def record_reset(self, year: int, status: str, run_id: str | None, now: datetime) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO annual_resets (year, status, run_id, decided_at) VALUES (?, ?, ?, ?)",
            (year, status, run_id, now.isoformat()),
        )
        self.log("annual_reset", {"year": year, "status": status}, now, run_id)

    def completed_reset_years(self) -> frozenset[int]:
        return frozenset(r["year"] for r in self._db.execute("SELECT year FROM annual_resets"))


def _status_for(order: BrokerOrder) -> str:
    state = order.state.lower()
    if state == "filled":
        return "FILLED"
    if state in ("rejected", "failed"):
        return "REJECTED"
    if state in ("cancelled", "canceled", "expired"):
        return "PARTIAL" if order.filled_notional > 0 else "CANCELLED"
    if state in ("partially_filled", "partial"):
        return "PARTIAL"
    # Open (queued, confirmed, ...) or unknown: keep it open and counted; never assume it failed.
    return "SUBMITTED"

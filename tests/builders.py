"""Builders shared by tests: snapshots, temp projects with a live plan, payloads."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import yaml

from rebalancer.capture import capture
from rebalancer.config import load_config
from rebalancer.integrity import write_armed
from rebalancer.ledger import Ledger
from rebalancer.models import Holding, Quote, Snapshot, TaxLot, Tradability
from rebalancer.plan import PlanContext, build_plan, plan_to_dict
from tests.conftest import REPO_ROOT, TARGETS, D

TODAY = date(2026, 11, 2)  # a Monday, Eastern Standard Time
NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)  # 10:00 New York
PRICES = {"AVUV": D(100), "QQQM": D(200), "VTI": D(300), "VXUS": D(60)}
OLD = date(2024, 1, 2)
LIVE_ENV = {"TRADING_ENABLED": "true", "DRY_RUN": "false"}
ORDER_TOOL = "mcp__robinhood-trading__place_equity_order"
REVIEW_TOOL = "mcp__robinhood-trading__review_equity_order"


def vals(vti=0, vxus=0, qqqm=0, avuv=0) -> dict[str, Decimal]:
    return {"VTI": D(vti), "VXUS": D(vxus), "QQQM": D(qqqm), "AVUV": D(avuv)}


def on_target_shares() -> dict[str, Decimal]:
    return {s: v / PRICES[s] for s, v in vals(5000, 2000, 1500, 1500).items()}


def make_snapshot(
    shares=None,
    cash="0",
    buying_power=None,
    quotes=None,
    lots=None,
    trades=(),
    open_orders=(),
    tradability=None,
    as_of=NOW,
    extra_holdings=(),
    equity=None,
) -> Snapshot:
    shares = {s: D(v) for s, v in (shares or {}).items()}
    holdings = tuple(Holding(s, q) for s, q in shares.items()) + tuple(extra_holdings)
    quote_prices = PRICES if quotes is None else quotes
    if lots is None:
        lots = tuple(TaxLot(f"{s}-1", s, OLD, q, PRICES[s]) for s, q in shares.items() if q > 0)
    if tradability is None:
        tradability = tuple(Tradability(s, True, True) for s in TARGETS)
    return Snapshot(
        as_of=as_of,
        account_id="ACC1",
        cash=D(cash),
        buying_power=None if buying_power is None else D(buying_power),
        holdings=holdings,
        quotes=tuple(Quote(s, p, as_of) for s, p in quote_prices.items()),
        lots=tuple(lots),
        open_order_ids=tuple(open_orders),
        trades=tuple(trades),
        tradability=tuple(tradability),
        reported_equity=None if equity is None else D(equity),
    )


def session(day: date) -> tuple[datetime, datetime] | None:
    """Fake NYSE calendar for EST dates: 09:30-16:00 on weekdays."""
    if day.weekday() >= 5:
        return None
    return (
        datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC),
        datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
    )


def make_project(tmp_path: Path, strategy: dict | None = None, limits: dict | None = None) -> Path:
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "state").mkdir()
    for name, overrides in (("targets.yaml", strategy), ("limits.yaml", limits)):
        doc = yaml.safe_load((REPO_ROOT / "config" / name).read_text())
        doc.update(overrides or {})
        (project / "config" / name).write_text(yaml.safe_dump(doc))
    (project / "hooks").mkdir()
    for name in ("guard_order.py", "capture_mcp.py"):
        shutil.copy(REPO_ROOT / "hooks" / name, project / "hooks" / name)
    return project


def arm(project: Path) -> None:
    write_armed(project, project / "state", NOW)


def record_live_plan(project: Path, snapshot: Snapshot | None = None, now: datetime = NOW, **ctx_overrides) -> dict:
    config = load_config(project / "config")
    snapshot = snapshot or make_snapshot(on_target_shares(), cash=1000)
    ctx = PlanContext(today=now.date(), now=now, live_requested=True, **ctx_overrides)
    doc = plan_to_dict(build_plan(snapshot, config, ctx))
    plans = project / "state" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / f"{doc['run_id']}.json"
    path.write_text(json.dumps(doc, indent=2))
    with Ledger(project / "state" / "ledger.sqlite3") as ledger:
        ledger.record_run(doc, path.relative_to(project).as_posix(), now)
    return doc


def order_payload(symbol="VTI", side="buy", amount="500.00", tool_use_id="toolu_1", **extra) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": ORDER_TOOL,
        "tool_input": {"symbol": symbol, "side": side, "amount": amount, **extra},
        "tool_use_id": tool_use_id,
    }


def capture_review(
    project: Path, symbol="VTI", side="buy", amount="500.00", now: datetime = NOW, event="PostToolUse"
) -> None:
    capture(
        {
            "hook_event_name": event,
            "tool_name": REVIEW_TOOL,
            "tool_input": {"symbol": symbol, "side": side, "amount": amount},
            "tool_response": {"content": [{"type": "text", "text": '{"ok": true}'}]},
            "tool_use_id": f"review-{symbol}-{side}-{amount}",
        },
        project / "state",
        now,
    )

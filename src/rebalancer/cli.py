"""`rebalancer` command line. Glue only: reads the clock and files, calls the
pure planner, writes plan/report files and the ledger.

Commands that grant authority (approve, arm, rearm, dismiss-reset) only work
in an interactive terminal and ask you to type a confirmation code; Claude
Code's Bash tool has no terminal, and settings.json denies them as well.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .broker.robinhood_raw import broker_orders, build_snapshot
from .broker.snapshot import SnapshotError, load_snapshot, save_snapshot
from .capture import raw_dir_for
from .config import Config, ConfigError, load_config
from .guard import OrderRequest, guard_and_claim
from .integrity import compute_manifest, verify_armed, write_armed
from .ledger import Ledger, LedgerError
from .models import Snapshot, TradeRecord
from .nyse import SessionBounds, execution_window, session_bounds
from .plan import PlanContext, build_plan, plan_to_dict, verify_plan_dict
from .report import render_report
from .risk import assess

NEW_YORK = ZoneInfo("America/New_York")


class CliError(RuntimeError):
    pass


@dataclass
class Runtime:
    project: Path
    now: datetime
    environ: Mapping[str, str]
    input_fn: Callable[[str], str]
    is_tty: bool
    out: Callable[[str], None]
    bounds: SessionBounds = session_bounds

    @property
    def state(self) -> Path:
        return self.project / "state"

    @property
    def today(self) -> date:
        return self.now.astimezone(NEW_YORK).date()

    @property
    def live_requested(self) -> bool:
        return self.environ.get("DRY_RUN") == "false" and self.environ.get("TRADING_ENABLED") == "true"


def find_project(start: Path, environ: Mapping[str, str]) -> Path:
    explicit = environ.get("CLAUDE_PROJECT_DIR")
    candidates = [Path(explicit)] if explicit else [start, *start.parents]
    for path in candidates:
        if (path / "config" / "targets.yaml").exists():
            return path
    raise CliError("cannot find the project (a directory with config/targets.yaml)")


def _require_tty(rt: Runtime, what: str) -> None:
    if not rt.is_tty:
        raise CliError(f"`{what}` must be run by a person in an interactive terminal")


def _config_sha(project: Path) -> str:
    digest = hashlib.sha256()
    for name in ("targets.yaml", "limits.yaml"):
        digest.update((project / "config" / name).read_bytes())
    return digest.hexdigest()


def merge_trades(broker: Sequence[TradeRecord], ledger: Sequence[TradeRecord]) -> tuple[TradeRecord, ...]:
    """Broker fills, with the ledger's had_loss estimate for our own sales; plus
    ledger-only records (e.g. manual entries)."""
    known = {t.order_id: t for t in ledger if t.order_id}
    merged = [replace(t, had_loss=known[t.order_id].had_loss) if t.order_id in known else t for t in broker]
    broker_ids = {t.order_id for t in broker if t.order_id}
    merged += [t for t in ledger if not t.order_id or t.order_id not in broker_ids]
    return tuple(merged)


def _window_open(rt: Runtime, config: Config) -> bool:
    window = execution_window(rt.today, config.limits, rt.bounds)
    return window is not None and window[0] <= rt.now <= window[1]


def make_plan(
    rt: Runtime, config: Config, snapshot: Snapshot, snapshot_sha: str, state_dir: Path, *, annual_reset: bool
) -> tuple[dict, Path]:
    # A live plan is only made when its orders could be placed now, so a plan made
    # before the window opens can't use up the day's one live run.
    window_open = _window_open(rt, config) if rt.live_requested else True
    with Ledger(state_dir / "ledger.sqlite3") as ledger:
        trades = merge_trades(snapshot.trades, ledger.trades_since(rt.today - timedelta(days=45)))
        snapshot = replace(snapshot, trades=trades)
        existing = ledger.live_run_for(rt.today)
        ctx = PlanContext(
            today=rt.today,
            now=rt.now,
            live_requested=rt.live_requested,
            trading_window_open=window_open,
            kill_switch=(rt.project / "KILL").exists(),
            live_run_exists_today=existing is not None,
            notional_used_today=ledger.notional_used(rt.today),
            completed_reset_years=ledger.completed_reset_years(),
            annual_reset_requested=annual_reset,
            snapshot_sha256=snapshot_sha,
            config_sha256=_config_sha(rt.project),
            code_version=__version__,
        )
        doc = plan_to_dict(build_plan(snapshot, config, ctx))
        plans = state_dir / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        plan_path = plans / f"{doc['run_id']}.json"
        text = json.dumps(doc, indent=2, sort_keys=True)
        plan_path.write_text(text)
        (state_dir / "plan.json").write_text(text)
        if ledger.run(doc["run_id"]) is None:
            ledger.record_run(doc, plan_path.relative_to(rt.project).as_posix(), rt.now)

    values = {
        h.symbol: float(h.shares * q.price)
        for h in snapshot.holdings
        if (q := snapshot.quote(h.symbol)) is not None and h.shares > 0
    }
    risk = assess(values, snapshot.closes, doc["portfolio"]["satellite_share"], config.strategy.risk)
    reports = state_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f"{doc['run_id']}.md"
    report_path.write_text(render_report(doc, risk))
    return doc, report_path


def _summary(rt: Runtime, doc: dict, report_path: Path, existing_live: str | None) -> None:
    rt.out(f"plan {doc['run_id']}  mode={doc['mode']}  action={doc['action']}")
    for order in doc["orders"]:
        rt.out(
            f"  {order['line_id']}: {order['side']} {order['symbol']} ${order['notional_usd']:,.2f} ({order['reason']})"
        )
    if not doc["orders"]:
        rt.out("  no orders")
    for blocker in doc["blockers"]:
        rt.out(f"  BLOCKER {blocker}")
    if doc["requires_approval"] and doc["mode"] == "live":
        rt.out(f"  needs approval: run `rebalancer approve {doc['run_id']}` in a terminal")
    if existing_live:
        rt.out(
            f"  today's live run is {existing_live}; place its remaining lines, a new live plan is not allowed today"
        )
    rt.out(f"report: {report_path.relative_to(rt.project)}")


# ---------------------------------------------------------------- commands


def cmd_snapshot(rt: Runtime, args: argparse.Namespace) -> int:
    raw_dir = Path(args.raw_dir) if args.raw_dir else raw_dir_for(rt.state, rt.now)
    snapshot, sources = build_snapshot(raw_dir, rt.now)
    sha = save_snapshot(snapshot, rt.state / "snapshot.json", sources)
    rt.out(
        f"snapshot written: state/snapshot.json ({len(snapshot.holdings)} positions, "
        f"{len(snapshot.quotes)} quotes, {len(snapshot.lots)} lots) sha256={sha[:12]}"
    )
    return 0


def cmd_plan(rt: Runtime, args: argparse.Namespace) -> int:
    config = load_config(rt.project / "config")
    snapshot, sha = load_snapshot(Path(args.snapshot) if args.snapshot else rt.state / "snapshot.json")
    with Ledger(rt.state / "ledger.sqlite3") as ledger:
        existing = ledger.live_run_for(rt.today)
    doc, report_path = make_plan(rt, config, snapshot, sha, rt.state, annual_reset=args.annual_reset)
    _summary(rt, doc, report_path, existing.run_id if existing and rt.live_requested else None)
    return 0


def cmd_report(rt: Runtime, args: argparse.Namespace) -> int:
    path = rt.state / "reports" / f"{args.run_id}.md" if args.run_id else None
    if path is None:
        reports = sorted((rt.state / "reports").glob("*.md"), key=lambda p: p.stat().st_mtime)
        if not reports:
            raise CliError("no reports yet; run `rebalancer plan`")
        path = reports[-1]
    rt.out(path.read_text())
    return 0


def cmd_status(rt: Runtime, args: argparse.Namespace) -> int:
    rt.out(f"project: {rt.project}")
    rt.out(
        f"TRADING_ENABLED={rt.environ.get('TRADING_ENABLED', '(unset)')}  "
        f"DRY_RUN={rt.environ.get('DRY_RUN', '(unset: dry run)')}  "
        f"kill switch: {'ON' if (rt.project / 'KILL').exists() else 'off'}"
    )
    problems = verify_armed(rt.project, rt.state)
    rt.out("armed: yes" if not problems else "armed: NO - " + "; ".join(problems[:5]))
    try:
        config = load_config(rt.project / "config")
        dep = config.strategy.deployment
        rt.out(
            f"targets: {', '.join(f'{s} {w}' for s, w in config.strategy.targets.items())}  "
            f"band ±{config.strategy.band_width}  lot method {config.strategy.lot_method}"
        )
        rt.out(
            f"limits: order ${config.limits.min_order}-${config.limits.max_order}, "
            f"daily ${config.limits.max_daily_notional}, cash reserve ${config.strategy.cash_reserve}"
        )
        rt.out(
            "phased deployment: "
            + (f"${dep.total_usd} in {dep.tranches} tranches from {dep.start_date}" if dep.enabled else "off")
        )
    except ConfigError as exc:
        rt.out(f"CONFIG ERROR: {exc}")
    with Ledger(rt.state / "ledger.sqlite3") as ledger:
        live = ledger.live_run_for(rt.today)
        if live is None:
            rt.out(f"no live run today ({rt.today})")
        else:
            rt.out(f"live run today: {live.run_id} ({live.action})")
            for line in ledger.lines(live.run_id):
                rt.out(f"  {line.line_id}: {line.side} {line.symbol} ${line.notional_usd:,.2f} -> {line.status}")
    return 0


def cmd_pending(rt: Runtime, args: argparse.Namespace) -> int:
    """Today's live run's PENDING lines as JSON: exactly what the agent may place.

    Prints [] when there is nothing to place, with a `#` line saying why when a
    live run exists: a dry-run session, the kill switch, an expired plan, or a
    missing approval. The guard checks all of this again for every order.
    """
    limits = load_config(rt.project / "config").limits
    with Ledger(rt.state / "ledger.sqlite3") as ledger:
        live = ledger.live_run_for(rt.today)
        if live is None:
            rt.out("[]")
            return 0
        reason = None
        if not rt.live_requested:
            reason = "dry-run session: orders need TRADING_ENABLED=true and DRY_RUN=false"
        elif (rt.project / "KILL").exists():
            reason = "the kill switch is on (KILL file)"
        elif rt.now - live.created_at > limits.plan_max_age:
            reason = f"{live.run_id} is older than {limits.plan_max_age} and can't be executed any more"
        elif live.requires_approval and not ledger.has_approval(live.run_id, live.plan_sha256, rt.today):
            reason = f"{live.run_id} needs approval first: `rebalancer approve {live.run_id}` in a terminal"
        if reason is not None:
            rt.out("[]")
            rt.out(f"# {reason}")
            return 0
        lines = [
            {"line_id": x.line_id, "symbol": x.symbol, "side": x.side, "notional_usd": f"{x.notional_usd:.2f}"}
            for x in ledger.lines(live.run_id)
            if x.status == "PENDING"
        ]
    rt.out(json.dumps(lines, indent=1))
    return 0


def cmd_reconcile(rt: Runtime, args: argparse.Namespace) -> int:
    config = load_config(rt.project / "config")
    orders = broker_orders(Path(args.raw_dir) if args.raw_dir else raw_dir_for(rt.state, rt.now))
    with Ledger(rt.state / "ledger.sqlite3") as ledger:
        live = ledger.live_run_for(rt.today)
        if live is None:
            rt.out("no live run today; nothing to reconcile")
            return 0
        changes = ledger.reconcile(live.run_id, orders, rt.now, tolerance=config.limits.match_tolerance)
        lines = ledger.lines(live.run_id)
        if "annual_reset" in live.action and any(x.status in ("FILLED", "PARTIAL") for x in lines):
            ledger.record_reset(rt.today.year, "executed", live.run_id, rt.now)
    for line in lines:
        mark = " (updated)" if line.line_id in changes else ""
        rt.out(f"{line.line_id}: {line.side} {line.symbol} ${line.notional_usd:,.2f} -> {line.status}{mark}")
    return 0


def cmd_approve(rt: Runtime, args: argparse.Namespace) -> int:
    _require_tty(rt, "rebalancer approve")
    config = load_config(rt.project / "config")
    reference = (args.reference or "").strip() or None
    if config.limits.approval_for_all_trades and reference is None:
        raise CliError("compliance mode: give the pre-clearance reference with --reference")
    with Ledger(rt.state / "ledger.sqlite3") as ledger:
        run = ledger.run(args.run_id)
        if run is None or run.mode != "live" or run.run_date != rt.today:
            raise CliError(f"{args.run_id} is not today's live run")
        doc = json.loads((rt.project / run.plan_path).read_text())
        if not verify_plan_dict(doc) or doc["plan_sha256"] != run.plan_sha256:
            raise CliError("plan file does not match the ledger; refusing to approve")
        rt.out(f"Plan {run.run_id} ({doc['action']}):")
        for order in doc["orders"]:
            rt.out(f"  {order['side']} {order['symbol']} ${order['notional_usd']:,.2f}")
        tax = doc["tax_estimate"]
        rt.out(f"Estimated realized gains: short-term ${tax['st_gain_usd']:,.2f}, long-term ${tax['lt_gain_usd']:,.2f}")
        typed = rt.input_fn(f"Type {run.plan_sha256[:8]} to approve (anything else cancels): ").strip()
        if typed != run.plan_sha256[:8]:
            rt.out("not approved")
            return 1
        ledger.record_approval(run.run_id, run.plan_sha256, rt.today, rt.now, reference)
    rt.out("approved for today" + (f" (reference {reference})" if reference else ""))
    return 0


def cmd_arm(rt: Runtime, args: argparse.Namespace) -> int:
    _require_tty(rt, "rebalancer arm")
    manifest = compute_manifest(rt.project)
    problems = verify_armed(rt.project, rt.state)
    rt.out(f"{len(manifest)} protected files. Differences from the last arming:")
    for problem in problems:
        rt.out(f"  {problem}")
    if rt.input_fn("Type ARM to trust these files for trading: ").strip() != "ARM":
        rt.out("not armed")
        return 1
    write_armed(rt.project, rt.state, rt.now)
    rt.out("armed")
    return 0


def cmd_rearm(rt: Runtime, args: argparse.Namespace) -> int:
    _require_tty(rt, "rebalancer rearm")
    if rt.input_fn(f"Confirm there is no broker order for {args.line_id} (type YES): ").strip() != "YES":
        rt.out("not re-armed")
        return 1
    with Ledger(rt.state / "ledger.sqlite3") as ledger:
        ledger.rearm_line(args.run_id, args.line_id, rt.now)
    rt.out(f"{args.line_id} is pending again")
    return 0


def cmd_dismiss_reset(rt: Runtime, args: argparse.Namespace) -> int:
    _require_tty(rt, "rebalancer dismiss-reset")
    with Ledger(rt.state / "ledger.sqlite3") as ledger:
        ledger.record_reset(args.year, "dismissed", None, rt.now)
    rt.out(f"annual reset for {args.year} dismissed")
    return 0


def cmd_paper_run(
    rt: Runtime,
    args: argparse.Namespace,
    broker_factory: Callable | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    from .broker.alpaca_paper import AlpacaPaperBroker

    config = load_config(rt.project / "config")
    state = rt.state / "paper"
    factory = broker_factory or (
        lambda: AlpacaPaperBroker(
            rt.environ.get("APCA_API_KEY_ID", ""), rt.environ.get("APCA_API_SECRET_KEY", ""), config.strategy.targets
        )
    )
    broker = factory()
    snapshot = broker.snapshot(rt.now)
    sha = save_snapshot(snapshot, state / "snapshot.json")
    doc, report_path = make_plan(rt, config, snapshot, sha, state, annual_reset=False)
    _summary(rt, doc, report_path, None)
    if doc["mode"] != "live":
        return 0
    from .models import Order, Side
    from .money import to_decimal

    for line in doc["orders"]:
        request = OrderRequest(line["symbol"], line["side"], to_decimal(line["notional_usd"]), None)
        client_id = f"{doc['run_id']}-{line['line_id']}"
        result = guard_and_claim(
            request, rt.project, state, rt.environ, rt.now, tool_use_id=client_id, review_found=True
        )
        if result.denied:
            rt.out(f"guard refused {line['line_id']}: {result.reason}")
            return 1
        order = Order(line["line_id"], line["symbol"], Side(line["side"]), request.amount, line["reason"])
        broker_id = broker.submit(order, client_id)
        with Ledger(state / "ledger.sqlite3") as ledger:
            ledger.mark_submitted(client_id, broker_id, rt.now)
        rt.out(f"submitted {line['line_id']} as {broker_id}")
    sleep(5)
    with Ledger(state / "ledger.sqlite3") as ledger:
        live = ledger.live_run_for(rt.today)
        assert live is not None
        changes = ledger.reconcile(
            live.run_id, broker.orders(), datetime.now(UTC), tolerance=config.limits.match_tolerance
        )
    rt.out(f"reconciled: {changes}")
    return 0


# ---------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rebalancer", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show switches, arming and today's run")
    p = sub.add_parser("snapshot", help="build state/snapshot.json from captured MCP responses")
    p.add_argument("--raw-dir")
    p = sub.add_parser("plan", help="build today's plan (live only if DRY_RUN=false and TRADING_ENABLED=true)")
    p.add_argument("--snapshot")
    p.add_argument("--annual-reset", action="store_true")
    p = sub.add_parser("report", help="print a report (latest by default)")
    p.add_argument("run_id", nargs="?")
    sub.add_parser("pending", help="today's live lines still to place, as JSON (what the agent may place)")
    p = sub.add_parser("reconcile", help="update today's lines from captured get_equity_orders")
    p.add_argument("--raw-dir")
    p = sub.add_parser("approve", help="approve today's live plan (terminal only)")
    p.add_argument("run_id")
    p.add_argument("--reference", help="pre-clearance reference; required when compliance needs approval")
    sub.add_parser("arm", help="trust the current code/config hashes for trading (terminal only)")
    p = sub.add_parser("rearm", help="make a failed line pending again (terminal only)")
    p.add_argument("run_id")
    p.add_argument("line_id")
    p = sub.add_parser("dismiss-reset", help="skip this year's annual reset (terminal only)")
    p.add_argument("year", type=int)
    sub.add_parser("paper-run", help="snapshot, plan and execute against Alpaca paper trading")
    return parser


COMMANDS = {
    "status": cmd_status,
    "snapshot": cmd_snapshot,
    "plan": cmd_plan,
    "report": cmd_report,
    "pending": cmd_pending,
    "reconcile": cmd_reconcile,
    "approve": cmd_approve,
    "arm": cmd_arm,
    "rearm": cmd_rearm,
    "dismiss-reset": cmd_dismiss_reset,
    "paper-run": cmd_paper_run,
}


def main(argv: Sequence[str] | None = None, runtime: Runtime | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rt = runtime or Runtime(
            project=find_project(Path.cwd(), os.environ),
            now=datetime.now(UTC),
            environ=os.environ,
            input_fn=input,
            is_tty=sys.stdin.isatty() and sys.stdout.isatty(),
            out=print,
        )
        rt.state.mkdir(parents=True, exist_ok=True)
        return COMMANDS[args.command](rt, args)
    except (CliError, ConfigError, SnapshotError, LedgerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

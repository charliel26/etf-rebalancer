"""Order guard shared by the Claude Code hook and the Alpaca paper executor
(STRATEGY.md §11). Deny-only: it never approves a call, it only refuses one.

`check_order` is a pure function of an order and a context; `pre_tool_use`
gathers that context from config, ledger, plan file and captured responses,
and claims the plan line atomically when everything passes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import lots
from .config import ConfigError, Limits, load_config
from .integrity import verify_armed
from .ledger import Ledger, RunRow
from .money import to_decimal
from .nyse import SessionBounds, execution_window, session_bounds
from .plan import verify_plan_dict

NEW_YORK = ZoneInfo("America/New_York")
PREFIX = "mcp__robinhood-trading__"
READ_TOOLS = frozenset(
    {
        "get_portfolio",
        "get_equity_positions",
        "get_equity_quotes",
        "get_equity_historicals",
        "get_equity_tax_lots",
        "get_equity_tradability",
        "get_equity_orders",
    }
)
REVIEW_TOOL = "review_equity_order"
ORDER_TOOL = "place_equity_order"
CANCEL_TOOL = "cancel_equity_order"
ALLOWED_TOOLS = READ_TOOLS | {REVIEW_TOOL, ORDER_TOOL, CANCEL_TOOL}

# Accepted spellings of each order parameter. Anything else is refused, so a
# new or unexpected parameter (limit price, stop, quantity, ...) cannot slip
# through. Update after dumping the real place_equity_order schema.
FIELDS: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "ticker"),
    "side": ("side",),
    "amount": ("amount", "dollar_amount", "dollars", "notional", "notional_usd", "amount_usd"),
    "order_type": ("type", "order_type"),
    "time_in_force": ("time_in_force", "tif"),
    "extended_hours": ("extended_hours", "extendedHours", "allow_extended_hours"),
    "trading_session": ("trading_session", "market_hours"),
    "account": ("account_number", "account_id", "account"),
    "ref_id": ("ref_id", "client_order_id", "idempotency_key"),
}
ALIAS_TO_FIELD = {alias: name for name, aliases in FIELDS.items() for alias in aliases}


class GuardViolation(ValueError):
    pass


@dataclass(frozen=True)
class GuardResult:
    denied: bool
    reason: str


ALLOW = GuardResult(False, "")


def deny(reason: str) -> GuardResult:
    return GuardResult(True, reason)


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str
    amount: Decimal
    account: str | None


def parse_order(tool_input: Any) -> OrderRequest:
    """Strictly parse place/review order arguments; raise GuardViolation if anything is off."""
    if not isinstance(tool_input, Mapping):
        raise GuardViolation("order arguments are not an object")
    values: dict[str, Any] = {}
    for key, value in tool_input.items():
        name = ALIAS_TO_FIELD.get(key)
        if name is None:
            raise GuardViolation(f"unrecognized order parameter {key!r}")
        if name in values:
            raise GuardViolation(f"parameter {name!r} given twice")
        values[name] = value

    symbol = values.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise GuardViolation("missing symbol")
    side = values.get("side")
    if not isinstance(side, str) or side.strip().lower() not in ("buy", "sell"):
        raise GuardViolation(f"side must be buy or sell, got {side!r}")
    if "amount" not in values:
        raise GuardViolation("order must be a dollar amount (no dollar amount given)")
    try:
        amount = to_decimal(values["amount"])
    except (TypeError, ValueError) as exc:
        raise GuardViolation(f"bad dollar amount: {exc}") from exc
    if amount <= 0:
        raise GuardViolation("dollar amount must be positive")

    order_type = values.get("order_type")
    if order_type is not None and str(order_type).lower() != "market":
        raise GuardViolation(f"only market orders are allowed, got {order_type!r}")
    tif = values.get("time_in_force")
    if tif is not None and str(tif).lower() not in ("gfd", "day"):
        raise GuardViolation(f"time in force must be good-for-day, got {tif!r}")
    extended = values.get("extended_hours")
    if extended is not None and extended is not False and str(extended).lower() != "false":
        raise GuardViolation("extended-hours trading is not allowed")
    session = values.get("trading_session")
    if session is not None and str(session).lower() not in ("regular", "regular_hours"):
        raise GuardViolation(f"only regular-hours sessions are allowed, got {session!r}")
    account = values.get("account")
    return OrderRequest(
        symbol=symbol.strip().upper(),
        side=side.strip().lower(),
        amount=amount,
        account=None if account is None else str(account),
    )


@dataclass(frozen=True)
class OrderContext:
    now: datetime
    today: date
    env: Mapping[str, str]
    limits: Limits
    targets: frozenset[str]
    kill_file: bool
    manifest_problems: tuple[str, ...]
    window: tuple[datetime, datetime] | None
    live_run: RunRow | None
    plan: Mapping[str, Any] | None
    plan_error: str | None
    approved: bool
    wash_sale_blocked: frozenset[str]
    review_found: bool


def check_order(order: OrderRequest, ctx: OrderContext) -> str | None:
    """First reason to refuse the order, or None. Pure."""
    limits = ctx.limits
    if ctx.manifest_problems:
        return "integrity check failed: " + "; ".join(ctx.manifest_problems)
    if ctx.kill_file:
        return "kill switch is on (KILL file present)"
    if ctx.env.get("TRADING_ENABLED") != "true":
        return "TRADING_ENABLED is not 'true'"
    if ctx.env.get("DRY_RUN") != "false":
        return "DRY_RUN is not 'false' (dry run is the default)"
    if ctx.window is None:
        return f"{ctx.today} is not an NYSE trading session"
    start, end = ctx.window
    if not start <= ctx.now <= end:
        return f"outside the execution window {start.astimezone(NEW_YORK):%H:%M}-{end.astimezone(NEW_YORK):%H:%M} ET"
    if ctx.live_run is None:
        return "no live run is registered for today"
    if ctx.plan_error is not None or ctx.plan is None:
        return ctx.plan_error or "plan file missing"
    plan = ctx.plan
    if not verify_plan_dict(plan):
        return "plan hash does not match its content (plan file was modified)"
    if plan.get("plan_sha256") != ctx.live_run.plan_sha256 or plan.get("run_id") != ctx.live_run.run_id:
        return "plan does not match the ledger's live run for today"
    if plan.get("mode") != "live":
        return "plan is not a live plan"
    if plan.get("date") != ctx.today.isoformat():
        return "plan is not for today"
    if plan.get("blockers"):
        return f"plan has blockers: {plan['blockers']}"
    created = datetime.fromisoformat(plan["inputs"]["created_at"])
    if ctx.now - created > limits.plan_max_age:
        return f"plan is older than {limits.plan_max_age}"
    if plan.get("requires_approval") and not ctx.approved:
        return "plan requires approval: run `rebalancer approve <run_id>` in a terminal"
    if order.symbol not in ctx.targets:
        return f"{order.symbol} is not on the whitelist"
    if not limits.min_order <= order.amount <= limits.max_order:
        return f"amount {order.amount} outside [{limits.min_order}, {limits.max_order}]"
    if limits.agentic_account_id and order.account is not None and order.account != limits.agentic_account_id:
        return "order names an account other than the agentic account"
    if order.side == "buy" and order.symbol in ctx.wash_sale_blocked:
        return f"wash-sale guard: {order.symbol} was sold at a loss in the last 30 days"
    if limits.require_review and not ctx.review_found:
        return "no matching review_equity_order in the last few minutes"
    return None


# ---------------------------------------------------------------- I/O side


def find_review(raw_dir: Path, order: OrderRequest, now: datetime, tolerance: Decimal, max_age: timedelta) -> bool:
    """A successful review_equity_order with the same symbol, side and amount, recently captured."""
    if not raw_dir.is_dir():
        return False
    for path in sorted(raw_dir.glob(f"*_{REVIEW_TOOL}_*.json")):
        try:
            record = json.loads(path.read_text())
            reviewed = parse_order(record["tool_input"])
            captured = datetime.fromisoformat(record["captured_at"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if record.get("event") != "PostToolUse" or not timedelta(0) <= now - captured <= max_age:
            continue
        if (reviewed.symbol, reviewed.side) == (order.symbol, order.side) and abs(
            reviewed.amount - order.amount
        ) <= tolerance:
            return True
    return False


def _load_plan(run: RunRow | None, project_dir: Path) -> tuple[Mapping[str, Any] | None, str | None]:
    if run is None:
        return None, None
    path = Path(run.plan_path)
    if not path.is_absolute():
        path = project_dir / path
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return None, f"cannot read plan file {path}: {exc}"
    if not isinstance(document, dict):
        return None, "plan file is not a JSON object"
    return document, None


def pre_tool_use(
    payload: Mapping[str, Any],
    project_dir: Path,
    environ: Mapping[str, str],
    now: datetime | None = None,
    bounds: SessionBounds = session_bounds,
) -> GuardResult:
    """Decide on one Robinhood MCP tool call (STRATEGY.md §11)."""
    now = now or datetime.now(UTC)
    tool_name = str(payload.get("tool_name", ""))
    if not tool_name.startswith(PREFIX):
        return ALLOW  # not ours; the hook is only registered for this server anyway
    short = tool_name[len(PREFIX):]
    if short not in ALLOWED_TOOLS:
        return deny(f"{short} is not on the robinhood-trading allowlist")
    if short != ORDER_TOOL:
        return ALLOW

    try:
        order = parse_order(payload.get("tool_input"))
    except GuardViolation as exc:
        return deny(str(exc))
    try:
        config = load_config(project_dir / "config")
    except ConfigError as exc:
        return deny(f"config error: {exc}")

    limits = config.limits
    state_dir = project_dir / "state"
    today = now.astimezone(NEW_YORK).date()
    ledger_path = state_dir / "ledger.sqlite3"
    if not ledger_path.exists():
        return deny("no ledger: generate a live plan first")
    with Ledger(ledger_path) as ledger:
        live_run = ledger.live_run_for(today)
        plan, plan_error = _load_plan(live_run, project_dir)
        approved = bool(live_run and plan and ledger.has_approval(live_run.run_id, live_run.plan_sha256, today))
        trades = ledger.trades_since(today - timedelta(days=lots.WASH_SALE_DAYS + 1))
        groups = config.strategy.identical_groups
        ctx = OrderContext(
            now=now,
            today=today,
            env=environ,
            limits=limits,
            targets=frozenset(config.strategy.targets),
            kill_file=(project_dir / "KILL").exists(),
            manifest_problems=tuple(verify_armed(project_dir, state_dir)),
            window=execution_window(today, limits, bounds),
            live_run=live_run,
            plan=plan,
            plan_error=plan_error,
            approved=approved,
            wash_sale_blocked=frozenset(
                s for s in config.strategy.targets if lots.buy_blocked(s, today, trades, groups)
            ),
            review_found=find_review(
                state_dir / "raw" / today.isoformat(), order, now, limits.match_tolerance, limits.review_max_age
            ),
        )
        reason = check_order(order, ctx)
        if reason is not None:
            ledger.log("order_denied", {"reason": reason, "order": order.__dict__}, now)
            return deny(reason)
        assert live_run is not None
        claim = ledger.claim_line(
            live_run.run_id,
            order.symbol,
            order.side,
            order.amount,
            tolerance=limits.match_tolerance,
            daily_cap=limits.max_daily_notional,
            run_date=today,
            now=now,
            tool_use_id=str(payload.get("tool_use_id") or "") or None,
        )
        if not claim.ok:
            ledger.log("order_denied", {"reason": claim.reason, "order": order.__dict__}, now, live_run.run_id)
            return deny(claim.reason)
    return ALLOW


def hook_output(result: GuardResult) -> tuple[int, str]:
    """Exit code and stdout for a PreToolUse command hook. Deny = exit 2 + JSON."""
    if not result.denied:
        return 0, ""
    body = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"rebalancer guard: {result.reason}",
        }
    }
    return 2, json.dumps(body)


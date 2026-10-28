"""The committed Claude Code settings must match what the guard expects."""

import json
import re

import pytest

from rebalancer.guard import ALLOWED_TOOLS, ORDER_TOOL, PREFIX, READ_TOOLS, REVIEW_TOOL
from tests.conftest import REPO_ROOT

SETTINGS = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
PERMISSIONS = SETTINGS["permissions"]


def robinhood_rules(kind: str) -> set[str]:
    return {rule[len(PREFIX) :] for rule in PERMISSIONS[kind] if rule.startswith(PREFIX)}


def denied(tool: str) -> bool:
    """Whether a robinhood-trading deny rule (with * globs) matches the tool."""
    patterns = [re.escape(rule).replace(r"\*", ".*") for rule in robinhood_rules("deny")]
    return any(re.fullmatch(p, tool) for p in patterns)


def test_modes_that_skip_checks_are_off():
    assert SETTINGS["disableAllHooks"] is False
    assert PERMISSIONS["disableBypassPermissionsMode"] == "disable"
    assert PERMISSIONS["disableAutoMode"] == "disable"


def test_only_read_and_review_tools_run_without_asking():
    assert robinhood_rules("allow") == READ_TOOLS | {REVIEW_TOOL}


def test_orders_and_cancels_always_ask():
    assert robinhood_rules("ask") == {ORDER_TOOL, "cancel_equity_order"}
    assert robinhood_rules("allow") | robinhood_rules("ask") == ALLOWED_TOOLS


@pytest.mark.parametrize(
    "tool",
    [
        "get_equity_news",
        "place_option_order",
        "get_crypto_quote",
        "add_to_watchlist",
        "run_scanner",
        "create_price_alert",
        "place_stop_loss_order",
        "place_limit_order",
        "place_trailing_stop_order",
        "place_bracket_order",
        "get_margin_status",
        "transfer_funds",
        "deposit_cash",
        "withdraw_cash",
        "get_card_transactions",
        "search_instruments",
    ],
)
def test_unneeded_robinhood_tools_are_denied(tool):
    assert denied(tool)


def test_no_needed_tool_is_denied():
    assert not [tool for tool in ALLOWED_TOOLS if denied(tool)]


def test_mcp_rules_have_no_parentheses():
    # Claude Code skips any mcp__ rule with parentheses when it loads settings.
    rules = PERMISSIONS["allow"] + PERMISSIONS["ask"] + PERMISSIONS["deny"]
    assert not [r for r in rules if r.startswith("mcp__") and "(" in r]


@pytest.mark.parametrize("path", ["/config/**", "/state/**", "/hooks/**", "/src/**", "/.claude/**", "/.venv/**"])
def test_trading_files_are_locked_against_claude_edits(path):
    assert f"Edit({path})" in PERMISSIONS["deny"] and f"Write({path})" in PERMISSIONS["deny"]


@pytest.mark.parametrize("command", ["approve", "arm", "rearm", "dismiss-reset"])
def test_authority_commands_are_denied_to_the_agent(command):
    assert f"Bash(*rebalancer {command}*)" in PERMISSIONS["deny"]
    assert not any(command in rule for rule in PERMISSIONS["allow"])


def test_guard_and_capture_hooks_cover_every_robinhood_tool():
    for event, script in (
        ("PreToolUse", "guard_order.py"),
        ("PostToolUse", "capture_mcp.py"),
        ("PostToolUseFailure", "capture_mcp.py"),
    ):
        [entry] = SETTINGS["hooks"][event]
        assert re.fullmatch(entry["matcher"], PREFIX + ORDER_TOOL)
        assert re.fullmatch(entry["matcher"], PREFIX + "some_future_tool")
        [hook] = entry["hooks"]
        assert hook["command"] == "${CLAUDE_PROJECT_DIR}/.venv/bin/python"
        assert hook["args"] == [f"${{CLAUDE_PROJECT_DIR}}/hooks/{script}"]
    [guard] = SETTINGS["hooks"]["PreToolUse"][0]["hooks"]
    assert guard["onFailure"] == "block"


def test_agent_procedure_places_only_pending_lines():
    text = (REPO_ROOT / ".claude" / "commands" / "rebalance.md").read_text()
    assert "rebalancer pending" in text and "review_equity_order" in text
    for allowed in ("status", "snapshot *", "plan *", "pending", "reconcile *", "report *"):
        assert f"Bash(uv run rebalancer {allowed})" in PERMISSIONS["allow"]

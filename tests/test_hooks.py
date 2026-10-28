"""The hook scripts, run as Claude Code runs them: a subprocess with JSON on stdin."""

import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta

import pytest

from rebalancer.capture import capture
from rebalancer.integrity import compute_manifest, verify_armed, write_armed
from rebalancer.ledger import Ledger
from rebalancer.nyse import execution_window, session_bounds
from tests.builders import (
    NOW,
    ORDER_TOOL,
    TODAY,
    arm,
    make_project,
    order_payload,
    record_live_plan,
)
from tests.conftest import REPO_ROOT, make_config


def run_hook(script, project, payload, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("TRADING_ENABLED", "DRY_RUN")}
    env.update({"CLAUDE_PROJECT_DIR": str(project), **(env_extra or {})})
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "hooks" / script)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


@pytest.fixture
def project(tmp_path):
    project = make_project(tmp_path)
    record_live_plan(project)
    arm(project)
    return project


class TestGuardHookProcess:
    def test_read_tool_exits_zero_silently(self, project):
        result = run_hook(
            "guard_order.py", project, {"tool_name": "mcp__robinhood-trading__get_portfolio", "tool_input": {}}
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_unlisted_tool_is_blocked_with_exit_2_and_json(self, project):
        result = run_hook(
            "guard_order.py", project, {"tool_name": "mcp__robinhood-trading__place_option_order", "tool_input": {}}
        )
        assert result.returncode == 2
        body = json.loads(result.stdout)["hookSpecificOutput"]
        assert body["permissionDecision"] == "deny"
        assert "allowlist" in body["permissionDecisionReason"]
        assert "allowlist" in result.stderr

    def test_order_blocked_in_dry_run(self, project):
        result = run_hook("guard_order.py", project, order_payload(), {"TRADING_ENABLED": "true"})
        assert result.returncode == 2
        assert "DRY_RUN" in result.stderr

    def test_malformed_input_fails_closed(self, project):
        result = run_hook("guard_order.py", project, "this is not json")
        assert result.returncode == 2
        assert "failing closed" in result.stderr

    def test_missing_config_fails_closed(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        result = run_hook("guard_order.py", empty, order_payload(), {"TRADING_ENABLED": "true", "DRY_RUN": "false"})
        assert result.returncode == 2
        assert "config error" in result.stderr


class TestCaptureHookProcess:
    def test_writes_raw_response(self, project):
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__robinhood-trading__get_portfolio",
            "tool_input": {},
            "tool_response": {"content": [{"type": "text", "text": '{"cash": "100.00"}'}]},
            "tool_use_id": "toolu_abc",
        }
        result = run_hook("capture_mcp.py", project, payload)
        assert result.returncode == 0
        files = list((project / "state" / "raw").rglob("*_get_portfolio_*.json"))
        assert len(files) == 1
        record = json.loads(files[0].read_text())
        assert record["tool_response"] == payload["tool_response"] and record["event"] == "PostToolUse"

    def test_never_blocks_and_logs_errors(self, project):
        result = run_hook("capture_mcp.py", project, "garbage")
        assert result.returncode == 0
        assert "capture_mcp" in (project / "state" / "hook_errors.log").read_text()


class TestCaptureInProcess:
    def test_order_submission_updates_ledger(self, project):
        from rebalancer.guard import pre_tool_use
        from tests.builders import LIVE_ENV, capture_review, session

        capture_review(project)
        assert not pre_tool_use(order_payload(tool_use_id="toolu_X"), project, LIVE_ENV, now=NOW, bounds=session).denied
        response = {"content": [{"type": "text", "text": json.dumps({"order": {"id": "rh-123", "state": "queued"}})}]}
        capture(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": ORDER_TOOL,
                "tool_use_id": "toolu_X",
                "tool_input": {},
                "tool_response": response,
            },
            project / "state",
            NOW,
        )
        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            line = next(x for x in ledger.lines(ledger.live_run_for(TODAY).run_id) if x.symbol == "VTI")
        assert (line.status, line.broker_order_id) == ("SUBMITTED", "rh-123")

    def test_order_failure_marks_line(self, project):
        from rebalancer.guard import pre_tool_use
        from tests.builders import LIVE_ENV, capture_review, session

        capture_review(project)
        pre_tool_use(order_payload(tool_use_id="toolu_Y"), project, LIVE_ENV, now=NOW, bounds=session)
        capture(
            {
                "hook_event_name": "PostToolUseFailure",
                "tool_name": ORDER_TOOL,
                "tool_use_id": "toolu_Y",
                "tool_input": {},
                "error": "insufficient buying power",
            },
            project / "state",
            NOW,
        )
        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            line = next(x for x in ledger.lines(ledger.live_run_for(TODAY).run_id) if x.symbol == "VTI")
        assert line.status == "SUBMIT_FAILED"

    def test_other_servers_ignored(self, tmp_path):
        assert capture({"tool_name": "mcp__other__x"}, tmp_path, NOW) is None


class TestIntegrity:
    def test_manifest_detects_changes(self, tmp_path):
        project = make_project(tmp_path)
        write_armed(project, project / "state", NOW)
        assert verify_armed(project, project / "state") == []
        (project / "hooks" / "guard_order.py").write_text("print('pwned')\n")
        (project / "hooks" / "new.py").write_text("")
        (project / "hooks" / "capture_mcp.py").unlink()
        problems = verify_armed(project, project / "state")
        assert problems == [
            "changed since arming: hooks/guard_order.py",
            "new since arming: hooks/new.py",
            "missing since arming: hooks/capture_mcp.py",
        ]

    def test_unreadable_manifest(self, tmp_path):
        (tmp_path / "armed.json").write_text("{}")
        assert "unreadable" in verify_armed(tmp_path, tmp_path)[0]

    def test_repo_manifest_covers_guard_code(self):
        manifest = compute_manifest(REPO_ROOT)
        for path in (
            "config/limits.yaml",
            "hooks/guard_order.py",
            "src/rebalancer/guard.py",
            "src/rebalancer/plan.py",
            ".claude/settings.json",
            ".claude/commands/rebalance.md",
            "uv.lock",
        ):
            assert path in manifest


class TestExecutionWindow:
    def test_fake_session(self):
        limits = make_config().limits
        start, end = execution_window(TODAY, limits, lambda d: (NOW.replace(hour=14, minute=30), NOW.replace(hour=21)))
        assert (start, end) == (datetime(2026, 11, 2, 14, 35, tzinfo=UTC), datetime(2026, 11, 2, 20, 55, tzinfo=UTC))

    def test_half_day_ends_before_early_close(self):
        limits = make_config().limits
        _, end = execution_window(TODAY, limits, lambda d: (NOW.replace(hour=14, minute=30), NOW.replace(hour=18)))
        assert end == datetime(2026, 11, 2, 17, 55, tzinfo=UTC)  # 12:55 ET

    def test_closed_and_empty_windows(self):
        limits = make_config().limits
        assert execution_window(TODAY, limits, lambda d: None) is None
        assert (
            execution_window(
                TODAY, limits, lambda d: (NOW.replace(hour=14, minute=30), NOW.replace(hour=14, minute=36))
            )
            is None
        )

    def test_real_nyse_calendar(self):
        # Thanksgiving 2026 is closed; the day after closes at 13:00 ET (18:00 UTC).
        assert session_bounds(date(2026, 11, 26)) is None
        _, close = session_bounds(date(2026, 11, 27))
        assert close == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
        open_, close = session_bounds(TODAY)
        assert (open_, close) == (datetime(2026, 11, 2, 14, 30, tzinfo=UTC), datetime(2026, 11, 2, 21, 0, tzinfo=UTC))
        assert session_bounds(TODAY + timedelta(days=5)) is None  # Saturday

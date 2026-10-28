"""Guard tests against a temp project with a real ledger, plan and armed manifest."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from rebalancer.guard import (
    ALLOW,
    GuardViolation,
    deny,
    find_review,
    hook_output,
    parse_order,
    pre_tool_use,
)
from rebalancer.ledger import Ledger
from rebalancer.models import Side, TradeRecord
from tests.builders import (
    LIVE_ENV,
    NOW,
    TODAY,
    arm,
    capture_review,
    make_project,
    make_snapshot,
    on_target_shares,
    order_payload,
    record_live_plan,
    session,
)
from tests.conftest import D


class TestParseOrder:
    def test_minimal_and_aliases(self):
        order = parse_order({"ticker": " vti ", "side": "BUY", "dollar_amount": 12.5})
        assert (order.symbol, order.side, order.amount, order.account) == ("VTI", "buy", D("12.5"), None)

    def test_safe_optional_fields(self):
        order = parse_order(
            {
                "symbol": "VTI",
                "side": "sell",
                "amount": "10",
                "type": "market",
                "time_in_force": "gfd",
                "extended_hours": False,
                "trading_session": "regular_hours",
                "account_number": "A1",
                "ref_id": "x",
            }
        )
        assert order.account == "A1"
        assert parse_order({"symbol": "VTI", "side": "buy", "amount": "1", "extended_hours": "false"}).amount == 1

    @pytest.mark.parametrize(
        "tool_input",
        [
            "VTI buy 10",
            {"symbol": "VTI", "side": "buy", "amount": "10", "limit_price": "5"},
            {"symbol": "VTI", "side": "buy", "amount": "10", "quantity": "1"},
            {"symbol": "VTI", "ticker": "VTI", "side": "buy", "amount": "10"},
            {"side": "buy", "amount": "10"},
            {"symbol": "", "side": "buy", "amount": "10"},
            {"symbol": "VTI", "side": "short", "amount": "10"},
            {"symbol": "VTI", "side": "buy"},
            {"symbol": "VTI", "side": "buy", "amount": "ten"},
            {"symbol": "VTI", "side": "buy", "amount": "-5"},
            {"symbol": "VTI", "side": "buy", "amount": "10", "type": "limit"},
            {"symbol": "VTI", "side": "buy", "amount": "10", "time_in_force": "gtc"},
            {"symbol": "VTI", "side": "buy", "amount": "10", "extended_hours": True},
            {"symbol": "VTI", "side": "buy", "amount": "10", "extended_hours": "true"},
            {"symbol": "VTI", "side": "buy", "amount": "10", "trading_session": "extended"},
        ],
    )
    def test_refuses(self, tool_input):
        with pytest.raises(GuardViolation):
            parse_order(tool_input)


@pytest.fixture
def project(tmp_path):
    project = make_project(tmp_path)
    record_live_plan(project)  # buys VTI 500, VXUS 200, QQQM 150, AVUV 150
    arm(project)
    capture_review(project, "VTI", "buy", "500.00")
    return project


def run(project, payload=None, env=LIVE_ENV, now=NOW):
    return pre_tool_use(payload or order_payload(), project, env, now=now, bounds=session)


def line_status(project, symbol):
    with Ledger(project / "state" / "ledger.sqlite3") as ledger:
        run_ = ledger.live_run_for(TODAY)
        return next(x.status for x in ledger.lines(run_.run_id) if x.symbol == symbol)


class TestToolAllowlist:
    @pytest.mark.parametrize(
        "tool",
        ["get_portfolio", "get_equity_quotes", "get_equity_orders", "review_equity_order", "cancel_equity_order"],
    )
    def test_allowed_tools_pass_silently(self, project, tool):
        result = run(project, {"tool_name": f"mcp__robinhood-trading__{tool}", "tool_input": {}})
        assert not result.denied

    @pytest.mark.parametrize("tool", ["place_option_order", "get_equity_news", "add_to_watchlist", "transfer"])
    def test_everything_else_denied(self, project, tool):
        result = run(project, {"tool_name": f"mcp__robinhood-trading__{tool}", "tool_input": {}})
        assert result.denied and "allowlist" in result.reason

    def test_other_servers_ignored(self, project):
        assert not run(project, {"tool_name": "mcp__other__place_equity_order", "tool_input": {}}).denied


class TestOrderChecks:
    def test_happy_path_claims_the_line(self, project):
        assert not run(project).denied
        assert line_status(project, "VTI") == "CLAIMED"

    def test_same_order_twice_is_refused(self, project):
        assert not run(project).denied
        second = run(project, order_payload(tool_use_id="toolu_2"))
        assert second.denied and "matches 0 pending plan lines" in second.reason

    def test_within_one_dollar_matches(self, project):
        capture_review(project, "VTI", "buy", "500.90")
        assert not run(project, order_payload(amount="500.90")).denied

    def test_beyond_one_dollar_refused(self, project):
        capture_review(project, "VTI", "buy", "501.50")
        result = run(project, order_payload(amount="501.50"))
        assert result.denied and "matches 0" in result.reason

    @pytest.mark.parametrize(
        ("env", "fragment"),
        [
            ({"DRY_RUN": "false"}, "TRADING_ENABLED"),
            ({"TRADING_ENABLED": "true"}, "DRY_RUN"),
            ({"TRADING_ENABLED": "TRUE", "DRY_RUN": "false"}, "TRADING_ENABLED"),
            ({"TRADING_ENABLED": "true", "DRY_RUN": "0"}, "DRY_RUN"),
        ],
    )
    def test_switches(self, project, env, fragment):
        result = run(project, env=env)
        assert result.denied and fragment in result.reason
        assert line_status(project, "VTI") == "PENDING"

    def test_kill_file(self, project):
        (project / "KILL").write_text("")
        assert "kill switch" in run(project).reason

    def test_outside_window_and_closed_days(self, project):
        assert "outside the execution window" in run(project, now=NOW.replace(hour=21, minute=0)).reason
        assert "outside the execution window" in run(project, now=NOW.replace(hour=14, minute=31)).reason
        saturday = NOW + timedelta(days=5)
        assert "not an NYSE trading session" in run(project, now=saturday).reason

    def test_integrity(self, project):
        limits = project / "config" / "limits.yaml"
        limits.write_text(limits.read_text().replace("max_order_usd: '1500'", "max_order_usd: '1500.00'"))
        result = run(project)
        assert result.denied and "changed since arming: config/limits.yaml" in result.reason

    def test_not_armed(self, project):
        (project / "state" / "armed.json").unlink()
        assert "not armed" in run(project).reason

    def test_plan_tampering_detected(self, project):
        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            path = project / ledger.live_run_for(TODAY).plan_path
        doc = json.loads(path.read_text())
        doc["orders"][0]["notional_usd"] = 1500.0
        path.write_text(json.dumps(doc))
        assert "plan hash does not match" in run(project).reason

    def test_plan_file_missing(self, project):
        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            (project / ledger.live_run_for(TODAY).plan_path).unlink()
        assert "cannot read plan file" in run(project).reason

    def test_plan_file_not_an_object(self, project):
        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            (project / ledger.live_run_for(TODAY).plan_path).write_text("[1, 2]")
        assert "not a JSON object" in run(project).reason

    def test_no_live_run_today(self, project):
        tomorrow = NOW + timedelta(days=1)
        capture_review(project, now=tomorrow)
        assert "no live run" in run(project, now=tomorrow).reason

    def test_no_ledger(self, tmp_path):
        project = make_project(tmp_path)
        arm(project)
        assert "no ledger" in run(project).reason

    def test_stale_plan(self, project):
        later = NOW + timedelta(minutes=61)
        capture_review(project, now=later)
        assert "older than" in run(project, now=later).reason

    def test_order_limits(self, project):
        assert "not on the whitelist" in run(project, order_payload(symbol="TSLA")).reason
        capture_review(project, "VTI", "buy", "1500.01")
        assert "outside" in run(project, order_payload(amount="1500.01")).reason
        capture_review(project, "VTI", "buy", "0.50")
        assert "outside" in run(project, order_payload(amount="0.50")).reason

    def test_unparseable_order(self, project):
        assert "unrecognized order parameter" in run(project, order_payload(stop_price="1")).reason

    def test_missing_review(self, project):
        capture_review(project, "VXUS", "buy", "200.00", now=NOW - timedelta(minutes=11))
        result = run(project, order_payload(symbol="VXUS", amount="200.00"))
        assert result.denied and "review_equity_order" in result.reason

    def test_review_not_required_when_disabled(self, tmp_path):
        project = make_project(tmp_path, limits={"require_review": False})
        record_live_plan(project)
        arm(project)
        assert not run(project, order_payload(symbol="QQQM", amount="150.00")).denied

    def test_wrong_account(self, tmp_path):
        project = make_project(tmp_path, limits={"agentic_account_id": "AGENT1"})
        record_live_plan(project)
        arm(project)
        capture_review(project)
        assert "other than the agentic account" in run(project, order_payload(account_number="MAIN")).reason
        assert not run(project, order_payload(account_number="AGENT1")).denied

    def test_wash_sale_buy_block(self, project):
        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            ledger.record_trade(
                TradeRecord("VTSAX", Side.SELL, TODAY - timedelta(days=3), D(100), had_loss=True), "manual"
            )
        result = run(project)
        assert result.denied and "wash-sale" in result.reason

    def test_config_error(self, project):
        (project / "config" / "limits.yaml").write_text("min_order_usd: [")
        assert "config error" in run(project).reason

    def test_daily_limit_across_lines(self, tmp_path):
        project = make_project(tmp_path, limits={"max_daily_notional_usd": "1500", "max_order_usd": "1500"})
        snapshot = make_snapshot(on_target_shares(), cash=5000)
        record_live_plan(project, snapshot)  # contribution limited to 1500: VTI 750, VXUS 300, ...
        arm(project)
        capture_review(project, "VTI", "buy", "750.00")
        assert not run(project, order_payload(amount="750.00")).denied
        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            used = ledger.notional_used(TODAY)
        assert used == D(750)


class TestApprovals:
    @pytest.fixture
    def reset_project(self, tmp_path):
        project = make_project(tmp_path)
        shares = {"VTI": D(5550) / 300, "VXUS": D(30), "QQQM": D("6.75"), "AVUV": D("13.5")}
        record_live_plan(project, make_snapshot(shares, cash=0), annual_reset_requested=True)
        arm(project)
        capture_review(project, "VTI", "sell", "525.00")
        return project

    def test_requires_approval(self, reset_project):
        result = run(reset_project, order_payload(side="sell", amount="525.00"))
        assert result.denied and "requires approval" in result.reason

    def test_approved_today_passes(self, reset_project):
        with Ledger(reset_project / "state" / "ledger.sqlite3") as ledger:
            live = ledger.live_run_for(TODAY)
            ledger.record_approval(live.run_id, live.plan_sha256, TODAY, NOW)
        assert not run(reset_project, order_payload(side="sell", amount="525.00")).denied

    def test_yesterdays_approval_does_not_count(self, reset_project):
        with Ledger(reset_project / "state" / "ledger.sqlite3") as ledger:
            live = ledger.live_run_for(TODAY)
            ledger.record_approval(live.run_id, live.plan_sha256, TODAY - timedelta(days=1), NOW)
        assert run(reset_project, order_payload(side="sell", amount="525.00")).denied


class TestFindReview:
    def test_ignores_failures_and_garbage(self, project):
        raw = project / "state" / "raw" / TODAY.isoformat()
        (raw / "x_review_equity_order_bad.json").write_text("{not json")
        capture_review(project, "AVUV", "buy", "150.00", event="PostToolUseFailure")
        order = parse_order({"symbol": "AVUV", "side": "buy", "amount": "150.00"})
        assert not find_review(raw, order, NOW, Decimal(1), timedelta(minutes=10))

    def test_missing_dir(self, tmp_path):
        order = parse_order({"symbol": "AVUV", "side": "buy", "amount": "150.00"})
        assert not find_review(tmp_path / "nope", order, NOW, Decimal(1), timedelta(minutes=10))


def test_hook_output_format():
    code, out = hook_output(deny("because"))
    assert code == 2
    body = json.loads(out)["hookSpecificOutput"]
    assert body == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "rebalancer guard: because",
    }
    assert hook_output(ALLOW) == (0, "")


class TestCheckOrderDirect:
    """Defensive checks that need a self-consistent but wrong plan document."""

    def _ctx(self, project, **plan_changes):
        from rebalancer.config import load_config
        from rebalancer.guard import OrderContext
        from rebalancer.ledger import RunRow
        from rebalancer.plan import finalize

        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            run = ledger.live_run_for(TODAY)
        doc = json.loads((project / run.plan_path).read_text())
        doc.update(plan_changes)
        doc = finalize(doc)
        live_run = RunRow(doc["run_id"], TODAY, "live", doc["plan_sha256"], run.plan_path, False, doc["action"], NOW)
        config = load_config(project / "config")
        return OrderContext(
            now=NOW,
            today=TODAY,
            env=LIVE_ENV,
            limits=config.limits,
            targets=frozenset(config.strategy.targets),
            kill_file=False,
            manifest_problems=(),
            window=session(TODAY),
            live_run=live_run,
            plan=doc,
            plan_error=None,
            approved=False,
            wash_sale_blocked=frozenset(),
            review_found=True,
        )

    def test_consistent_plan_passes(self, project):
        from rebalancer.guard import check_order

        assert check_order(parse_order({"symbol": "VTI", "side": "buy", "amount": "500"}), self._ctx(project)) is None

    @pytest.mark.parametrize(
        ("changes", "fragment"),
        [
            ({"mode": "dry_run"}, "not a live plan"),
            ({"date": "2026-11-01"}, "not for today"),
            ({"blockers": ["OPEN_ORDERS"]}, "has blockers"),
        ],
    )
    def test_wrong_but_sealed_plans(self, project, changes, fragment):
        from rebalancer.guard import check_order

        reason = check_order(
            parse_order({"symbol": "VTI", "side": "buy", "amount": "500"}), self._ctx(project, **changes)
        )
        assert fragment in reason

    def test_plan_not_matching_ledger(self, project):
        from dataclasses import replace

        from rebalancer.guard import check_order

        ctx = self._ctx(project)
        ctx = replace(ctx, live_run=replace(ctx.live_run, plan_sha256="0" * 64))
        assert "does not match the ledger" in check_order(
            parse_order({"symbol": "VTI", "side": "buy", "amount": "500"}), ctx
        )

    def test_absolute_plan_path(self, project):
        from rebalancer.guard import _load_plan

        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            run = ledger.live_run_for(TODAY)
        from dataclasses import replace

        absolute = replace(run, plan_path=str(project / run.plan_path))
        doc, error = _load_plan(absolute, project / "elsewhere")
        assert error is None and doc["run_id"] == run.run_id
        assert _load_plan(None, project) == (None, None)

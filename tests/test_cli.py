"""End-to-end CLI flows in a temp project (no network, injected clock and terminal)."""

import json
from datetime import timedelta

import pytest

from rebalancer.cli import Runtime, find_project, main, merge_trades
from rebalancer.guard import pre_tool_use
from rebalancer.ledger import BrokerOrder, Ledger
from rebalancer.models import Side, TradeRecord
from tests.builders import (
    LIVE_ENV,
    NOW,
    TODAY,
    capture_review,
    make_project,
    order_payload,
    session,
)
from tests.conftest import D
from tests.test_snapshot import capture_tool, mcp, write_raw_session


def runtime(project, *, env=None, tty=False, inputs=(), now=NOW):
    out: list[str] = []
    answers = iter(inputs)
    rt = Runtime(
        project=project, now=now, environ=env or {}, input_fn=lambda _prompt: next(answers), is_tty=tty, out=out.append
    )
    return rt, out


def cli(project, *argv, **kwargs):
    rt, out = runtime(project, **kwargs)
    code = main(list(argv), runtime=rt)
    return code, "\n".join(out)


@pytest.fixture
def project(tmp_path):
    project = make_project(tmp_path)
    write_raw_session(project / "state")
    assert cli(project, "snapshot")[0] == 0
    return project


def live_run(project):
    with Ledger(project / "state" / "ledger.sqlite3") as ledger:
        return ledger.live_run_for(TODAY)


class TestPlanning:
    def test_dry_run_plan(self, project):
        code, out = cli(project, "plan")
        assert code == 0
        assert "mode=dry_run" in out and "buy VTI $515.00" in out
        doc = json.loads((project / "state" / "plan.json").read_text())
        assert doc["mode"] == "dry_run"
        assert (project / "state" / "reports" / f"{doc['run_id']}.md").exists()
        assert live_run(project) is None

    def test_live_plan_once_per_day(self, project):
        code, out = cli(project, "plan", env=LIVE_ENV)
        assert code == 0 and "mode=live" in out
        first = live_run(project)
        code, out = cli(project, "plan", env=LIVE_ENV)
        assert "mode=dry_run" in out and "LIVE_RUN_EXISTS_TODAY" in out
        assert f"today's live run is {first.run_id}" in out
        assert live_run(project) == first

    def test_identical_dry_runs_are_idempotent(self, project):
        assert cli(project, "plan")[0] == 0
        assert cli(project, "plan")[0] == 0

    def test_kill_switch_keeps_plan_dry(self, project):
        (project / "KILL").write_text("")
        _, out = cli(project, "plan", env=LIVE_ENV)
        assert "mode=dry_run" in out and "KILL_SWITCH" in out

    def test_report_command(self, project):
        assert cli(project, "report")[0] == 2  # nothing yet
        cli(project, "plan")
        code, out = cli(project, "report")
        assert code == 0 and "# Rebalancer report" in out
        run_id = json.loads((project / "state" / "plan.json").read_text())["run_id"]
        assert "# Rebalancer report" in cli(project, "report", run_id)[1]

    def test_missing_snapshot_is_an_error(self, tmp_path):
        project = make_project(tmp_path)
        assert cli(project, "plan")[0] == 2


class TestAuthorityCommands:
    def test_terminal_required(self, project):
        for argv in (["approve", "x"], ["arm"], ["rearm", "x", "L1"], ["dismiss-reset", "2026"]):
            assert cli(project, *argv)[0] == 2

    def test_arm(self, project):
        assert cli(project, "arm", tty=True, inputs=["no"])[0] == 1
        code, out = cli(project, "arm", tty=True, inputs=["ARM"])
        assert code == 0 and "armed" in out
        assert "armed: yes" in cli(project, "status")[1]

    def test_approve(self, project):
        cli(project, "plan", "--annual-reset", env=LIVE_ENV)
        run = live_run(project)
        assert run.requires_approval
        assert cli(project, "approve", run.run_id, tty=True, inputs=["wrong"])[0] == 1
        code, out = cli(project, "approve", run.run_id, tty=True, inputs=[run.plan_sha256[:8]])
        assert code == 0 and "approved" in out
        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            assert ledger.has_approval(run.run_id, run.plan_sha256, TODAY)

    def test_approve_refuses_other_runs_and_tampered_plans(self, project):
        assert cli(project, "approve", "nope", tty=True, inputs=["x"])[0] == 2
        cli(project, "plan", env=LIVE_ENV)
        run = live_run(project)
        path = project / run.plan_path
        doc = json.loads(path.read_text())
        doc["orders"][0]["notional_usd"] = 1499.0
        path.write_text(json.dumps(doc))
        assert cli(project, "approve", run.run_id, tty=True, inputs=["x"])[0] == 2

    def test_rearm_and_dismiss(self, project):
        cli(project, "plan", env=LIVE_ENV)
        run = live_run(project)
        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            ledger.claim_line(
                run.run_id,
                "VTI",
                "buy",
                D("515"),
                tolerance=D(1),
                daily_cap=D(3000),
                run_date=TODAY,
                now=NOW,
                tool_use_id="t1",
            )
            ledger.reconcile(run.run_id, [], NOW + timedelta(minutes=10), tolerance=D(1))
        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            vti = next(x for x in ledger.lines(run.run_id) if x.symbol == "VTI")
        assert vti.status == "NOT_PLACED"
        assert cli(project, "rearm", run.run_id, vti.line_id, tty=True, inputs=["no"])[0] == 1
        code, _ = cli(project, "rearm", run.run_id, vti.line_id, tty=True, inputs=["YES"])
        assert code == 0
        assert cli(project, "dismiss-reset", "2026", tty=True)[0] == 0
        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            assert ledger.completed_reset_years() == frozenset({2026})


class TestReconcileAndStatus:
    def test_full_live_flow(self, project):
        assert cli(project, "arm", tty=True, inputs=["ARM"])[0] == 0
        cli(project, "plan", env=LIVE_ENV)
        run = live_run(project)
        capture_review(project, "VTI", "buy", "515.00")
        assert not pre_tool_use(
            order_payload(amount="515.00", tool_use_id="tu1"), project, LIVE_ENV, now=NOW, bounds=session
        ).denied
        capture_tool(
            project / "state",
            "get_equity_orders",
            mcp(
                {
                    "results": [
                        {
                            "id": "rh-1",
                            "symbol": "VTI",
                            "side": "buy",
                            "state": "filled",
                            "dollar_amount": "515.00",
                            "filled_notional": "515.00",
                            "created_at": "2026-11-02T15:00:30Z",
                        }
                    ]
                }
            ),
            now=NOW + timedelta(minutes=1),
        )
        code, out = cli(project, "reconcile", now=NOW + timedelta(minutes=2))
        assert code == 0 and "VTI $515.00 -> FILLED (updated)" in out
        status = cli(project, "status", env=LIVE_ENV)[1]
        assert f"live run today: {run.run_id}" in status and "-> FILLED" in status
        assert "TRADING_ENABLED=true" in status

    def test_reconcile_without_live_run(self, project):
        code, out = cli(project, "reconcile")
        assert code == 0 and "nothing to reconcile" in out

    def test_annual_reset_marked_executed(self, project):
        # No cash: the full reset has to sell QQQM and AVUV (15.1% each) down to 15%.
        write_raw_session(project / "state", cash="0")
        assert cli(project, "snapshot")[0] == 0
        cli(project, "plan", "--annual-reset", env=LIVE_ENV)
        run = live_run(project)
        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            sell = next(x for x in ledger.lines(run.run_id) if x.side == "sell")
            assert sell.notional_usd == D("10.50")
            ledger.claim_line(
                run.run_id,
                sell.symbol,
                "sell",
                sell.notional_usd,
                tolerance=D(1),
                daily_cap=D(3000),
                run_date=TODAY,
                now=NOW,
                tool_use_id="t1",
            )
            ledger.mark_submitted("t1", "rh-9", NOW)
        capture_tool(
            project / "state",
            "get_equity_orders",
            mcp(
                {
                    "results": [
                        {
                            "id": "rh-9",
                            "symbol": sell.symbol,
                            "side": "sell",
                            "state": "filled",
                            "filled_notional": str(sell.notional_usd),
                            "created_at": "2026-11-02T15:00:30Z",
                        }
                    ]
                }
            ),
        )
        cli(project, "reconcile")
        with Ledger(project / "state" / "ledger.sqlite3") as ledger:
            assert 2026 in ledger.completed_reset_years()

    def test_status_reports_config_errors_and_unarmed(self, project):
        (project / "config" / "limits.yaml").write_text("nonsense: 1\n")
        out = cli(project, "status")[1]
        assert "CONFIG ERROR" in out and "armed: NO" in out and "no live run today" in out


class TestHelpers:
    def test_find_project(self, project):
        nested = project / "state"
        assert find_project(nested, {}) == project
        assert find_project(nested, {"CLAUDE_PROJECT_DIR": str(project)}) == project
        with pytest.raises(Exception, match="cannot find"):
            find_project(project.parent, {})

    def test_merge_trades_prefers_ledger_loss_estimate(self):
        broker = [
            TradeRecord("VTI", Side.SELL, TODAY, D(10), None, "o1"),
            TradeRecord("VTI", Side.BUY, TODAY, D(5), None, "o2"),
        ]
        ledger = [
            TradeRecord("VTI", Side.SELL, TODAY, D(10), False, "o1"),
            TradeRecord("AVUV", Side.SELL, TODAY, D(1), True, None),
        ]
        merged = merge_trades(broker, ledger)
        assert [(t.order_id, t.had_loss) for t in merged] == [("o1", False), ("o2", None), (None, True)]


class FakeBroker:
    def __init__(self, snapshot):
        self.snap = snapshot
        self.submitted = []

    def snapshot(self, now):
        return self.snap

    def submit(self, order, client_order_id):
        self.submitted.append((order, client_order_id))
        return f"alp-{len(self.submitted)}"

    def orders(self):
        return [
            BrokerOrder(f"alp-{i + 1}", o.symbol, o.side.value, "filled", o.notional_usd, o.notional_usd, NOW, NOW)
            for i, (o, _) in enumerate(self.submitted)
        ]


class TestPaperRun:
    def _run(self, project, env):
        from rebalancer.broker.snapshot import load_snapshot
        from rebalancer.cli import cmd_paper_run

        snapshot, _ = load_snapshot(project / "state" / "snapshot.json")
        broker = FakeBroker(snapshot)
        rt, out = runtime(project, env=env)
        code = cmd_paper_run(rt, None, broker_factory=lambda: broker, sleep=lambda _s: None)
        return code, "\n".join(out), broker

    def test_dry_run_submits_nothing(self, project):
        code, out, broker = self._run(project, {})
        assert code == 0 and "mode=dry_run" in out and broker.submitted == []

    def test_live_paper_run_goes_through_the_guard(self, project):
        cli(project, "arm", tty=True, inputs=["ARM"])
        code, out, broker = self._run(project, LIVE_ENV)
        assert code == 0, out
        assert len(broker.submitted) == 4
        assert "reconciled" in out
        with Ledger(project / "state" / "paper" / "ledger.sqlite3") as ledger:
            run = ledger.live_run_for(TODAY)
            assert {x.status for x in ledger.lines(run.run_id)} == {"FILLED"}

    def test_guard_refusal_stops_paper_run(self, project):
        code, out, broker = self._run(project, LIVE_ENV)  # never armed
        assert code == 1 and "not armed" in out and broker.submitted == []

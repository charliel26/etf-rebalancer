"""Markdown summary of one run: portfolio, orders, taxes, risk, checks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .risk import RiskSummary


def _usd(value: Any) -> str:
    return "—" if value is None else f"${float(value):,.2f}"


def _pct(value: Any, digits: int = 1) -> str:
    return "—" if value is None else f"{100 * float(value):.{digits}f}%"


def render_report(plan: Mapping[str, Any], risk: RiskSummary | None) -> str:
    p = plan["portfolio"]
    lines = [
        f"# Rebalancer report — {plan['date']}",
        "",
        f"- Run: `{plan['run_id']}`",
        f"- Mode: **{plan['mode']}**"
        + (" (orders may be placed)" if plan["mode"] == "live" else " (nothing will be traded)"),
        f"- Action: **{plan['action']}**",
        f"- Requires approval: **{'yes' if plan['requires_approval'] else 'no'}**",
    ]
    if plan["blockers"]:
        lines += ["", "## Blockers", "", *[f"- `{b}`" for b in plan["blockers"]]]
    if plan["flags"]:
        lines += ["", "## Flags", "", *[f"- `{f}`" for f in plan["flags"]]]

    lines += ["", "## Portfolio", ""]
    lines += [
        "| Total | Core | Satellite | Unmanaged | Cash | Reserved | Deployable |",
        "|---|---|---|---|---|---|---|",
        f"| {_usd(p['total_value'])} | {_usd(p['core_value'])} | {_usd(p['satellite_value'])} | "
        f"{_usd(p['unmanaged_value'])} | {_usd(p['cash'])} | {_usd(p['reserve'])} | {_usd(p['deployable_cash'])} |",
        "",
    ]
    if p.get("equity_gap") is not None:
        lines += [
            f"Broker-reported equity {_usd(p['reported_equity'])}; positions × quotes + cash differ from it by "
            f"{_usd(p['equity_gap'])}.",
            "",
        ]
    if p["funds"]:
        lines += ["| Fund | Value | Weight | Target | Band | After buys | Breach |", "|---|---|---|---|---|---|---|"]
        for symbol, f in p["funds"].items():
            lines.append(
                f"| {symbol} | {_usd(f['value'])} | {_pct(f['weight'])} | {_pct(f['target'])} | "
                f"{_pct(f['band_low'])}–{_pct(f['band_high'])} | {_pct(f['projected_weight'])} | {f['breach'] or ''} |"
            )
    if p["deployment"]:
        lines += ["", "Phased deployment:", ""]
        lines += [
            f"- {t['date']}: {_usd(t['amount_usd'])} ({'released' if t['released'] else 'held back'})"
            for t in p["deployment"]
        ]
    if p["annual_reset_due"]:
        lines += [
            "",
            "**Annual reset is due.** In a terminal, run `rebalancer plan --annual-reset` to see the proposal "
            "(it needs your approval), or `rebalancer dismiss-reset <year>` to skip it this year. See RUNBOOK.md.",
        ]

    lines += ["", "## Orders", ""]
    if plan["orders"]:
        lines += ["| Line | Side | Symbol | Amount | Reason |", "|---|---|---|---|---|"]
        lines += [
            f"| {o['line_id']} | {o['side']} | {o['symbol']} | {_usd(o['notional_usd'])} | {o['reason']} |"
            for o in plan["orders"]
        ]
        lines.append(f"\nTotals: buys {_usd(plan['totals']['buys'])}, sells {_usd(plan['totals']['sells'])}.")
    else:
        lines.append("No orders.")
    if plan["deferred_orders"]:
        lines += ["", "Sale proceeds will be redeployed at the next run (after settlement), roughly:", ""]
        lines += [f"- buy {o['symbol']} {_usd(o['notional_usd'])}" for o in plan["deferred_orders"]]

    tax = plan["tax_estimate"]
    lines += [
        "",
        "## Tax estimate",
        "",
        f"Lot method `{tax['lot_method']}`: short-term {_usd(tax['st_gain_usd'])}, "
        f"long-term {_usd(tax['lt_gain_usd'])} (estimated at plan prices).",
    ]

    lines += ["", "## Risk (report only)", ""]
    if risk is None:
        lines.append("No risk data.")
    else:
        lines += [
            f"- EWMA volatility (63 days, annualized): {_pct(risk.vol_annualized)}",
            f"- Drawdown of the current mix: {_pct(risk.drawdown_current)} "
            f"(worst in window {_pct(risk.drawdown_worst)})",
            f"- Satellite share: {_pct(risk.satellite_share)}",
            f"- Observations: {risk.observations}",
        ]
        if risk.flags:
            lines.append(f"- Flags: {', '.join(f'`{x}`' for x in risk.flags)}")

    if plan["checks"]:
        lines += ["", "## Checks", ""]
        lines += [f"- {'PASS' if ok else 'FAIL'} `{name}`" for name, ok in plan["checks"].items()]
    lines += ["", f"Plan hash `{plan['plan_sha256']}`", ""]
    return "\n".join(lines)

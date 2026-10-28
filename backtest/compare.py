"""Run every comparison in STRATEGY.md §13 and write the report.

python -m backtest.compare --download    fetch data (needs network), then run -> backtest/report.md
python -m backtest.compare               run on cached data in backtest/data/raw -> backtest/report.md
python -m backtest.compare --synthetic   pipeline check on FAKE data -> backtest/out/synthetic/ (not results)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np

from rebalancer.config import load_config
from rebalancer.risk import drawdown as risk_drawdown
from rebalancer.risk import ewma_vol

from . import charts, metrics
from .contributions import contribution_stream
from .data import MANIFEST, RAW_DIR, DataUnavailable, MarketData, build_market, download, synthetic_market
from .params import CONFIG_PATH, BacktestParams, load_params
from .signal_study import below_ema, study
from .simulate import SimResult, Strategy, contributions_for_window, simulate

REPO = Path(__file__).resolve().parents[1]
REPORT = Path(__file__).with_name("report.md")
FIGURES = Path(__file__).with_name("figures")
SYNTHETIC_OUT = Path(__file__).with_name("out") / "synthetic"


# ---------------------------------------------------------------- strategies


def core_targets() -> dict[str, Decimal]:
    return dict(load_config(REPO / "config").strategy.targets)


def make_strategies(params: BacktestParams, targets: Mapping[str, Decimal]) -> list[Strategy]:
    cap = params.st_cap_fraction
    lot = params.lot_method
    low, _, high = params.band_widths
    return [
        Strategy("S1", {"VT": Decimal(1)}, lot_method=lot),
        Strategy("S2", targets, lot_method=lot),
        Strategy("S3", targets, band_width=params.headline_band, st_cap_fraction=cap, lot_method=lot),
        Strategy("S4a", targets, band_width=low, st_cap_fraction=cap, lot_method=lot),
        Strategy("S4b", targets, band_width=high, st_cap_fraction=cap, lot_method=lot),
        Strategy("S5", targets, ema_signal=True, lot_method=lot),
        Strategy("S6", targets, annual_reset=True, lot_method=lot),
    ]


def sensitivity_variants(s3: Strategy, s5: Strategy) -> list[tuple[str, Strategy]]:
    return [
        ("S3, FIFO lot relief", replace(s3, lot_method="fifo")),
        ("S3, fixed 100 USD ST-gain cap", replace(s3, st_cap_fraction=None, st_cap_fixed=Decimal(100))),
        ("S3, AVUV proxy DFSVX", replace(s3, series={"AVUV": "AVUV_DFSVX"})),
        ("S3, cash earns T-bill rate", replace(s3, cash_yield=True)),
        ("S5, cash earns T-bill rate", replace(s5, cash_yield=True)),
    ]


def run_suite(
    market: MarketData,
    params: BacktestParams,
    strategies: Sequence[Strategy],
    stream: Mapping[int, Decimal],
    start: int,
    end: int,
) -> dict[str, SimResult]:
    window = contributions_for_window(stream, start, end)
    return {s.name: simulate(market, s, params, window, start, end) for s in strategies}


# ---------------------------------------------------------------- summaries


def summarize(result: SimResult, rf: np.ndarray) -> dict:
    r = metrics.twr_returns(result.values, result.flows)
    dd = metrics.drawdown_stats(r, result.dates)
    years = max((result.dates[-1] - result.dates[0]).days / 365.25, 1e-9)
    average_value = float(np.mean(result.values)) or 1.0
    sells = sum(float(t.notional) for t in result.trades if t.side == "sell")
    gross = sum(float(t.notional) for t in result.trades)
    avg_cash, drag, cash_days = metrics.cash_stats(
        result.cash, result.values, result.invested_returns, np.zeros(len(result.values))
    )
    return {
        "cagr": metrics.cagr(r, result.dates[0], result.dates[-1]),
        "after_tax": result.after_tax_terminal,
        "irr": metrics.xirr(result.irr_flows),
        "vol": metrics.annualized_vol(r),
        "sharpe": metrics.sharpe(r, rf),
        "max_dd": dd.max_drawdown,
        "underwater": dd.longest_underwater_days,
        "unrecovered": dd.longest_unrecovered,
        "turnover": sells / average_value / years,
        "gross_turnover": gross / average_value / years,
        "trades": len(result.trades),
        "realized_st": sum(float(t.st_gain) for t in result.trades),
        "realized_lt": sum(float(t.lt_gain) for t in result.trades),
        "taxes": sum(result.taxes_paid.values()) + result.liquidation_tax,
        "taxes_paid": sum(result.taxes_paid.values()),
        "avg_cash": avg_cash,
        "cash_drag_bp": drag * 10000,
        "cash_days": cash_days,
        "wash_sales": result.wash_sale_violations,
        "contributions": result.contributions,
    }


def equal_blocks(start: int, end: int, n: int) -> list[tuple[int, int]]:
    edges = np.linspace(start, end, n + 1).round().astype(int)
    return [(int(edges[i]) if i == 0 else int(edges[i]) + 1, int(edges[i + 1])) for i in range(n)]


def walk_forward_choice(block_rows: Sequence[Mapping[str, dict]]) -> list[dict]:
    """Band width with the best after-tax wealth in block k-1, applied in block k."""
    options = {"S4a": "10%", "S3": "20%", "S4b": "30%"}
    out = []
    for k in range(1, len(block_rows)):
        previous = block_rows[k - 1]
        best = max(options, key=lambda name: previous[name]["after_tax"])
        out.append(
            {
                "block": k + 1,
                "chosen": f"{options[best]} ({best})",
                "chosen_after_tax": block_rows[k][best]["after_tax"],
                "fixed_after_tax": block_rows[k]["S3"]["after_tax"],
            }
        )
    return out


def window_indices(dates: Sequence[date], start: date, end: date) -> tuple[int, int] | None:
    idx = [i for i, d in enumerate(dates) if start <= d <= end]
    return (idx[0], idx[-1]) if len(idx) > 1 else None


def crisis_rows(results: Mapping[str, SimResult], params: BacktestParams, targets: Mapping[str, Decimal]) -> list[dict]:
    rows = []
    for window in params.crisis_windows:
        for name, result in results.items():
            span = window_indices(result.dates, window.start, window.end)
            if span is None:
                continue
            a, b = span
            r = metrics.twr_returns(result.values, result.flows)[a + 1 : b + 1]
            nav = np.cumprod(1.0 + r)
            in_window = [t for t in result.trades if window.start <= t.day <= window.end]
            deviation = float("nan")
            if name != "S1":
                devs = [
                    abs(result.weights[s][i] / float(w) - 1.0)
                    for s, w in targets.items()
                    for i in range(a, b + 1)
                    if not math.isnan(result.weights[s][i])
                ]
                deviation = max(devs) if devs else float("nan")
            rows.append(
                {
                    "window": window.name,
                    "strategy": name,
                    "return": float(nav[-1] - 1.0) if len(nav) else 0.0,
                    "max_dd": float(np.min(nav / np.maximum.accumulate(nav) - 1.0)) if len(nav) else 0.0,
                    "vol": float(np.std(r, ddof=1) * math.sqrt(252)) if len(r) > 1 else float("nan"),
                    "trades": len(in_window),
                    "realized": sum(float(t.st_gain + t.lt_gain) for t in in_window),
                    "max_deviation": deviation,
                }
            )
    return rows


def risk_flag_rates(result: SimResult, lam: float = 0.94, window: int = 63, lookback: int = 756) -> dict:
    r = metrics.twr_returns(result.values, result.flows)
    vol_days = dd_days = counted = 0
    for t in range(window, len(r)):
        vol = ewma_vol(r[t - window + 1 : t + 1], lam, window)
        dd = risk_drawdown(r[max(1, t - lookback + 1) : t + 1], lookback)
        counted += 1
        vol_days += vol is not None and vol > 0.20
        dd_days += dd is not None and dd[0] < -0.25
    return {"days": counted, "vol_flag": vol_days / max(counted, 1), "dd_flag": dd_days / max(counted, 1)}


# ---------------------------------------------------------------- formatting


def pct(x: float, digits: int = 2) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.{digits}f}%"


def usd(x: float) -> str:
    if x is None or math.isnan(x):
        return "n/a"
    rounded = round(x)
    return f"-${-rounded:,.0f}" if rounded < 0 else f"${rounded:,.0f}"


def num(x: float, digits: int = 2) -> str:
    return "n/a" if x is None or math.isnan(x) else f"{x:.{digits}f}"


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return [
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
        *["| " + " | ".join(row) + " |" for row in rows],
    ]


def picture(stem: str, alt: str, rel: str) -> str:
    return (
        f'<picture><source media="(prefers-color-scheme: dark)" srcset="{rel}/{stem}-dark.png">'
        f'<img alt="{alt}" src="{rel}/{stem}-light.png"></picture>'
    )


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


# ---------------------------------------------------------------- report


def build_report(market: MarketData, params: BacktestParams, start: int, end: int, out_dir: Path) -> str:
    targets = core_targets()
    strategies = make_strategies(params, targets)
    by_name = {s.name: s for s in strategies}
    stream = contribution_stream(len(market.dates), params.contributions, params.headline_seed)
    rf = market.rf[start : end + 1]

    headline = run_suite(market, params, strategies, stream, start, end)
    full = {name: summarize(r, rf) for name, r in headline.items()}

    blocks = equal_blocks(start, end, params.blocks)
    block_rows = []
    for a, b in blocks:
        results = run_suite(market, params, strategies, stream, a, b)
        block_rows.append({name: summarize(r, market.rf[a : b + 1]) for name, r in results.items()})
    wf = walk_forward_choice(block_rows)

    crises = crisis_rows(headline, params, targets)

    signal_rows = []
    for symbol in sorted(targets):
        prices = market.tr_index[symbol]
        signal_rows += study(symbol, prices, below_ema(prices, params.ema_span), params.horizons, start)

    ratios: dict[str, list[float]] = {name: [] for name in by_name}
    for seed in params.robustness_seeds:
        seed_stream = contribution_stream(len(market.dates), params.contributions, seed)
        results = run_suite(market, params, strategies, seed_stream, start, end)
        base = results["S3"].after_tax_terminal
        for name, r in results.items():
            ratios[name].append(r.after_tax_terminal / base)

    sensitivities = []
    for label, variant in sensitivity_variants(by_name["S3"], by_name["S5"]):
        result = run_suite(market, params, [variant], stream, start, end)[variant.name]
        reference = full[variant.name]
        sensitivities.append((label, summarize(result, rf), reference))
    open_start = next(
        (
            i
            for i in range(start, end)
            if all(market.open[s][j] is not None for s in targets for j in range(i + 1, end + 1))
        ),
        None,
    )
    if open_start is not None and end - open_start > 60:
        for execution in ("close", "open"):
            variant = replace(by_name["S3"], execution=execution)
            result = run_suite(market, params, [variant], stream, open_start, end)["S3"]
            sensitivities.append(
                (
                    f"S3 from {market.dates[open_start]}, execute at next {execution}",
                    summarize(result, market.rf[open_start : end + 1]),
                    None,
                )
            )

    flag_rates = risk_flag_rates(headline["S3"])
    figures = charts.render_all(
        headline, ratios, {s: float(w) for s, w in targets.items()}, float(params.headline_band), out_dir / "figures"
    )
    return render(
        market,
        params,
        start,
        end,
        full,
        blocks,
        block_rows,
        wf,
        crises,
        signal_rows,
        ratios,
        sensitivities,
        flag_rates,
        figures,
        headline,
    )


def render(
    market,
    params,
    start,
    end,
    full,
    blocks,
    block_rows,
    wf,
    crises,
    signal_rows,
    ratios,
    sensitivities,
    flag_rates,
    figures,
    headline,
) -> str:
    dates = market.dates
    synthetic = market.source == "synthetic"
    lines = ["# Backtest report", ""]
    if synthetic:
        lines += ["> **SYNTHETIC DATA. PIPELINE CHECK ONLY. THESE ARE NOT BACKTEST RESULTS.**", ""]
    manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() and not synthetic else {}
    lines += [
        f"- Generated: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC, code `{git_commit()}`, "
        f"parameters `backtest/config.yaml` sha256 `{hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()[:12]}`",
        f"- Data: {market.source}; sample {dates[start]} to {dates[end]} ({end - start + 1} trading days); "
        f"downloaded {manifest.get('downloaded_at', 'n/a')}",
        f"- Initial capital {usd(float(params.initial_capital))} in {params.tranches} monthly tranches; "
        f"contribution seed {params.headline_seed}; costs {float(params.cost_rate) * 1e4:.0f} bp per trade; "
        f"taxes ST {pct(float(params.taxes.short_term), 0)}, LT {pct(float(params.taxes.long_term), 0)}, paid each "
        f"15 April from the portfolio; lot method `{params.lot_method}`",
        "- Every assumption is in [STRATEGY.md §13](../STRATEGY.md). Nothing was tuned on this sample.",
        "",
        "## 1. Full sample (headline seed)",
        "",
        "CAGR is time-weighted and pre-tax; after-tax terminal wealth assumes everything is sold on the last day "
        "and all remaining tax is paid; IRR is the money-weighted after-tax return on all contributions.",
        "",
    ]
    names = list(full)
    lines += table(
        ["Strategy", "CAGR", "After-tax terminal", "IRR", "Vol", "Sharpe", "Max DD", "Longest underwater (days)"],
        [
            [
                n,
                pct(full[n]["cagr"]),
                usd(full[n]["after_tax"]),
                pct(full[n]["irr"]),
                pct(full[n]["vol"], 1),
                num(full[n]["sharpe"]),
                pct(full[n]["max_dd"], 1),
                f"{full[n]['underwater']}{'*' if full[n]['unrecovered'] else ''}",
            ]
            for n in names
        ],
    )
    if any(full[n]["unrecovered"] for n in names):
        lines += ["", "\\* not yet recovered at the end of the sample."]
    lines += [""]
    lines += table(
        [
            "Strategy",
            "Turnover (sells/yr)",
            "Trades",
            "Realized ST",
            "Realized LT",
            "Taxes incl. liquidation",
            "Avg cash",
            "Cash drag (bp/yr)",
            "Days in cash",
            "Wash sales",
        ],
        [
            [
                n,
                pct(full[n]["turnover"], 1),
                str(full[n]["trades"]),
                usd(full[n]["realized_st"]),
                usd(full[n]["realized_lt"]),
                usd(full[n]["taxes"]),
                pct(full[n]["avg_cash"]),
                num(full[n]["cash_drag_bp"], 1),
                str(full[n]["cash_days"]),
                str(full[n]["wash_sales"]),
            ]
            for n in names
        ],
    )
    lines += [
        "",
        f"Total contributed (initial + stream): {usd(full['S3']['contributions'])}. Cash drag is the "
        "return given up (negative: gained) by holding cash, including the phased-deployment months when "
        "most of the initial capital waits in cash by design.",
        "",
    ]
    for stem, alt in figures:
        lines += [picture(stem, alt, "figures"), ""]

    lines += ["## 2. Walk-forward blocks (each a fresh start)", ""]
    for k, ((a, b), rows) in enumerate(zip(blocks, block_rows, strict=True), start=1):
        lines += [f"**Block {k}: {dates[a]} to {dates[b]}**", ""]
        lines += table(
            ["Strategy", "After-tax terminal", "CAGR", "Max DD", "Trades", "Taxes"],
            [
                [
                    n,
                    usd(rows[n]["after_tax"]),
                    pct(rows[n]["cagr"]),
                    pct(rows[n]["max_dd"], 1),
                    str(rows[n]["trades"]),
                    usd(rows[n]["taxes"]),
                ]
                for n in rows
            ],
        )
        lines.append("")
    lines += ["Walk-forward band choice: the band width that won the previous block, applied to the next.", ""]
    lines += table(
        ["Block", "Chosen (from previous block)", "After-tax terminal", "Fixed 20% (S3)"],
        [[str(row["block"]), row["chosen"], usd(row["chosen_after_tax"]), usd(row["fixed_after_tax"])] for row in wf],
    )

    lines += ["", "## 3. Crisis windows (from the full-sample run)", ""]
    lines += table(
        ["Window", "Strategy", "Return", "Max DD", "Vol", "Trades", "Realized gains", "Max |weight dev|"],
        [
            [
                c["window"],
                c["strategy"],
                pct(c["return"], 1),
                pct(c["max_dd"], 1),
                pct(c["vol"], 1),
                str(c["trades"]),
                usd(c["realized"]),
                pct(c["max_deviation"], 1),
            ]
            for c in crises
        ],
    )

    lines += [
        "",
        "## 4. S5 signal study: close below its 8-day EMA",
        "",
        "Forward returns from the signal close and from the execution close (what S5 can earn). "
        "Difference = mean on signal days minus other days, from OLS on a signal dummy with "
        "Newey-West (Bartlett) errors.",
        "",
    ]
    lines += table(
        [
            "Fund",
            "h",
            "From",
            "Fires",
            "Mean | signal",
            "Mean | other",
            "Mean all",
            "Difference",
            "t (difference)",
            "t (mean | signal)",
            "N",
            "NW lags",
        ],
        [
            [
                s.symbol,
                str(s.horizon),
                s.start,
                pct(s.fire_rate, 1),
                pct(s.mean_signal, 3),
                pct(s.mean_other, 3),
                pct(s.mean_all, 3),
                pct(s.difference, 3),
                num(s.t_difference),
                num(s.t_conditional),
                str(s.observations),
                str(s.lags),
            ]
            for s in signal_rows
        ],
    )

    lines += [
        "",
        f"## 5. Robustness across {len(params.robustness_seeds)} contribution seeds",
        "",
        "After-tax terminal wealth relative to S3 in the same seed (1.00 = same).",
    ]
    lines += [
        "",
        *table(
            ["Strategy", "Median", "Min", "Max", "Seeds above S3"],
            [
                [n, num(float(np.median(v)), 3), num(min(v), 3), num(max(v), 3), f"{sum(x > 1 for x in v)}/{len(v)}"]
                for n, v in ratios.items()
                if n != "S3"
            ],
        ),
    ]

    lines += ["", "## 6. Sensitivities", ""]
    rows = []
    for label, summary, reference in sensitivities:
        delta = "" if reference is None else pct(summary["after_tax"] / reference["after_tax"] - 1, 2)
        rows.append(
            [
                label,
                usd(summary["after_tax"]),
                delta,
                pct(summary["cagr"]),
                usd(summary["taxes"]),
                str(summary["trades"]),
            ]
        )
    lines += table(["Variant", "After-tax terminal", "vs headline", "CAGR", "Taxes", "Trades"], rows)

    lines += [
        "",
        "## 7. Risk flags on S3 (report-only monitor)",
        "",
        f"Share of days the 63-day EWMA volatility exceeded 20%: {pct(flag_rates['vol_flag'], 1)}; "
        f"drawdown below -25%: {pct(flag_rates['dd_flag'], 1)} ({flag_rates['days']} days).",
        "",
    ]

    lines += ["## 8. Data and proxies", ""]
    if market.splices:
        lines += table(
            ["Sleeve", "ETF from", "Overlap days", "Correlation", "Tracking error", "Return gap/yr"],
            [
                [
                    s["sleeve"],
                    s["switch"],
                    str(s.get("overlap_days", "")),
                    num(s.get("correlation", float("nan")), 3),
                    pct(s.get("tracking_error", float("nan"))),
                    pct(s.get("return_gap", float("nan"))),
                ]
                for s in market.splices
            ],
        )
    lines += ["", *[f"- {note}" for note in market.notes]]
    if manifest:
        lines += ["", "Raw files (not committed; re-download and compare hashes):", ""]
        lines += table(
            ["File", "Rows", "First", "Last", "SHA-256"],
            [
                [f, str(e["rows"]), e["first"], e["last"], f"`{e['sha256'][:16]}`"]
                for f, e in sorted(manifest["files"].items())
            ],
        )
    events = {name: dict(r.events) for name, r in headline.items()}
    lines += [
        "",
        "## 9. Diagnostics",
        "",
        "Decision flags counted over the full sample (days on which each occurred):",
        "",
        "```",
        json.dumps(events, indent=1, sort_keys=True),
        "```",
        "",
        "## Change log",
        "",
        "- None: parameters as pre-registered.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backtest.compare", description=__doc__)
    parser.add_argument("--download", action="store_true", help="fetch market data first (needs network)")
    parser.add_argument("--synthetic", action="store_true", help="run on fake data to check the pipeline")
    parser.add_argument("--days", type=int, default=6950, help="synthetic sample length")
    args = parser.parse_args(argv)
    params = load_params()
    if args.synthetic:
        market = synthetic_market(args.days, seed=7)
        out_dir, report_path = SYNTHETIC_OUT, SYNTHETIC_OUT / "report.md"
    else:
        try:
            if args.download:
                download(params)
            market = build_market(params, RAW_DIR)
        except DataUnavailable as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        out_dir, report_path = REPORT.parent, REPORT
    start, end = params.warmup_days, len(market.dates) - 1
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_report(market, params, start, end, out_dir))
    print(f"wrote {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

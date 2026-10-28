"""Static PNG charts for backtest/report.md, rendered in a light and a dark
variant (the report embeds both with <picture> so GitHub picks one).

Colors are the validated categorical slots (fixed order, color follows the
strategy, never its rank); every chart has a table twin in the report.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

from .metrics import twr_returns
from .simulate import SimResult

THEMES = {
    "light": {
        "surface": "#fcfcfb",
        "ink": "#0b0b0b",
        "ink2": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"],
    },
    "dark": {
        "surface": "#1a1a19",
        "ink": "#f0efec",
        "ink2": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9"],
    },
}
ORDER = ("S1", "S2", "S3", "S4a", "S4b", "S5", "S6")  # fixed slot per strategy
LINE_WIDTH = 1.2
DPI = 150


def _slot(name: str) -> int:
    return ORDER.index(name) if name in ORDER else len(ORDER) - 1


def _figure(theme: dict, size: tuple[float, float] = (8.0, 4.2), rows: int = 1, cols: int = 1):
    fig, axes = plt.subplots(rows, cols, figsize=size, squeeze=False)
    fig.patch.set_facecolor(theme["surface"])
    for ax in axes.flat:
        ax.set_facecolor(theme["surface"])
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(theme["axis"])
            ax.spines[side].set_linewidth(0.8)
        ax.tick_params(colors=theme["muted"], labelsize=8, length=0)
        ax.grid(axis="y", color=theme["grid"], linewidth=0.6, linestyle="-")
        ax.set_axisbelow(True)
    return fig, axes


def _legend(ax, theme: dict, **kwargs) -> None:
    legend = ax.legend(frameon=False, fontsize=8, labelcolor=theme["ink2"], **kwargs)
    for handle in legend.legend_handles:
        handle.set_linewidth(2.0)


def _title(ax, theme: dict, title: str, subtitle: str = "") -> None:
    ax.set_title(title, loc="left", fontsize=11, color=theme["ink"], pad=16 if subtitle else 8)
    if subtitle:
        ax.text(0, 1.015, subtitle, transform=ax.transAxes, fontsize=8, color=theme["ink2"], va="bottom")


def _save(fig, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path, dpi=DPI, facecolor=fig.get_facecolor())
    plt.close(fig)


def growth(results: Mapping[str, SimResult], path: Path, theme: dict) -> None:
    fig, axes = _figure(theme)
    ax = axes[0, 0]
    for name in ORDER:
        if name not in results:
            continue
        r = results[name]
        nav = np.cumprod(1.0 + twr_returns(r.values, r.flows))
        ax.plot(r.dates, nav, color=theme["series"][_slot(name)], linewidth=LINE_WIDTH, label=name)
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"${value:g}"))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_ylabel("Growth of $1 (log)", color=theme["ink2"], fontsize=8)
    _title(ax, theme, "Time-weighted growth of $1, pre-tax", "Contributions and tax payments removed as flows")
    _legend(ax, theme, ncol=7, loc="upper left")
    _save(fig, path)


def drawdowns(results: Mapping[str, SimResult], names: Sequence[str], path: Path, theme: dict) -> None:
    fig, axes = _figure(theme)
    ax = axes[0, 0]
    for name in names:
        r = results[name]
        nav = np.cumprod(1.0 + twr_returns(r.values, r.flows))
        dd = nav / np.maximum.accumulate(nav) - 1.0
        color = theme["series"][_slot(name)]
        ax.plot(r.dates, 100 * dd, color=color, linewidth=LINE_WIDTH, label=name)
        ax.annotate(
            name,
            (r.dates[-1], 100 * dd[-1]),
            xytext=(4, 0),
            textcoords="offset points",
            fontsize=8,
            color=theme["ink2"],
            va="center",
        )
    ax.axhline(0, color=theme["axis"], linewidth=0.8)
    ax.set_ylabel("Drawdown from peak (%)", color=theme["ink2"], fontsize=8)
    _title(ax, theme, "Drawdowns", "Time-weighted, pre-tax")
    _legend(ax, theme, loc="best")
    _save(fig, path)


def weights(result: SimResult, targets: Mapping[str, float], band: float, path: Path, theme: dict) -> None:
    symbols = sorted(targets)
    fig, axes = _figure(theme, size=(8.0, 5.2), rows=2, cols=2)
    for ax, (i, symbol) in zip(axes.flat, enumerate(symbols), strict=True):
        w = 100 * np.asarray(result.weights[symbol])
        target = 100 * float(targets[symbol])
        ax.plot(result.dates, w, color=theme["series"][i], linewidth=LINE_WIDTH)
        ax.axhline(target, color=theme["muted"], linewidth=0.8)
        for edge in (target * (1 - band), target * (1 + band)):
            ax.axhline(edge, color=theme["muted"], linewidth=0.8, linestyle=(0, (4, 3)))
        ax.set_title(
            f"{symbol}: target {target:.0f}%, band {target * (1 - band):.0f}-{target * (1 + band):.0f}%",
            loc="left",
            fontsize=9,
            color=theme["ink"],
        )
        ax.set_ylim(0, max(np.nanmax(w) if np.isfinite(w).any() else target, target * (1 + band)) * 1.15)
    fig.suptitle(
        f"{result.strategy}: core weights against their bands (dashed)",
        x=0.01,
        ha="left",
        fontsize=11,
        color=theme["ink"],
    )
    _save(fig, path)


def seed_ratios(ratios: Mapping[str, Sequence[float]], baseline: str, path: Path, theme: dict) -> None:
    names = [n for n in ORDER if n in ratios and n != baseline]
    fig, axes = _figure(theme, size=(8.0, 0.45 * len(names) + 1.6))
    ax = axes[0, 0]
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=theme["grid"], linewidth=0.6, linestyle="-")
    for row, name in enumerate(names):
        values = np.asarray(ratios[name])
        jitter = np.linspace(-0.12, 0.12, len(values))
        ax.scatter(values, row + jitter, s=18, color=theme["series"][0], edgecolors=theme["surface"], linewidths=1.0)
        ax.plot([np.median(values)] * 2, [row - 0.3, row + 0.3], color=theme["ink"], linewidth=1.4)
    ax.axvline(1.0, color=theme["muted"], linewidth=0.8)
    ax.set_yticks(range(len(names)), names, color=theme["ink2"])
    ax.invert_yaxis()
    ax.set_xlabel(f"After-tax terminal wealth relative to {baseline} (1.0 = same)", color=theme["ink2"], fontsize=8)
    _title(ax, theme, f"Robustness across contribution seeds (vs {baseline})", "One dot per seed; bar = median")
    _save(fig, path)


def render_all(
    headline: Mapping[str, SimResult],
    ratios: Mapping[str, Sequence[float]],
    targets: Mapping[str, float],
    band: float,
    out_dir: Path,
) -> list[tuple[str, str]]:
    """Write every chart in both themes; return [(file stem, alt text)]."""
    out_dir.mkdir(parents=True, exist_ok=True)
    charts = [
        ("growth", "Growth of one dollar for each strategy, log scale", lambda p, t: growth(headline, p, t)),
        ("drawdowns", "Drawdowns of S1 and S3", lambda p, t: drawdowns(headline, ["S1", "S3"], p, t)),
        (
            "weights",
            "S3 fund weights over time against their bands",
            lambda p, t: weights(headline["S3"], targets, band, p, t),
        ),
        (
            "seeds",
            "After-tax terminal wealth relative to S3 across seeds",
            lambda p, t: seed_ratios(ratios, "S3", p, t),
        ),
    ]
    for stem, _alt, draw in charts:
        for mode, theme in THEMES.items():
            draw(out_dir / f"{stem}-{mode}.png", theme)
    return [(stem, alt) for stem, alt, _ in charts]

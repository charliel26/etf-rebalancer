"""Market data for the backtest (research only; the live system never uses it).

Pipeline: download (yfinance + FRED, needs network) -> raw CSVs + manifest ->
per-ticker close, open and cash distributions -> proxy splicing -> one aligned
calendar -> MarketData. `synthetic_market` builds a clearly labelled fake
market for tests; it is never used for reported results.
"""

from __future__ import annotations

import hashlib
import io
import json
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from .params import BacktestParams

DATA_DIR = Path(__file__).with_name("data")
RAW_DIR = DATA_DIR / "raw"
MANIFEST = DATA_DIR / "manifest.json"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
PRICE_Q = Decimal("0.000001")
DIST_Q = Decimal("0.00000001")
NOISE = 5e-5  # implied distribution yields below this are adjustment-rounding noise


class DataUnavailable(RuntimeError):
    pass


@dataclass
class MarketData:
    dates: list[date]
    price: dict[str, list[Decimal]]  # close-price level per series (split-adjusted, spliced)
    open: dict[str, list[Decimal | None]]  # None where no real opening price exists
    dist: dict[str, list[Decimal]]  # cash distribution per share on the ex-date
    tr_index: dict[str, np.ndarray]  # total-return index (signals, validation)
    rf: np.ndarray  # daily risk-free return
    source: str  # "yahoo" or "synthetic"
    notes: list[str] = field(default_factory=list)
    splices: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- download


def tickers_needed(params: BacktestParams) -> list[str]:
    names = {params.vt_proxy["etf"], *params.vt_proxy["blend"]}
    for spec in params.sleeves.values():
        names |= {spec["etf"], spec["proxy"]}
    return sorted(names)


def download(params: BacktestParams, raw_dir: Path = RAW_DIR) -> dict:  # pragma: no cover - needs network
    """Fetch full daily histories and write CSVs plus a manifest with SHA-256s."""
    import yfinance as yf

    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"downloaded_at": datetime.now(UTC).isoformat(), "files": {}}
    for ticker in tickers_needed(params):
        frame = yf.Ticker(ticker).history(period="max", auto_adjust=False, actions=True)
        if frame.empty:
            raise DataUnavailable(f"no data returned for {ticker}")
        frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
        frame.index.name = "Date"
        path = raw_dir / f"{ticker}.csv"
        frame.to_csv(path)
        manifest["files"][path.name] = _file_entry(path, frame.index)
    series = params.risk_free["series"]
    with urllib.request.urlopen(FRED_URL.format(series=series), timeout=60) as response:
        text = response.read().decode()
    path = raw_dir / f"{series}.csv"
    path.write_text(text)
    rf = pd.read_csv(io.StringIO(text))
    manifest["files"][path.name] = _file_entry(path, pd.to_datetime(rf.iloc[:, 0]))
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _file_entry(path: Path, index: pd.Index) -> dict:  # pragma: no cover - used by download
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": len(index),
        "first": str(pd.Timestamp(index.min()).date()),
        "last": str(pd.Timestamp(index.max()).date()),
    }


# ---------------------------------------------------------------- per-ticker series


def load_raw(raw_dir: Path, ticker: str) -> pd.DataFrame:
    path = raw_dir / f"{ticker}.csv"
    if not path.exists():
        raise DataUnavailable(f"missing {path}; run `python -m backtest.compare --download` with network access")
    frame = pd.read_csv(path, index_col=0, parse_dates=True)
    return frame.sort_index()


def prepare(frame: pd.DataFrame, ticker: str, notes: list[str]) -> pd.DataFrame:
    """close, open and cash distribution per share for one ticker.

    Reported distributions (Dividends + Capital Gains) are used when the total
    return they imply agrees with the provider's adjusted close to within
    0.1%/yr; otherwise the distributions implied by the adjusted close are used
    and a note is recorded.
    """
    close = frame["Close"].astype(float)
    adj = frame["Adj Close"].astype(float) if "Adj Close" in frame else close
    reported = frame.get("Dividends", 0.0) + frame.get("Capital Gains", 0.0)
    reported = pd.Series(reported, index=frame.index).fillna(0.0).astype(float)
    pr = close / close.shift(1) - 1.0
    tr = adj / adj.shift(1) - 1.0
    implied_yield = (1.0 - (1.0 + pr) / (1.0 + tr)).fillna(0.0)
    implied_yield = implied_yield.where(implied_yield.abs() > NOISE, 0.0)
    implied = implied_yield * close.shift(1).fillna(close)

    years = max((frame.index[-1] - frame.index[0]).days / 365.25, 1 / 365.25)
    tr_reported = float(np.log1p(pr.fillna(0.0) + (reported / close.shift(1)).fillna(0.0)).sum()) / years
    tr_adjusted = float(np.log1p(tr.fillna(0.0)).sum()) / years
    if abs(tr_reported - tr_adjusted) < 0.001:
        dist = reported
    else:
        dist = implied
        notes.append(
            f"{ticker}: reported distributions imply {tr_reported:.4%}/yr vs adjusted close {tr_adjusted:.4%}/yr; "
            "used distributions implied by the adjusted close"
        )
    opens = frame["Open"].astype(float) if "Open" in frame else pd.Series(np.nan, index=frame.index)
    return pd.DataFrame({"close": close, "open": opens, "dist": dist.clip(lower=0.0)})


def switch_date(etf: pd.DataFrame) -> pd.Timestamp:
    """First trading day of the month after the ETF's first month of data."""
    first = etf.index[0]
    next_month = (first + pd.offsets.MonthBegin(1)).normalize()
    return etf.index[etf.index >= next_month][0]


def splice(etf: pd.DataFrame, proxy: pd.DataFrame, name: str) -> tuple[pd.DataFrame, dict]:
    """Proxy history through the ETF's inception month, scaled so its level
    equals the ETF's close on the last proxy day; the real ETF afterwards."""
    switch = switch_date(etf)
    anchor = etf.index[etf.index < switch][-1]
    proxy_upto = proxy[proxy.index <= anchor]
    if proxy_upto.empty:
        raise DataUnavailable(f"{name}: proxy has no data before {anchor.date()}")
    proxy_anchor = proxy_upto.index[-1]
    scale = etf.loc[anchor, "close"] / proxy.loc[proxy_anchor, "close"]
    early = pd.DataFrame(
        {"close": proxy_upto["close"] * scale, "open": np.nan, "dist": proxy_upto["dist"] * scale},
        index=proxy_upto.index,
    )
    early.loc[proxy_anchor, "close"] = etf.loc[anchor, "close"]
    late = etf[etf.index > anchor][["close", "open", "dist"]]
    spliced = pd.concat([early, late])
    overlap = _overlap_stats(etf, proxy)
    return spliced, {"sleeve": name, "switch": str(switch.date()), **overlap}


def _overlap_stats(etf: pd.DataFrame, proxy: pd.DataFrame) -> dict:
    """Daily total-return correlation, tracking error and return gap where both exist."""
    common = etf.index.intersection(proxy.index)
    if len(common) < 30:
        return {"overlap_days": len(common)}

    def total_returns(frame: pd.DataFrame) -> pd.Series:
        f = frame.loc[common]
        return ((f["close"] + f["dist"]) / f["close"].shift(1) - 1.0).dropna()

    a, b = total_returns(etf), total_returns(proxy)
    diff = a - b
    return {
        "overlap_days": len(common),
        "correlation": float(a.corr(b)),
        "tracking_error": float(diff.std() * np.sqrt(252)),
        "return_gap": float(diff.mean() * 252),
    }


def blend(components: dict[str, pd.DataFrame], weights: dict[str, float], start: pd.Timestamp) -> pd.DataFrame:
    """Buy-and-hold blend with distributions paid out (weights drift with price,
    as cap weights do). Level starts at 100."""
    common = None
    for frame in components.values():
        idx = frame.index[frame.index >= start]
        common = idx if common is None else common.intersection(idx)
    units = {k: weights[k] * 100.0 / components[k].loc[common[0], "close"] for k in components}
    close = sum(units[k] * components[k].loc[common, "close"] for k in components)
    dist = sum(units[k] * components[k].loc[common, "dist"] for k in components)
    return pd.DataFrame({"close": close, "open": np.nan, "dist": dist}, index=common)


def align(series: dict[str, pd.DataFrame]) -> tuple[pd.DatetimeIndex, dict[str, pd.DataFrame]]:
    """Common calendar: dates every series has, from the latest start. A
    distribution on a dropped date moves to the next kept date (never lost)."""
    start = max(frame.index[0] for frame in series.values())
    common = None
    for frame in series.values():
        idx = frame.index[frame.index >= start]
        common = idx if common is None else common.intersection(idx)
    out = {}
    for name, frame in series.items():
        frame = frame[frame.index >= start]
        bucket = np.searchsorted(common.values, frame.index.values, side="left")
        keep = bucket < len(common)
        dist = pd.Series(frame["dist"].values[keep]).groupby(bucket[keep]).sum()
        aligned = frame.reindex(common)
        aligned["dist"] = 0.0
        aligned.iloc[dist.index.values, aligned.columns.get_loc("dist")] = dist.values
        out[name] = aligned
    return common, out


def risk_free(raw_dir: Path, series: str, dates: pd.DatetimeIndex) -> np.ndarray:
    """Daily simple return of 3-month T-bills: yield(t-1) * days / 360."""
    path = raw_dir / f"{series}.csv"
    if not path.exists():
        raise DataUnavailable(f"missing {path}")
    frame = pd.read_csv(path)
    rates = pd.to_numeric(frame.iloc[:, 1], errors="coerce").values / 100.0
    curve = pd.Series(rates, index=pd.to_datetime(frame.iloc[:, 0])).ffill()
    on_dates = curve.reindex(dates, method="ffill").fillna(0.0).values
    days = np.diff(dates.values).astype("timedelta64[D]").astype(float)
    out = np.zeros(len(dates))
    out[1:] = on_dates[:-1] * days / 360.0
    return out


def to_market(
    dates: pd.DatetimeIndex,
    frames: dict[str, pd.DataFrame],
    rf: np.ndarray,
    source: str,
    notes: list[str],
    splices: list[dict],
) -> MarketData:
    py_dates = [d.date() for d in dates]
    price, opens, dist, tr = {}, {}, {}, {}
    for name, frame in frames.items():
        closes = frame["close"].values
        price[name] = [Decimal(repr(float(x))).quantize(PRICE_Q) for x in closes]
        opens[name] = [None if np.isnan(x) else Decimal(repr(float(x))).quantize(PRICE_Q) for x in frame["open"].values]
        dist[name] = [Decimal(repr(float(x))).quantize(DIST_Q) for x in frame["dist"].values]
        growth = np.ones(len(closes))
        growth[1:] = (closes[1:] + frame["dist"].values[1:]) / closes[:-1]
        tr[name] = np.cumprod(growth)
    return MarketData(py_dates, price, opens, dist, tr, rf, source, notes, splices)


def build_market(params: BacktestParams, raw_dir: Path = RAW_DIR) -> MarketData:
    notes: list[str] = []
    prepared = {t: prepare(load_raw(raw_dir, t), t, notes) for t in tickers_needed(params)}
    series, splices = {}, []
    for name, spec in params.sleeves.items():
        series[name], info = splice(prepared[spec["etf"]], prepared[spec["proxy"]], name)
        splices.append(info)
    start = max(frame.index[0] for frame in series.values())
    weights = {k: float(v) for k, v in params.vt_proxy["blend"].items()}
    vt_blend = blend({k: prepared[k] for k in weights}, weights, start)
    series["VT"], info = splice(prepared[params.vt_proxy["etf"]], vt_blend, "VT")
    splices.append(info)
    dates, frames = align(series)
    rf = risk_free(raw_dir, params.risk_free["series"], dates)
    return to_market(dates, frames, rf, "yahoo", notes, splices)


# ---------------------------------------------------------------- synthetic (tests only)


def synthetic_market(
    n_days: int,
    seed: int,
    names: tuple[str, ...] = ("VTI", "VXUS", "QQQM", "AVUV", "VT", "AVUV_DFSVX"),
    start: date = date(2000, 1, 3),
) -> MarketData:
    """Correlated random-walk prices with quarterly distributions. FAKE DATA:
    for exercising the pipeline in tests, never for reported results."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_days)
    market = rng.normal(0.0003, 0.011, n_days)
    frames = {}
    for i, name in enumerate(names):
        own = rng.normal(0.0001 * i, 0.006 + 0.002 * i, n_days)
        returns = 0.9 * market + own
        returns[0] = 0.0
        close = 50.0 * (i + 1) * np.cumprod(1.0 + returns)
        dist = np.zeros(n_days)
        dist[63::63] = 0.004 * close[62:-1:63][: len(dist[63::63])]
        frames[name] = pd.DataFrame(
            {"close": close, "open": close * (1 + rng.normal(0, 0.002, n_days)), "dist": dist}, index=dates
        )
    rf = np.full(n_days, 0.02 / 252)
    rf[0] = 0.0
    return to_market(dates, frames, rf, "synthetic", ["SYNTHETIC DATA - not market history"], [])

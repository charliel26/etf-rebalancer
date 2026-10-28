"""Typed view of backtest/config.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from rebalancer.money import to_decimal

CONFIG_PATH = Path(__file__).with_name("config.yaml")


@dataclass(frozen=True)
class TaxRates:
    short_term: Decimal
    long_term: Decimal
    dividends: Decimal
    interest: Decimal
    pay_month: int
    pay_day: int


@dataclass(frozen=True)
class ContributionModel:
    probability: float
    median: float
    log_sigma: float
    minimum: float
    maximum: float


@dataclass(frozen=True)
class CrisisWindow:
    name: str
    start: date
    end: date


@dataclass(frozen=True)
class BacktestParams:
    initial_capital: Decimal
    tranches: int
    contributions: ContributionModel
    headline_seed: int
    robustness_seeds: tuple[int, ...]
    cost_rate: Decimal
    dividend_lag: int
    min_cash_to_deploy: Decimal
    min_order: Decimal
    warmup_days: int
    st_cap_fraction: Decimal
    taxes: TaxRates
    reset_month: int
    reset_day: int
    lot_method: str
    band_widths: tuple[Decimal, ...]
    headline_band: Decimal
    blocks: int
    crisis_windows: tuple[CrisisWindow, ...]
    ema_span: int
    horizons: tuple[int, ...]
    sleeves: dict[str, dict[str, str]]
    vt_proxy: dict[str, Any]
    risk_free: dict[str, str]


def load_params(path: Path = CONFIG_PATH) -> BacktestParams:
    raw = yaml.safe_load(path.read_text())
    c, t = raw["contributions"], raw["taxes"]
    return BacktestParams(
        initial_capital=to_decimal(raw["initial_capital_usd"]),
        tranches=int(raw["tranches"]),
        contributions=ContributionModel(
            probability=float(c["probability_per_trading_day"]),
            median=float(c["median_usd"]),
            log_sigma=float(c["log_sigma"]),
            minimum=float(c["min_usd"]),
            maximum=float(c["max_usd"]),
        ),
        headline_seed=int(raw["seeds"]["headline"]),
        robustness_seeds=tuple(int(s) for s in raw["seeds"]["robustness"]),
        cost_rate=to_decimal(raw["costs_bps"]) / Decimal(10000),
        dividend_lag=int(raw["dividend_lag_trading_days"]),
        min_cash_to_deploy=to_decimal(raw["min_cash_to_deploy_usd"]),
        min_order=to_decimal(raw["min_order_usd"]),
        warmup_days=int(raw["warmup_trading_days"]),
        st_cap_fraction=to_decimal(raw["st_cap_fraction_of_value"]),
        taxes=TaxRates(
            short_term=to_decimal(t["short_term"]),
            long_term=to_decimal(t["long_term"]),
            dividends=to_decimal(t["dividends"]),
            interest=to_decimal(t["interest"]),
            pay_month=int(t["pay_month"]),
            pay_day=int(t["pay_day"]),
        ),
        reset_month=int(raw["annual_reset"]["month"]),
        reset_day=int(raw["annual_reset"]["day"]),
        lot_method=raw["lot_method"],
        band_widths=tuple(to_decimal(b) for b in raw["band_widths"]),
        headline_band=to_decimal(raw["headline_band_width"]),
        blocks=int(raw["walk_forward_blocks"]),
        crisis_windows=tuple(
            CrisisWindow(w["name"], date.fromisoformat(w["start"]), date.fromisoformat(w["end"]))
            for w in raw["crisis_windows"]
        ),
        ema_span=int(raw["signal"]["ema_span"]),
        horizons=tuple(int(h) for h in raw["signal"]["horizons"]),
        sleeves=raw["sleeves"],
        vt_proxy=raw["vt_proxy"],
        risk_free=raw["risk_free"],
    )

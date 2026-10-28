"""Load and validate config/targets.yaml and config/limits.yaml.

Validation is strict: an unknown key, a missing key or a bad value raises
ConfigError, and the run stops. Amounts are arbitrary non-negative numbers;
nothing assumes a particular account size.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .money import to_decimal

LOT_METHODS = ("fifo", "tax_aware")


class ConfigError(ValueError):
    """The configuration is invalid; nothing should run."""


@dataclass(frozen=True)
class DeploymentSchedule:
    """Phase a lump sum in over `tranches` monthly dates starting at `start_date`."""

    start_date: date | None
    total_usd: Decimal | None
    tranches: int

    @property
    def enabled(self) -> bool:
        return self.start_date is not None and self.total_usd is not None and self.total_usd > 0


@dataclass(frozen=True)
class RiskConfig:
    ewma_lambda: Decimal
    vol_window_days: int
    drawdown_lookback_days: int
    vol_flag: Decimal
    drawdown_flag: Decimal
    satellite_cap: Decimal


@dataclass(frozen=True)
class StrategyConfig:
    targets: Mapping[str, Decimal]
    band_width: Decimal
    satellite_symbols: frozenset[str]
    identical_groups: tuple[frozenset[str], ...]
    min_cash_to_deploy: Decimal
    cash_reserve: Decimal
    deployment: DeploymentSchedule
    annual_reset_month: int
    annual_reset_day: int
    lot_method: str
    risk: RiskConfig


@dataclass(frozen=True)
class Limits:
    min_order: Decimal
    max_order: Decimal
    max_daily_notional: Decimal
    max_st_gain: Decimal
    match_tolerance: Decimal
    quote_max_age: timedelta
    plan_max_age: timedelta
    review_max_age: timedelta
    require_review: bool
    window_start: time
    window_end: time
    timezone: str
    close_buffer: timedelta
    agentic_account_id: str | None
    min_holding_days: int
    approval_for_all_trades: bool


@dataclass(frozen=True)
class Config:
    strategy: StrategyConfig
    limits: Limits


# ---------------------------------------------------------------- helpers


def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where}: expected a mapping, got {type(value).__name__}")
    return value


def _check_keys(section: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(section) - allowed
    missing = allowed - set(section)
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")
    if missing:
        raise ConfigError(f"{where}: missing keys {sorted(missing)}")


def _decimal(value: Any, where: str, *, minimum: Decimal | None = Decimal(0)) -> Decimal:
    try:
        result = to_decimal(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: {exc}") from exc
    if minimum is not None and result < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {result}")
    return result


def _int(value: Any, where: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected an integer, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {value}")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false, got {value!r}")
    return value


def _symbol(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip().upper():
        raise ConfigError(f"{where}: symbols must be non-empty upper-case strings, got {value!r}")
    return value


def _clock(value: Any, where: str) -> time:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected 'HH:MM', got {value!r}")
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{where}: expected 'HH:MM', got {value!r}") from exc


def _optional_date(value: Any, where: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ConfigError(f"{where}: expected YYYY-MM-DD, got {value!r}") from exc
    raise ConfigError(f"{where}: expected YYYY-MM-DD, got {value!r}")


# ---------------------------------------------------------------- parsing


def parse_strategy(raw: Any) -> StrategyConfig:
    doc = _require_mapping(raw, "targets.yaml")
    _check_keys(
        doc,
        {
            "targets",
            "band_width",
            "satellite_symbols",
            "substantially_identical",
            "cash",
            "initial_deployment",
            "annual_reset",
            "lot_method",
            "risk",
        },
        "targets.yaml",
    )

    raw_targets = _require_mapping(doc["targets"], "targets")
    if not raw_targets:
        raise ConfigError("targets: at least one fund is required")
    targets: dict[str, Decimal] = {}
    for symbol, weight in raw_targets.items():
        sym = _symbol(symbol, "targets")
        w = _decimal(weight, f"targets.{sym}")
        if not Decimal(0) < w < Decimal(1) and not (w == 1 and len(raw_targets) == 1):
            raise ConfigError(f"targets.{sym}: weight must be in (0, 1], got {w}")
        targets[sym] = w
    if sum(targets.values(), Decimal(0)) != 1:
        raise ConfigError(f"targets: weights must sum to exactly 1, got {sum(targets.values())}")
    ordered_targets = dict(sorted(targets.items()))

    band_width = _decimal(doc["band_width"], "band_width")
    if not Decimal(0) < band_width < Decimal(1):
        raise ConfigError(f"band_width: must be in (0, 1), got {band_width}")

    satellites_raw = doc["satellite_symbols"]
    if satellites_raw is None:
        satellites_raw = []
    if not isinstance(satellites_raw, list):
        raise ConfigError("satellite_symbols: expected a list")
    satellites = frozenset(_symbol(s, "satellite_symbols") for s in satellites_raw)
    if satellites & set(targets):
        raise ConfigError(f"satellite_symbols overlap the core: {sorted(satellites & set(targets))}")

    groups_raw = doc["substantially_identical"] or []
    if not isinstance(groups_raw, list):
        raise ConfigError("substantially_identical: expected a list of lists")
    groups: list[frozenset[str]] = []
    seen: set[str] = set()
    for group in groups_raw:
        if not isinstance(group, list) or not group:
            raise ConfigError("substantially_identical: each group must be a non-empty list")
        members = frozenset(_symbol(s, "substantially_identical") for s in group)
        if members & seen:
            raise ConfigError(f"substantially_identical: {sorted(members & seen)} in two groups")
        seen |= members
        groups.append(members)

    cash = _require_mapping(doc["cash"], "cash")
    _check_keys(cash, {"min_cash_to_deploy_usd", "cash_reserve_usd"}, "cash")

    dep = _require_mapping(doc["initial_deployment"], "initial_deployment")
    _check_keys(dep, {"start_date", "total_usd", "tranches"}, "initial_deployment")
    total = None if dep["total_usd"] is None else _decimal(dep["total_usd"], "initial_deployment.total_usd")
    deployment = DeploymentSchedule(
        start_date=_optional_date(dep["start_date"], "initial_deployment.start_date"),
        total_usd=total,
        tranches=_int(dep["tranches"], "initial_deployment.tranches", minimum=1),
    )

    reset = _require_mapping(doc["annual_reset"], "annual_reset")
    _check_keys(reset, {"month", "day"}, "annual_reset")
    month = _int(reset["month"], "annual_reset.month", minimum=1)
    day = _int(reset["day"], "annual_reset.day", minimum=1)
    try:
        date(2001, month, day)  # a non-leap year: 29 Feb would never trigger
    except ValueError as exc:
        raise ConfigError(f"annual_reset: invalid month/day {month}/{day}") from exc

    lot_method = doc["lot_method"]
    if lot_method not in LOT_METHODS:
        raise ConfigError(f"lot_method: must be one of {LOT_METHODS}, got {lot_method!r}")

    risk = _require_mapping(doc["risk"], "risk")
    _check_keys(
        risk,
        {
            "ewma_lambda",
            "vol_window_days",
            "drawdown_lookback_days",
            "vol_flag",
            "drawdown_flag",
            "satellite_cap",
        },
        "risk",
    )
    lam = _decimal(risk["ewma_lambda"], "risk.ewma_lambda")
    if not Decimal(0) < lam < Decimal(1):
        raise ConfigError("risk.ewma_lambda: must be in (0, 1)")

    return StrategyConfig(
        targets=ordered_targets,
        band_width=band_width,
        satellite_symbols=satellites,
        identical_groups=tuple(groups),
        min_cash_to_deploy=_decimal(cash["min_cash_to_deploy_usd"], "cash.min_cash_to_deploy_usd"),
        cash_reserve=_decimal(cash["cash_reserve_usd"], "cash.cash_reserve_usd"),
        deployment=deployment,
        annual_reset_month=month,
        annual_reset_day=day,
        lot_method=lot_method,
        risk=RiskConfig(
            ewma_lambda=lam,
            vol_window_days=_int(risk["vol_window_days"], "risk.vol_window_days", minimum=2),
            drawdown_lookback_days=_int(
                risk["drawdown_lookback_days"], "risk.drawdown_lookback_days", minimum=2
            ),
            vol_flag=_decimal(risk["vol_flag"], "risk.vol_flag"),
            drawdown_flag=_decimal(risk["drawdown_flag"], "risk.drawdown_flag"),
            satellite_cap=_decimal(risk["satellite_cap"], "risk.satellite_cap"),
        ),
    )


def parse_limits(raw: Any) -> Limits:
    doc = _require_mapping(raw, "limits.yaml")
    _check_keys(
        doc,
        {
            "min_order_usd",
            "max_order_usd",
            "max_daily_notional_usd",
            "max_st_gain_per_rebalance_usd",
            "match_tolerance_usd",
            "quote_max_age_minutes",
            "plan_max_age_minutes",
            "review_max_age_minutes",
            "require_review",
            "execution_window",
            "agentic_account_id",
            "compliance",
        },
        "limits.yaml",
    )
    min_order = _decimal(doc["min_order_usd"], "min_order_usd", minimum=Decimal("0.01"))
    max_order = _decimal(doc["max_order_usd"], "max_order_usd", minimum=Decimal("0.01"))
    daily = _decimal(doc["max_daily_notional_usd"], "max_daily_notional_usd", minimum=Decimal("0.01"))
    if max_order < min_order:
        raise ConfigError("max_order_usd must be >= min_order_usd")
    if daily < max_order:
        raise ConfigError("max_daily_notional_usd must be >= max_order_usd")

    window = _require_mapping(doc["execution_window"], "execution_window")
    _check_keys(window, {"start", "end", "timezone", "close_buffer_minutes"}, "execution_window")
    start = _clock(window["start"], "execution_window.start")
    end = _clock(window["end"], "execution_window.end")
    if not start < end:
        raise ConfigError("execution_window: start must be before end")
    if window["timezone"] != "America/New_York":
        raise ConfigError("execution_window.timezone: only America/New_York is supported")

    compliance = _require_mapping(doc["compliance"], "compliance")
    _check_keys(compliance, {"min_holding_days", "approval_for_all_trades"}, "compliance")

    account = doc["agentic_account_id"]
    if account is not None and (not isinstance(account, str) or not account.strip()):
        raise ConfigError("agentic_account_id: expected null or a non-empty string")

    return Limits(
        min_order=min_order,
        max_order=max_order,
        max_daily_notional=daily,
        max_st_gain=_decimal(doc["max_st_gain_per_rebalance_usd"], "max_st_gain_per_rebalance_usd"),
        match_tolerance=_decimal(doc["match_tolerance_usd"], "match_tolerance_usd"),
        quote_max_age=timedelta(minutes=_int(doc["quote_max_age_minutes"], "quote_max_age_minutes", minimum=1)),
        plan_max_age=timedelta(minutes=_int(doc["plan_max_age_minutes"], "plan_max_age_minutes", minimum=1)),
        review_max_age=timedelta(
            minutes=_int(doc["review_max_age_minutes"], "review_max_age_minutes", minimum=1)
        ),
        require_review=_bool(doc["require_review"], "require_review"),
        window_start=start,
        window_end=end,
        timezone=window["timezone"],
        close_buffer=timedelta(
            minutes=_int(window["close_buffer_minutes"], "execution_window.close_buffer_minutes")
        ),
        agentic_account_id=account.strip() if isinstance(account, str) else None,
        min_holding_days=_int(compliance["min_holding_days"], "compliance.min_holding_days"),
        approval_for_all_trades=_bool(
            compliance["approval_for_all_trades"], "compliance.approval_for_all_trades"
        ),
    )


def load_yaml(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc


def load_config(config_dir: Path) -> Config:
    return Config(
        strategy=parse_strategy(load_yaml(config_dir / "targets.yaml")),
        limits=parse_limits(load_yaml(config_dir / "limits.yaml")),
    )

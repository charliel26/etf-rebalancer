import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from rebalancer.config import ConfigError, load_config, load_yaml, parse_limits, parse_strategy
from tests.conftest import REPO_ROOT, TARGETS, D, raw_limits, raw_strategy


def strategy_with(**changes):
    raw = raw_strategy()
    raw.update(changes)
    return raw


def limits_with(**changes):
    raw = raw_limits()
    raw.update(changes)
    return raw


def test_repo_config_loads():
    config = load_config(REPO_ROOT / "config")
    assert config.strategy.targets == TARGETS
    assert list(config.strategy.targets) == sorted(TARGETS)  # deterministic order
    assert config.strategy.band_width == D("0.20")
    assert config.strategy.deployment.enabled is False
    assert config.strategy.lot_method == "fifo"
    assert config.limits.max_order == D(1500)
    assert config.limits.max_daily_notional == D(3000)
    assert config.limits.window_start == dt.time(9, 35)


class TestArbitraryAmounts:
    @pytest.mark.parametrize("total", ["0.01", "250", "8100", "1234567.89", 5000, 9000.5])
    def test_any_deployment_amount(self, total):
        raw = strategy_with(initial_deployment={"start_date": "2026-11-02", "total_usd": total, "tranches": 3})
        schedule = parse_strategy(raw).deployment
        assert schedule.enabled
        assert schedule.total_usd == Decimal(str(total))

    def test_deployment_disabled_by_nulls_or_zero(self):
        for start, total in [(None, "100"), ("2026-11-02", None), ("2026-11-02", "0")]:
            raw = strategy_with(initial_deployment={"start_date": start, "total_usd": total, "tranches": 3})
            assert parse_strategy(raw).deployment.enabled is False

    def test_any_cash_reserve(self):
        raw = strategy_with(cash={"min_cash_to_deploy_usd": "25", "cash_reserve_usd": "4000.50"})
        assert parse_strategy(raw).cash_reserve == D("4000.50")

    def test_date_object_accepted(self):
        raw = strategy_with(initial_deployment={"start_date": dt.date(2026, 11, 2), "total_usd": "1", "tranches": 1})
        assert parse_strategy(raw).deployment.start_date == dt.date(2026, 11, 2)

    def test_single_fund_target_allowed(self):
        raw = strategy_with(targets={"VT": "1"}, substantially_identical=[])
        assert parse_strategy(raw).targets == {"VT": Decimal(1)}


@pytest.mark.parametrize(
    "changes",
    [
        {"targets": {"VTI": "0.6", "VXUS": "0.3"}},
        {"targets": {"VTI": "1.0", "VXUS": "0"}},
        {"targets": {}},
        {"targets": ["VTI"]},
        {"targets": {"vti": "1"}},
        {"targets": {"VTI": "abc"}},
        {"band_width": "0"},
        {"band_width": "1"},
        {"satellite_symbols": "TSLA"},
        {"satellite_symbols": ["VTI"]},
        {"substantially_identical": "VTI"},
        {"substantially_identical": [[]]},
        {"substantially_identical": [["VTI", "QQQ"], ["QQQ"]]},
        {"cash": {"min_cash_to_deploy_usd": "25"}},
        {"cash": {"min_cash_to_deploy_usd": "25", "cash_reserve_usd": "-1"}},
        {"initial_deployment": {"start_date": "2026-13-01", "total_usd": "1", "tranches": 3}},
        {"initial_deployment": {"start_date": 20261101, "total_usd": "1", "tranches": 3}},
        {"initial_deployment": {"start_date": dt.datetime(2026, 11, 2, 9), "total_usd": "1", "tranches": 3}},
        {"initial_deployment": {"start_date": None, "total_usd": "1", "tranches": 0}},
        {"initial_deployment": {"start_date": None, "total_usd": "1", "tranches": True}},
        {"annual_reset": {"month": 2, "day": 29}},
        {"annual_reset": {"month": 13, "day": 1}},
        {"lot_method": "hifo"},
        {"risk": {"ewma_lambda": "1"}},
        {"unexpected": 1},
    ],
)
def test_strategy_validation_errors(changes):
    raw = strategy_with(**changes)
    if "risk" in changes:
        raw["risk"] = {**raw_strategy()["risk"], **changes["risk"]}
    with pytest.raises(ConfigError):
        parse_strategy(raw)


def test_strategy_missing_key():
    raw = raw_strategy()
    del raw["band_width"]
    with pytest.raises(ConfigError, match="missing"):
        parse_strategy(raw)


def test_strategy_not_a_mapping():
    with pytest.raises(ConfigError):
        parse_strategy(["targets"])


def test_empty_satellites_and_groups_as_null():
    raw = strategy_with(satellite_symbols=None, substantially_identical=None)
    parsed = parse_strategy(raw)
    assert parsed.satellite_symbols == frozenset()
    assert parsed.identical_groups == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"min_order_usd": "0"},
        {"max_order_usd": "0.5", "min_order_usd": "1"},
        {"max_daily_notional_usd": "100"},
        {
            "execution_window": {
                "start": "15:55",
                "end": "09:35",
                "timezone": "America/New_York",
                "close_buffer_minutes": 5,
            }
        },
        {
            "execution_window": {
                "start": "9h",
                "end": "15:55",
                "timezone": "America/New_York",
                "close_buffer_minutes": 5,
            }
        },
        {"execution_window": {"start": 935, "end": "15:55", "timezone": "America/New_York", "close_buffer_minutes": 5}},
        {"execution_window": {"start": "09:35", "end": "15:55", "timezone": "UTC", "close_buffer_minutes": 5}},
        {"require_review": "yes"},
        {"agentic_account_id": ""},
        {"agentic_account_id": 123},
        {"quote_max_age_minutes": 0},
        {"compliance": {"min_holding_days": -1, "approval_for_all_trades": False}},
    ],
)
def test_limits_validation_errors(changes):
    with pytest.raises(ConfigError):
        parse_limits(limits_with(**changes))


def test_limits_account_id_is_stripped():
    assert parse_limits(limits_with(agentic_account_id=" 5QR ")).agentic_account_id == "5QR"


def test_load_yaml_errors(tmp_path: Path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_yaml(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("targets: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_yaml(bad)

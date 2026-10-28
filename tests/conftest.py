"""Shared fixtures and helpers."""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from rebalancer.config import Config, parse_limits, parse_strategy

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGETS = {"AVUV": Decimal("0.15"), "QQQM": Decimal("0.15"), "VTI": Decimal("0.50"), "VXUS": Decimal("0.20")}


def D(value: object) -> Decimal:
    return Decimal(str(value))


def raw_strategy() -> dict:
    with (REPO_ROOT / "config" / "targets.yaml").open() as handle:
        return yaml.safe_load(handle)


def raw_limits() -> dict:
    with (REPO_ROOT / "config" / "limits.yaml").open() as handle:
        return yaml.safe_load(handle)


def make_config(strategy_overrides: dict | None = None, limit_overrides: dict | None = None) -> Config:
    strategy = copy.deepcopy(raw_strategy())
    limits = copy.deepcopy(raw_limits())
    for key, value in (strategy_overrides or {}).items():
        strategy[key] = value
    for key, value in (limit_overrides or {}).items():
        limits[key] = value
    return Config(strategy=parse_strategy(strategy), limits=parse_limits(limits))


@pytest.fixture
def config() -> Config:
    return make_config()

"""The broker interface the planner and executors depend on."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from ..ledger import BrokerOrder
from ..models import Order, Snapshot
from .robinhood_raw import broker_orders, build_snapshot


class BrokerError(RuntimeError):
    pass


class Broker(ABC):
    @abstractmethod
    def snapshot(self, now: datetime) -> Snapshot:
        """Account, positions, quotes, lots, orders and tradability right now."""

    @abstractmethod
    def submit(self, order: Order, client_order_id: str) -> str:
        """Place a dollar-based market order; return the broker's order id."""

    @abstractmethod
    def orders(self) -> list[BrokerOrder]:
        """Recent orders, for reconciliation."""


class CapturedRobinhood(Broker):
    """Robinhood through the MCP: read-only from captured responses.

    Orders are placed by the Claude Code agent with the MCP tool and checked by
    the guard hook, never from Python.
    """

    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = raw_dir

    def snapshot(self, now: datetime) -> Snapshot:
        return build_snapshot(self.raw_dir, now)[0]

    def submit(self, order: Order, client_order_id: str) -> str:
        raise BrokerError("Robinhood orders are placed by the agent through the MCP, behind the guard hook")

    def orders(self) -> list[BrokerOrder]:
        return broker_orders(self.raw_dir)

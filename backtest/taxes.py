"""Annual US-style tax netting for the backtest (STRATEGY.md §13.2).

Per calendar year: net short-term S and long-term L (including carried-forward
losses of each character), then

    tax = r_st * max(0, S + min(0, L)) + r_lt * max(0, L + min(0, S))
          + r_div * dividends + r_int * interest

Net losses carry forward with their character. The 3,000 USD/yr offset
against ordinary income is ignored (conservative).
"""

from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from .params import TaxRates

ZERO = Decimal(0)


@dataclass
class YearActivity:
    st: Decimal = ZERO
    lt: Decimal = ZERO
    dividends: Decimal = ZERO
    interest: Decimal = ZERO


@dataclass
class TaxBook:
    rates: TaxRates
    years: dict[int, YearActivity] = field(default_factory=lambda: defaultdict(YearActivity))
    carry_st: Decimal = ZERO  # <= 0
    carry_lt: Decimal = ZERO  # <= 0
    settled: dict[int, Decimal] = field(default_factory=dict)

    def realize(self, year: int, st: Decimal, lt: Decimal) -> None:
        self.years[year].st += st
        self.years[year].lt += lt

    def dividend(self, year: int, amount: Decimal) -> None:
        self.years[year].dividends += amount

    def interest(self, year: int, amount: Decimal) -> None:
        self.years[year].interest += amount

    def settle(self, year: int) -> Decimal:
        """Tax due for `year`; updates the carry-forwards. Each year settles once."""
        if year in self.settled:
            raise ValueError(f"tax year {year} already settled")
        activity = self.years[year]
        s = activity.st + self.carry_st
        lt = activity.lt + self.carry_lt
        taxable_st = max(ZERO, s + min(ZERO, lt))
        taxable_lt = max(ZERO, lt + min(ZERO, s))
        if s < 0 and lt < 0:
            self.carry_st, self.carry_lt = s, lt
        elif s + lt < 0:
            # One side positive, the other more negative: the remaining loss keeps its character.
            self.carry_st, self.carry_lt = (s + lt, ZERO) if s < 0 else (ZERO, s + lt)
        else:
            self.carry_st = self.carry_lt = ZERO
        r = self.rates
        tax = (
            r.short_term * taxable_st
            + r.long_term * taxable_lt
            + r.dividends * activity.dividends
            + r.interest * activity.interest
        )
        self.settled[year] = tax
        return tax

    def unsettled_years(self, through: int) -> list[int]:
        return sorted(y for y in self.years if y <= through and y not in self.settled)

    def preview_liquidation(self, final_year: int, extra_st: Decimal, extra_lt: Decimal) -> Decimal:
        """Tax still owed if everything were sold now: all unsettled years
        through `final_year`, with the liquidation gains added to `final_year`."""
        trial = copy.deepcopy(self)
        trial.realize(final_year, extra_st, extra_lt)
        return sum((trial.settle(y) for y in trial.unsettled_years(final_year)), ZERO)

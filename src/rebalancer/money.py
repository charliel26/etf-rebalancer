"""Exact money arithmetic.

All amounts are `decimal.Decimal`. Order amounts are whole cents. Nothing here
does I/O or reads the clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_DOWN, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")


def to_decimal(value: object) -> Decimal:
    """Convert an int, str, float or Decimal to a finite Decimal.

    Floats go through their shortest repr, so 0.15 becomes Decimal("0.15"),
    not the binary expansion. Booleans and non-finite values are rejected.
    """
    if isinstance(value, bool):
        raise TypeError("booleans are not amounts")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except ArithmeticError as exc:
            raise ValueError(f"not a number: {value!r}") from exc
    else:
        raise TypeError(f"cannot convert {type(value).__name__} to Decimal")
    if not result.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return result


def floor_cents(amount: Decimal) -> Decimal:
    """Round a non-negative amount down to whole cents."""
    if amount < 0:
        raise ValueError(f"floor_cents needs a non-negative amount, got {amount}")
    return amount.quantize(CENT, rounding=ROUND_DOWN)


def is_whole_cents(amount: Decimal) -> bool:
    return amount == amount.quantize(CENT)


def allocate_cents(weights: Mapping[str, Decimal], total: Decimal) -> dict[str, Decimal]:
    """Split `total` (whole cents) in proportion to non-negative `weights`.

    Largest-remainder method: every share is first rounded down to a cent, then
    the leftover cents go one each to the largest fractional remainders. Ties go
    to the larger weight, then to the alphabetically first key. The result sums
    to exactly `total`, and each share is within one cent of its exact value.
    """
    if total < 0 or not is_whole_cents(total):
        raise ValueError(f"total must be a non-negative whole number of cents, got {total}")
    if any(w < 0 for w in weights.values()):
        raise ValueError("weights must be non-negative")
    weight_sum = sum(weights.values(), ZERO)
    if total == 0:
        return {key: ZERO for key in weights}
    if weight_sum == 0:
        raise ValueError("cannot allocate a positive total over zero weights")

    total_cents = int(total / CENT)
    exact = {key: Decimal(total_cents) * w / weight_sum for key, w in weights.items()}
    floors = {key: int(value) for key, value in exact.items()}  # int() truncates; values >= 0
    leftover = total_cents - sum(floors.values())
    by_remainder = sorted(
        weights,
        key=lambda key: (-(exact[key] - floors[key]), -weights[key], key),
    )
    for key in by_remainder[:leftover]:
        floors[key] += 1
    return {key: Decimal(cents) * CENT for key, cents in floors.items()}

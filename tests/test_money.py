from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rebalancer.money import CENT, allocate_cents, floor_cents, is_whole_cents, to_decimal
from tests.conftest import D


class TestToDecimal:
    def test_accepts_int_str_decimal(self):
        assert to_decimal(3) == Decimal(3)
        assert to_decimal(" 1.25 ") == D("1.25")
        assert to_decimal(D("2.5")) == D("2.5")

    def test_float_uses_shortest_repr(self):
        assert to_decimal(0.15) == D("0.15")
        assert str(to_decimal(0.1)) == "0.1"

    @pytest.mark.parametrize("bad", [True, False, None, [1], object()])
    def test_rejects_non_numbers(self, bad):
        with pytest.raises(TypeError):
            to_decimal(bad)

    @pytest.mark.parametrize("bad", ["abc", "", "NaN", "inf", float("nan"), float("inf")])
    def test_rejects_non_finite(self, bad):
        with pytest.raises(ValueError):
            to_decimal(bad)


class TestFloorCents:
    def test_rounds_down(self):
        assert floor_cents(D("1.239")) == D("1.23")
        assert floor_cents(D("1.2")) == D("1.20")
        assert floor_cents(D("0")) == D("0.00")

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            floor_cents(D("-0.01"))

    def test_is_whole_cents(self):
        assert is_whole_cents(D("1.20"))
        assert not is_whole_cents(D("1.201"))


class TestAllocateCents:
    def test_exact_sum_and_largest_remainder(self):
        result = allocate_cents({"A": D(1), "B": D(1), "C": D(1)}, D("1.00"))
        assert sum(result.values()) == D("1.00")
        # 33.33... cents each; the leftover cent goes to the alphabetically first key.
        assert result == {"A": D("0.34"), "B": D("0.33"), "C": D("0.33")}

    def test_tie_prefers_larger_weight(self):
        # Exact shares: A 0.333.. -> 0.33 r .333, B 0.666.. -> 0.66 r .666 => B gets the cent.
        result = allocate_cents({"A": D(1), "B": D(2)}, D("0.99"))
        assert result == {"A": D("0.33"), "B": D("0.66")}
        result = allocate_cents({"A": D(1), "B": D(2)}, D("1.00"))
        assert result == {"A": D("0.33"), "B": D("0.67")}

    def test_zero_total(self):
        assert allocate_cents({"A": D(0), "B": D(5)}, D("0")) == {"A": D(0), "B": D(0)}

    def test_zero_weight_never_receives_cents(self):
        result = allocate_cents({"A": D(0), "B": D(3), "C": D(3)}, D("0.01"))
        assert result["A"] == 0
        assert sum(result.values()) == D("0.01")

    @pytest.mark.parametrize(
        ("weights", "total"),
        [
            ({"A": D(1)}, D("-0.01")),
            ({"A": D(1)}, D("0.001")),
            ({"A": D(-1), "B": D(2)}, D("1")),
            ({"A": D(0)}, D("1")),
        ],
    )
    def test_rejects_bad_input(self, weights, total):
        with pytest.raises(ValueError):
            allocate_cents(weights, total)

    @given(
        weights=st.dictionaries(
            st.sampled_from(["VTI", "VXUS", "QQQM", "AVUV", "X"]),
            st.decimals(min_value=0, max_value=10**7, places=6, allow_nan=False, allow_infinity=False),
            min_size=1,
        ),
        cents=st.integers(min_value=0, max_value=10**9),
    )
    def test_properties(self, weights, cents):
        total = Decimal(cents) * CENT
        if sum(weights.values()) == 0:
            if cents == 0:
                assert all(v == 0 for v in allocate_cents(weights, total).values())
            else:
                with pytest.raises(ValueError):
                    allocate_cents(weights, total)
            return
        result = allocate_cents(weights, total)
        assert sum(result.values()) == total
        weight_sum = sum(weights.values())
        for key, share in result.items():
            assert is_whole_cents(share)
            assert share >= 0
            exact = total * weights[key] / weight_sum
            assert abs(share - exact) < CENT
            if weights[key] == 0:
                assert share == 0

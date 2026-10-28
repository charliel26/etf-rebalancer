"""Backtester tests: hand-worked cases for each piece, then end-to-end invariants
on synthetic data. Synthetic numbers are never results; they exercise the code."""

from __future__ import annotations

import dataclasses
import math
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from backtest import compare, metrics
from backtest.contributions import contribution_stream
from backtest.data import (
    DataUnavailable,
    MarketData,
    align,
    blend,
    load_raw,
    prepare,
    risk_free,
    splice,
    switch_date,
    synthetic_market,
    to_market,
)
from backtest.params import load_params
from backtest.signal_study import below_ema, ema, forward_returns, study
from backtest.simulate import Simulation, Strategy, contributions_for_window, simulate
from backtest.taxes import TaxBook
from tests.conftest import TARGETS, D

PARAMS = load_params()


# ---------------------------------------------------------------- contributions


def test_contribution_stream_is_seeded_and_bounded():
    a = contribution_stream(3000, PARAMS.contributions, 0)
    b = contribution_stream(3000, PARAMS.contributions, 0)
    prefix = contribution_stream(1000, PARAMS.contributions, 0)
    assert a == b
    assert {k: v for k, v in a.items() if k < 1000} == prefix
    assert a != contribution_stream(3000, PARAMS.contributions, 1)
    assert 0 not in a
    assert all(Decimal(50) <= v <= Decimal(5000) and v == v.to_integral_value() for v in a.values())
    assert 100 < len(a) < 190  # about 3000/21 = 143


# ---------------------------------------------------------------- taxes


class TestTaxes:
    def book(self):
        return TaxBook(PARAMS.taxes)

    def test_st_gain_net_of_lt_loss(self):
        book = self.book()
        book.realize(2020, D(100), D(-30))
        assert book.settle(2020) == D("0.22") * 70
        assert (book.carry_st, book.carry_lt) == (0, 0)

    def test_lt_gain_net_of_st_loss(self):
        book = self.book()
        book.realize(2020, D(-50), D(200))
        assert book.settle(2020) == D("0.15") * 150

    def test_carry_forward_keeps_character(self):
        book = self.book()
        book.realize(2020, D(-50), D(20))
        assert book.settle(2020) == 0
        assert (book.carry_st, book.carry_lt) == (D(-30), 0)
        book.realize(2021, D(40), D(0))
        assert book.settle(2021) == D("0.22") * 10
        book.realize(2022, D(10), D(-50))
        assert book.settle(2022) == 0
        assert (book.carry_st, book.carry_lt) == (0, D(-40))
        book.realize(2023, D(-5), D(-5))
        assert book.settle(2023) == 0
        assert (book.carry_st, book.carry_lt) == (D(-5), D(-45))

    def test_dividends_and_interest(self):
        book = self.book()
        book.dividend(2020, D(100))
        book.interest(2020, D(10))
        assert book.settle(2020) == D(15) + D("2.2")

    def test_settle_once_and_preview_does_not_mutate(self):
        book = self.book()
        book.realize(2020, D(10), D(0))
        book.settle(2020)
        with pytest.raises(ValueError):
            book.settle(2020)
        book.realize(2021, D(0), D(100))
        assert book.preview_liquidation(2021, D(0), D(100)) == D("0.15") * 200
        assert book.unsettled_years(2021) == [2021]


# ---------------------------------------------------------------- metrics


class TestMetrics:
    def test_twr_removes_flows(self):
        # Day 0: deposit 100. Day 1: +10% and deposit 50 -> 160. Day 2: -50% -> 80.
        r = metrics.twr_returns([100, 160, 80], [100, 50, 0])
        assert r == pytest.approx([0.0, 0.10, -0.5])
        assert metrics.twr_returns([0, 100], [0, 100]).tolist() == [0.0, 0.0]

    def test_cagr_vol_sharpe(self):
        r = np.array([0.0] + [0.001] * 252)
        expected = (1.001**252) ** (365.25 / 366) - 1  # 2020 is a leap year: 366 days
        assert metrics.cagr(r, date(2020, 1, 1), date(2021, 1, 1)) == pytest.approx(expected, rel=1e-12)
        assert metrics.annualized_vol(r) == pytest.approx(0.0, abs=1e-12)
        assert math.isnan(metrics.sharpe(r, np.zeros(len(r))))
        noisy = np.array([0.0, 0.01, -0.01, 0.02, -0.005])
        assert metrics.sharpe(noisy, np.zeros(5)) == pytest.approx(
            math.sqrt(252) * noisy[1:].mean() / noisy[1:].std(ddof=1)
        )
        assert math.isnan(metrics.cagr(r, date(2020, 1, 1), date(2020, 1, 1)))

    def test_drawdown_stats(self):
        dates = [date(2020, 1, d) for d in range(1, 8)]
        r = np.array([0.0, 0.10, -0.20, 0.05, 0.25, -0.10, 0.0])
        dd = metrics.drawdown_stats(r, dates)
        assert dd.max_drawdown == pytest.approx(-0.20)
        assert (dd.peak, dd.trough, dd.recovery) == (dates[1], dates[2], dates[4])
        assert dd.longest_underwater_days == 2 and dd.longest_unrecovered
        flat = metrics.drawdown_stats(np.zeros(3), dates[:3])
        assert flat.max_drawdown == 0 and flat.peak is None

    def test_xirr(self):
        flows = [(date(2020, 1, 1), -100.0), (date(2021, 1, 1), 110.0)]
        assert metrics.xirr(flows) == pytest.approx(1.1 ** (365.25 / 366) - 1, abs=1e-6)
        assert math.isnan(metrics.xirr([(date(2020, 1, 1), 100.0), (date(2021, 1, 1), 100.0)]))

    def test_cash_stats(self):
        avg, drag, days = metrics.cash_stats([50, 0, 0], [100, 100, 100], np.array([0.0, 0.02, 0.0]), np.zeros(3))
        assert avg == pytest.approx(50 / 300)
        assert drag == pytest.approx(252 * (0.5 * 0.02 + 0) / 2)
        assert days == 1

    def test_newey_west_matches_statsmodels(self):
        import statsmodels.api as sm

        rng = np.random.default_rng(3)
        x = (rng.random(400) < 0.3).astype(float)
        y = 0.001 * x + np.convolve(rng.normal(0, 0.01, 404), np.ones(5) / 5, mode="valid")
        beta, cov = metrics.newey_west_ols(y, x, 6)
        fit = sm.OLS(y, sm.add_constant(x)).fit(cov_type="HAC", cov_kwds={"maxlags": 6, "use_correction": False})
        assert beta == pytest.approx(fit.params)
        assert cov == pytest.approx(fit.cov_params(), rel=1e-9)

    def test_nw_lag_rule(self):
        assert metrics.nw_lag(1, 6900) == math.floor(4 * 69 ** (2 / 9))
        assert metrics.nw_lag(20, 6900) == 19


# ---------------------------------------------------------------- signal study


class TestSignals:
    def test_ema_by_hand(self):
        e = ema([10.0, 12.0, 9.0], 8)
        a = 2 / 9
        assert e[1] == pytest.approx(a * 12 + (1 - a) * 10)
        assert e[2] == pytest.approx(a * 9 + (1 - a) * e[1])
        assert below_ema([10.0, 12.0, 9.0], 8).tolist() == [False, False, True]

    def test_forward_returns(self):
        p = np.array([100.0, 110.0, 121.0, 133.1])
        assert forward_returns(p, 1, 0)[:3] == pytest.approx([0.1, 0.1, 0.1])
        assert np.isnan(forward_returns(p, 1, 0)[3])
        assert forward_returns(p, 2, 1)[0] == pytest.approx(0.21)
        assert np.isnan(forward_returns(p, 5, 0)).all()

    def test_study_rows(self):
        rng = np.random.default_rng(5)
        prices = 100 * np.cumprod(1 + rng.normal(0.0003, 0.01, 1500))
        rows = study("VTI", prices, below_ema(prices, 8), (1, 5, 20), 21)
        assert len(rows) == 6 and {r.start for r in rows} == {"signal close", "execution close"}
        for row in rows:
            assert 0.2 < row.fire_rate < 0.8
            assert row.mean_signal == pytest.approx(row.mean_other + row.difference)
            assert abs(row.t_difference) < 6  # a random walk has no strong signal


# ---------------------------------------------------------------- data


def frame(dates, close, dist=None, adj=None, opens=None):
    df = pd.DataFrame({"Close": close}, index=pd.DatetimeIndex(dates))
    df["Adj Close"] = adj if adj is not None else close
    df["Dividends"] = dist if dist is not None else 0.0
    df["Open"] = opens if opens is not None else close
    return df


class TestData:
    def test_prepare_prefers_reported_distributions_when_consistent(self):
        idx = pd.bdate_range("2020-01-01", periods=300)
        close = np.full(300, 100.0)
        dist = np.zeros(300)
        dist[100] = 1.0
        adj = np.ones(300) * 100.0
        adj[:100] = 99.0  # 1% dividend adjustment before the ex-date
        notes = []
        out = prepare(frame(idx, close, dist, adj), "T", notes)
        assert out["dist"].iloc[100] == pytest.approx(1.0) and notes == []

    def test_prepare_falls_back_to_implied_distributions(self):
        idx = pd.bdate_range("2020-01-01", periods=300)
        close = np.full(300, 100.0)
        adj = np.ones(300) * 100.0
        adj[:100] = 98.0  # a 2% distribution the dividend column does not report
        notes = []
        out = prepare(frame(idx, close, np.zeros(300), adj), "T", notes)
        assert out["dist"].iloc[100] == pytest.approx(100 * (1 - 98 / 100), rel=1e-6)
        assert "implied" in notes[0]

    def test_switch_and_splice(self):
        proxy_idx = pd.bdate_range("2019-01-01", "2019-03-29")
        etf_idx = pd.bdate_range("2019-02-12", "2019-03-29")
        proxy = pd.DataFrame(
            {
                "close": np.linspace(50, 60, len(proxy_idx)),
                "open": np.nan,
                "dist": np.where(proxy_idx == "2019-01-15", 0.5, 0.0),
            },
            index=proxy_idx,
        )
        etf = pd.DataFrame(
            {"close": np.linspace(200, 210, len(etf_idx)), "open": np.linspace(200, 210, len(etf_idx)), "dist": 0.0},
            index=etf_idx,
        )
        assert switch_date(etf) == pd.Timestamp("2019-03-01")
        spliced, info = splice(etf, proxy, "X")
        anchor = pd.Timestamp("2019-02-28")
        assert spliced.loc[anchor, "close"] == pytest.approx(etf.loc[anchor, "close"])
        scale = etf.loc[anchor, "close"] / proxy.loc[anchor, "close"]
        early = pd.Timestamp("2019-01-10")
        assert spliced.loc[early, "close"] == pytest.approx(proxy.loc[early, "close"] * scale)
        assert spliced.loc[pd.Timestamp("2019-01-15"), "dist"] == pytest.approx(0.5 * scale)
        assert np.isnan(spliced.loc[early, "open"])
        assert spliced.loc[pd.Timestamp("2019-03-05"), "close"] == etf.loc[pd.Timestamp("2019-03-05"), "close"]
        assert info["switch"] == "2019-03-01" and info["overlap_days"] == len(etf_idx)

    def test_splice_needs_proxy_history(self):
        etf = prepare(frame(pd.bdate_range("2019-02-12", periods=40), np.full(40, 10.0)), "E", [])
        proxy = prepare(frame(pd.bdate_range("2019-06-01", periods=40), np.full(40, 10.0)), "P", [])
        with pytest.raises(DataUnavailable):
            splice(etf, proxy, "X")

    def test_blend_holds_units(self):
        idx = pd.bdate_range("2020-01-01", periods=3)
        a = pd.DataFrame({"close": [100.0, 200.0, 200.0], "open": np.nan, "dist": [0.0, 0.0, 1.0]}, index=idx)
        b = pd.DataFrame({"close": [50.0, 50.0, 50.0], "open": np.nan, "dist": 0.0}, index=idx)
        out = blend({"A": a, "B": b}, {"A": 0.5, "B": 0.5}, idx[0])
        assert out["close"].tolist() == pytest.approx([100.0, 150.0, 150.0])
        assert out["dist"].iloc[2] == pytest.approx(0.5)

    def test_align_moves_distributions_forward(self):
        a = pd.DataFrame(
            {"close": [1.0, 1, 1, 1], "open": np.nan, "dist": [0, 0.3, 0, 0]},
            index=pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-06"]),
        )
        b = pd.DataFrame(
            {"close": [1.0, 1, 1], "open": np.nan, "dist": [0, 0, 0]},
            index=pd.to_datetime(["2020-01-01", "2020-01-03", "2020-01-06"]),
        )
        dates, frames = align({"A": a, "B": b})
        assert list(dates.strftime("%m-%d")) == ["01-01", "01-03", "01-06"]
        assert frames["A"]["dist"].tolist() == [0, 0.3, 0]

    def test_risk_free(self, tmp_path):
        (tmp_path / "DTB3.csv").write_text("observation_date,DTB3\n2020-01-02,3.6\n2020-01-03,.\n2020-01-06,3.6\n")
        dates = pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06"])
        rf = risk_free(tmp_path, "DTB3", dates)
        assert rf[0] == 0 and rf[1] == pytest.approx(0.036 / 360) and rf[2] == pytest.approx(0.036 * 3 / 360)
        with pytest.raises(DataUnavailable):
            risk_free(tmp_path, "MISSING", dates)
        with pytest.raises(DataUnavailable):
            load_raw(tmp_path, "VTI")

    def test_synthetic_is_deterministic_and_labelled(self):
        a, b = synthetic_market(300, 1), synthetic_market(300, 1)
        assert a.price["VTI"] == b.price["VTI"] and a.source == "synthetic"
        assert "SYNTHETIC" in a.notes[0]


# ---------------------------------------------------------------- simulation


def flat_market(n=300, price=100.0, dividends=None) -> MarketData:
    idx = pd.bdate_range("2021-01-04", periods=n)
    frames = {}
    for name in ("VTI", "VXUS", "QQQM", "AVUV", "VT"):
        dist = np.zeros(n)
        for day, amount in (dividends or {}).get(name, {}).items():
            dist[day] = amount
        frames[name] = pd.DataFrame({"close": np.full(n, price), "open": np.full(n, price), "dist": dist}, index=idx)
    return to_market(idx, frames, np.zeros(n), "synthetic", [], [])


CORE = Strategy("S3", TARGETS, band_width=D("0.2"), st_cap_fraction=PARAMS.st_cap_fraction)
NO_COST = dataclasses.replace(PARAMS, cost_rate=Decimal(0))


class TestSimulation:
    def test_flat_prices_no_costs_return_nothing(self):
        market = flat_market()
        result = simulate(market, CORE, NO_COST, {50: D(1000)}, 0, 299)
        r = metrics.twr_returns(result.values, result.flows)
        assert np.allclose(r, 0)
        assert result.after_tax_terminal == pytest.approx(10000)
        assert result.contributions == 10000

    def test_costs_are_charged(self):
        result = simulate(flat_market(), CORE, PARAMS, {}, 0, 299)
        bought = sum(float(t.notional) for t in result.trades)
        assert result.values[-1] == pytest.approx(9000 - bought * 0.0005 / 1.0005, rel=1e-6)

    def test_phased_deployment(self):
        result = simulate(flat_market(), CORE, NO_COST, {}, 0, 299)
        first_buys = [t for t in result.trades if t.day == result.dates[1]]
        assert sum(float(t.notional) for t in first_buys) == pytest.approx(3000)
        assert result.cash[1] == pytest.approx(6000)
        assert result.cash[-1] < 1  # all three tranches deployed by the end

    def test_single_asset_tracks_price(self):
        rng = np.random.default_rng(2)
        idx = pd.bdate_range("2021-01-04", periods=200)
        close = 100 * np.cumprod(1 + rng.normal(0, 0.01, 200))
        frames = {"VT": pd.DataFrame({"close": close, "open": close, "dist": 0.0}, index=idx)}
        market = to_market(idx, frames, np.zeros(200), "synthetic", [], [])
        params = dataclasses.replace(NO_COST, tranches=1)
        result = simulate(market, Strategy("S1", {"VT": Decimal(1)}), params, {}, 0, 199)
        r = metrics.twr_returns(result.values, result.flows)
        growth = np.prod(1 + r[2:])
        assert growth == pytest.approx(float(market.price["VT"][199] / market.price["VT"][1]), rel=1e-6)

    def test_dividends_paid_after_lag_and_taxed(self):
        market = flat_market(dividends={"VTI": {100: 1.0}})
        params = dataclasses.replace(NO_COST, tranches=1)
        result = simulate(market, Strategy("S2", TARGETS), params, {}, 0, 299)
        vti_shares = 4500 / 100
        assert result.dividends == pytest.approx(vti_shares * 1.0)
        assert result.cash[104] == pytest.approx(0, abs=1e-6)  # receivable not yet paid
        assert result.cash[105] == pytest.approx(45, abs=1e-6)

    def test_no_look_ahead(self):
        base = synthetic_market(500, 3)
        changed = synthetic_market(500, 3)
        k = 300
        for name in changed.price:
            changed.price[name] = changed.price[name][: k + 1] + [p * 2 for p in changed.price[name][k + 1 :]]
            changed.tr_index[name] = np.concatenate(
                [changed.tr_index[name][: k + 1], changed.tr_index[name][k + 1 :] * 2]
            )
        stream = contribution_stream(500, PARAMS.contributions, 0)
        for strategy in (CORE, Strategy("S5", TARGETS, ema_signal=True)):
            a = simulate(base, strategy, PARAMS, contributions_for_window(stream, 21, 499), 21, 499)
            b = simulate(changed, strategy, PARAMS, contributions_for_window(stream, 21, 499), 21, 499)
            decided_by_k = [t for t in a.trades if t.day <= base.dates[k + 1]]
            # Orders executed on k+1 were decided at k: same sizes even though prices changed.
            assert [(t.day, t.symbol, t.side, t.notional) for t in decided_by_k] == [
                (t.day, t.symbol, t.side, t.notional) for t in b.trades if t.day <= base.dates[k + 1]
            ]

    def test_deterministic_and_wash_sale_free(self):
        market = synthetic_market(1500, 4)
        stream = contributions_for_window(contribution_stream(1500, PARAMS.contributions, 0), 21, 1499)
        tight = Strategy("S4a", TARGETS, band_width=D("0.1"), st_cap_fraction=PARAMS.st_cap_fraction)
        for strategy in (
            tight,
            Strategy("S5", TARGETS, ema_signal=True),
            Strategy("S6", TARGETS, annual_reset=True),
            Strategy("S1", {"VT": Decimal(1)}),
            dataclasses.replace(tight, lot_method="fifo"),
            dataclasses.replace(CORE, cash_yield=True, execution="open"),
        ):
            a = simulate(market, strategy, PARAMS, stream, 21, 1499)
            b = simulate(market, strategy, PARAMS, stream, 21, 1499)
            assert a.values == b.values and a.trades == b.trades
            assert a.wash_sale_violations == 0
            assert min(a.values) > 0
        result = simulate(market, Strategy("S6", TARGETS, annual_reset=True), PARAMS, stream, 21, 1499)
        assert result.events["rebalance_annual_reset"] >= 4
        assert set(result.taxes_paid) == {2000, 2001, 2002, 2003, 2004}

    def test_open_execution_needs_opens_and_bad_window(self):
        market = synthetic_market(100, 1)
        market.open["VTI"][50] = None
        with pytest.raises(ValueError, match="opening prices"):
            Simulation(market, dataclasses.replace(CORE, execution="open"), PARAMS, {}, 0, 99)
        with pytest.raises(ValueError):
            Simulation(market, CORE, PARAMS, {}, 50, 50)


# ---------------------------------------------------------------- compare and report


class TestCompare:
    def test_blocks_and_walk_forward(self):
        assert compare.equal_blocks(0, 99, 2) == [(0, 50), (51, 99)]
        rows = [
            {"S4a": {"after_tax": 1}, "S3": {"after_tax": 2}, "S4b": {"after_tax": 3}},
            {"S4a": {"after_tax": 10}, "S3": {"after_tax": 20}, "S4b": {"after_tax": 30}},
        ]
        assert compare.walk_forward_choice(rows) == [
            {"block": 2, "chosen": "30% (S4b)", "chosen_after_tax": 30, "fixed_after_tax": 20}
        ]
        assert compare.window_indices([date(2020, 1, d) for d in range(1, 5)], date(2020, 1, 2), date(2020, 1, 3)) == (
            1,
            2,
        )
        assert compare.window_indices([date(2020, 1, 1)], date(2021, 1, 1), date(2021, 2, 1)) is None

    def test_formatting(self):
        assert compare.usd(-59.4) == "-$59" and compare.usd(1234.5) == "$1,234" and compare.usd(float("nan")) == "n/a"
        assert compare.pct(0.12345) == "12.35%" and compare.num(float("nan")) == "n/a"

    def test_end_to_end_synthetic_report(self, tmp_path, monkeypatch):
        params = dataclasses.replace(
            PARAMS, robustness_seeds=(1, 2), blocks=2, crisis_windows=(PARAMS.crisis_windows[0],)
        )
        market = synthetic_market(700, 9)
        text = compare.build_report(market, params, 21, 699, tmp_path)
        assert "SYNTHETIC DATA" in text
        for heading in (
            "## 1. Full sample",
            "## 2. Walk-forward blocks",
            "## 3. Crisis windows",
            "## 4. S5 signal study",
            "## 5. Robustness",
            "## 6. Sensitivities",
            "## 7. Risk flags",
            "## 8. Data and proxies",
            "## Change log",
        ):
            assert heading in text
        assert "| S3 |" in text and "| S6 |" in text
        assert len(list((tmp_path / "figures").glob("*.png"))) == 8

    def test_main_without_data_fails_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(compare, "RAW_DIR", tmp_path)
        assert compare.main([]) == 2

    def test_main_synthetic_writes_outside_the_report(self, tmp_path, monkeypatch):
        monkeypatch.setattr(compare, "SYNTHETIC_OUT", tmp_path)
        monkeypatch.setattr(
            compare,
            "load_params",
            lambda: dataclasses.replace(PARAMS, robustness_seeds=(1,), blocks=2, crisis_windows=()),
        )
        assert compare.main(["--synthetic", "--days", "400"]) == 0
        assert (tmp_path / "report.md").exists()


# ---------------------------------------------------------------- real-data path with fake CSVs

INCEPTIONS = {"VTI": "2001-05-24", "VXUS": "2001-09-10", "QQQM": "2002-03-04", "AVUV": "2002-06-03", "VT": "2002-01-15"}


def write_yahoo_csv(path, ticker, start, end, seed):
    """A file in yfinance's layout, with Adj Close built backward from the dividends."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    close = 40 * np.cumprod(1 + rng.normal(0.0003, 0.01, len(idx)))
    dividends = np.zeros(len(idx))
    dividends[60::63] = 0.005 * close[59:-1:63][: len(dividends[60::63])]
    factor = np.ones(len(idx))
    for i in range(len(idx) - 1, 0, -1):
        factor[i - 1] = factor[i] * (1 - dividends[i] / close[i - 1])
    frame = pd.DataFrame(
        {"Open": close * 0.999, "High": close * 1.01, "Low": close * 0.99, "Close": close, "Adj Close": close * factor,
         "Volume": 1000, "Dividends": dividends, "Stock Splits": 0.0, "Capital Gains": 0.0},
        index=pd.DatetimeIndex(idx, name="Date"),
    )
    frame.to_csv(path / f"{ticker}.csv")


@pytest.fixture
def fake_raw(tmp_path):
    from backtest.data import tickers_needed

    for i, ticker in enumerate(tickers_needed(PARAMS)):
        start = INCEPTIONS.get(ticker, "2000-01-03")
        write_yahoo_csv(tmp_path, ticker, start, "2003-12-31", seed=i)
    rates = pd.DataFrame({"observation_date": pd.bdate_range("1999-12-01", "2003-12-31").strftime("%Y-%m-%d"),
                          "DTB3": 4.5})
    rates.to_csv(tmp_path / "DTB3.csv", index=False)
    return tmp_path


class TestRealDataPath:
    def test_build_market_from_yahoo_layout(self, fake_raw):
        from backtest.data import build_market

        market = build_market(PARAMS, fake_raw)
        assert market.source == "yahoo" and market.notes == []
        assert market.dates[0] == date(2000, 1, 3) and market.dates[-1] == date(2003, 12, 31)
        assert set(market.price) == {"VTI", "VXUS", "QQQM", "AVUV", "AVUV_DFSVX", "VT"}
        switches = {s["sleeve"]: s["switch"] for s in market.splices}
        assert switches["VTI"] == "2001-06-01" and switches["QQQM"] == "2002-04-01" and switches["VT"] == "2002-02-01"
        vti = pd.read_csv(fake_raw / "VTI.csv", index_col=0, parse_dates=True)
        i = market.dates.index(date(2003, 6, 2))
        assert float(market.price["VTI"][i]) == pytest.approx(vti.loc["2003-06-02", "Close"], abs=1e-6)
        assert market.open["VTI"][market.dates.index(date(2001, 5, 31))] is None  # proxy period: no real opens
        assert market.rf[1] == pytest.approx(0.045 / 360)
        assert np.all(np.diff(market.tr_index["VT"]) != 0)

    def test_compare_main_on_cached_data(self, fake_raw, tmp_path, monkeypatch):
        monkeypatch.setattr(compare, "RAW_DIR", fake_raw)
        monkeypatch.setattr(compare, "REPORT", tmp_path / "out" / "report.md")
        monkeypatch.setattr(compare, "load_params", lambda: dataclasses.replace(
            PARAMS, robustness_seeds=(1,), blocks=2))
        assert compare.main([]) == 0
        text = (tmp_path / "out" / "report.md").read_text()
        assert "SYNTHETIC" not in text and "Data: yahoo" in text and "| VTI | 2001-06-01 |" in text

# etf-rebalancer

A deterministic, tax-aware, band-based rebalancer for a four-ETF core portfolio
(VTI 50%, VXUS 20%, QQQM 15%, AVUV 15%). A Claude Code agent executes it through the Robinhood
Trading MCP behind a fail-closed guard hook. Every number that becomes an order comes from pure,
unit-tested Python; the language model never decides a trade size.

- [STRATEGY.md](STRATEGY.md): the rules, formulas, assumptions and every ambiguity resolved.
- [RUNBOOK.md](RUNBOOK.md): setup, dry runs, going live, pausing, adding money, failed runs.

## The strategy in four rules

1. **New money does the rebalancing.** Deployable cash $C$ is split by the contribution rule,
   which never pushes a fund above its target:

   $$s_i = \max\bigl(0,\ w^{\star}_i (V + C) - V_i\bigr) \qquad b_i = C \cdot \frac{s_i}{\sum_j s_j} \qquad \sum_j s_j \ge C$$

2. **Bands.** A fund breaches when its weight is more than 20% away from target, relatively:

   $$\left\lvert \frac{w_i - w^{\star}_i}{w^{\star}_i} \right\rvert > 0.20$$

   Only a breach that new cash can't fix triggers sales. They're sold today, and the proceeds are
   reinvested by the contribution rule after settlement (no margin).
3. **Taxes.** Lots are chosen with the broker's real relief method (FIFO). If a rebalance would
   realize more than 100 USD of net short-term gain, it's cut down to the band edge; if that's
   still too much, it waits for your approval. A wash-sale guard blocks loss sales within 30 days
   of a buy, and buys within 30 days of a loss sale.
4. **Any amount.** Deposits of any size, a cash reserve that's never invested, and an optional
   phased deployment of a lump sum (default 3 monthly tranches, no market timing). An annual
   reset to target is proposed every January and never executes without your approval.

## How an order is made safe

```
read tools ──► responses saved by a hook ──► snapshot (code) ──► plan (pure code, 11 checks)
                                                                     │
           you confirm each order ◄── Claude Code "ask" ◄── guard hook ◄── review + place
```

| Layer | What it enforces |
|---|---|
| Planner (`engine.py`, `lots.py`, `plan.py`) | contribution and band rules, order and daily limits, wash sales, data freshness, an equity cross-check; pure functions with 100% branch coverage |
| Guard hook (`hooks/guard_order.py`) | every order must match exactly one pending line of today's armed, unexpired, approved live plan; kill switch; `DRY_RUN`; trading window; limits; atomic claim in SQLite; fails closed on any error |
| `.claude/settings.json` | read tools allowed, orders and cancels always ask, every other Robinhood tool denied, the agent locked out of code, settings, state and authority commands |
| Integrity manifest | config, code, hooks, settings or dependencies changed since you armed: no orders |
| Defaults | dry run unless `TRADING_ENABLED=true` and `DRY_RUN=false`; `touch KILL` stops everything |

## Quick start (dry run)

```bash
git clone https://github.com/charliel26/etf-rebalancer.git && cd etf-rebalancer
```

```bash
uv sync --extra dev && uv run pytest -q
```

```bash
claude mcp add --transport http robinhood-trading https://agent.robinhood.com/mcp/trading
```

Then edit `config/*.yaml` by hand, run `uv run rebalancer arm` in a terminal, start `claude`,
authenticate with `/mcp`, and type `/rebalance`. RUNBOOK.md has the full procedure, including
what to verify before the first live run.

## Tests and quality gates

```bash
uv run pytest -q --cov=src/rebalancer --cov=backtest --cov-branch
```

```bash
uv run coverage report --include="*/rebalancer/engine.py,*/rebalancer/lots.py,*/rebalancer/plan.py" --fail-under=100
```

```bash
uv run ruff check . && uv run ruff format --check .
```

- **Properties** (`hypothesis`): the contribution rule's invariants, including per-fund caps and
  rounding, and the band-edge and withdrawal formulas.
- **The guard.** Every refusal reason (100% branch coverage), the atomic claim, and the hook
  scripts run as subprocesses with real payloads, malformed input and missing config.
- **The settings.** `settings.json` is checked against the guard's tool allowlist.
- **The backtester.** Look-ahead, determinism, tax netting, the splice, and the real-data
  pipeline on files in Yahoo's format.

GitHub Actions runs these gates on every push (`.github/workflows/ci.yml`).

## Layout

```
config/            targets.yaml, limits.yaml (edit by hand, then `rebalancer arm`)
src/rebalancer/    engine, lots, plan (pure); ledger, guard, capture, cli, risk, report
  broker/          snapshot contract, Robinhood MCP parsers, Alpaca paper adapter
hooks/             guard_order.py (PreToolUse), capture_mcp.py (PostToolUse)
.claude/           settings.json, commands/rebalance.md (the agent's procedure)
backtest/          data, simulate, taxes, metrics, signal study, compare, charts
tests/             pytest + hypothesis
```

## Status and limitations

- **No backtest results yet.** The environment this was built in can't download market data.
  The backtester runs end to end on synthetic data, and on fake files in Yahoo's exact format.
  Run `uv run python -m backtest.compare --download` on a machine with internet access to
  produce `backtest/report.md`. No result in this repository is fabricated.
- **The MCP's formats aren't published.** The snapshot parsers and the guard's order-parameter
  list are written against the likely field names and fail closed on anything else. The first
  dry run verifies them (RUNBOOK.md §3–4).
- **Sales relieve lots FIFO** until the MCP is shown to accept specific lots.
- **The wash-sale guard only sees this account.**
- **The guardrails are tamper-evident, not tamper-proof** against a process with your shell
  rights.

Not investment advice. You're responsible for every order your account places.

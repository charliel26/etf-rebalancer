# Strategy specification: ETF core rebalancer

> **Status:** v0.2, implemented (2026-10-28). The code in `src/rebalancer/` and `backtest/`
> follows this document; where implementation changed a v0.1 decision, the change is noted
> in place and in §14 (A39–A49). Investment amounts are arbitrary: nothing assumes the
> 9,000 USD example (§8). Every backtest parameter is fixed in `backtest/config.yaml`
> before any data is loaded (§13.9).

---

## 0. Design principles

1. **Code decides, the agent executes.** Every number that becomes an order comes from pure,
   deterministic, unit-tested functions (`engine.py`, `lots.py`, `plan.py`: no I/O, no clock;
   dates are arguments). The Claude Code agent fetches broker data and submits exactly the
   lines of a plan. It never chooses a symbol, a side or an amount.
2. **The agent never transcribes numbers.** Broker responses are saved verbatim by a hook and
   turned into `snapshot.json` by code (§10.2). If the model typed a cash balance into a file,
   a typo would be a way for the model to size a trade.
3. **Safety layers can only remove orders.** Planner checks, then the `PreToolUse` guard hook,
   then Claude Code permission rules, then the broker's own controls. No layer can add or
   enlarge an order.
4. **Fail closed.** Missing or stale data, positions that don't reconcile, an open order at the
   broker, a crashed hook, or an order field the guard doesn't recognize all mean *no order*.
5. **New money does the rebalancing.** Buys never push a fund above its target. Sales happen
   only when a band is breached that new cash cannot fix, and they are tax-aware.

## 1. Notation

| Symbol | Meaning |
|---|---|
| $i \in \mathcal{C}$ | a core fund; $\mathcal{C}$ = VTI, VXUS, QQQM, AVUV |
| $w^{\star}_i$ | target weight, $\sum_i w^{\star}_i = 1$ |
| $q_i$, $p_i$ | shares held, price used for valuation |
| $V_i = q_i p_i$ | value of fund $i$ |
| $V = \sum_{i \in \mathcal{C}} V_i$ | core value (the spec's $V_{\text{core}}$) |
| $w_i = V_i / V$ | current core weight, defined only when $V > 0$ |
| $V_{\text{sat}}$, $V_{\text{oth}}$ | satellite sleeve value; value of unmanaged positions (§2) |
| $K$ | cash usable without margin (§3) |
| $A = V + V_{\text{sat}} + V_{\text{oth}} + K$ | total account value |
| $\delta$ | relative band half-width, default 0.20 |
| $R_t$ | cash held back on date $t$: configured reserve plus undeployed tranches (§8) |
| $C$ | cash deployed by this run |
| $b_i$, $x_j$ | buy amount for fund $i$, sale amount for fund $j$ (USD) |

I write the band width as $\delta$ so that $b_i$ keeps the spec's meaning (a buy amount).

## 2. Universe, targets and the satellite sleeve

- `config/targets.yaml` holds the targets (VTI 0.50, VXUS 0.20, QQQM 0.15, AVUV 0.15) and
  `satellite_symbols`. At load time every weight must be in (0, 1), the weights must sum to
  exactly 1 in decimal arithmetic, symbols must be unique, and no symbol may be both core and
  satellite. A bad file stops the run.
- The core symbols are the trading whitelist. Nothing else is ever bought or sold.
- Satellite positions are excluded from $V$ and from every weight, are never traded, and are
  included in $A$. Report check: $V_{\text{sat}} / A \le 0.15$, otherwise flag `SATELLITE_CAP`.
- A position that is neither core nor a listed satellite is **unmanaged**. It is excluded from
  weights, included in $A$, never traded, and flagged `UNMANAGED_POSITION`, so it can't go
  unnoticed.

## 3. Inputs and valuation

| Input | Source (Robinhood MCP) | Rule |
|---|---|---|
| Price $p_i$ | `get_equity_quotes` | Last trade price, at most 15 min old during regular hours, else the run stops (`STALE_QUOTE`). Outside regular hours the last close is used and the plan is dry-run only. |
| Shares $q_i$ | `get_equity_positions` | Fractional shares, 6 decimals. |
| Tax lots | `get_equity_tax_lots` | Lots must sum to $q_i$ within $10^{-6}$ shares, else that fund can't be sold this run (`LOT_MISMATCH`). |
| Cash $K$ | `get_portfolio` | The smaller of reported cash and buying power, so margin or instant-settlement credit is never spent. |
| Orders and fills | `get_equity_orders` | Any open order means no plan (`OPEN_ORDERS`). Fills from the last 31+ days feed the wash-sale guard. |
| Tradability | `get_equity_tradability` | A fund that isn't tradable or fractional-tradable gets no orders (`NOT_TRADABLE_IN_DOLLARS`; `TRADABILITY_UNKNOWN` if the response doesn't list it). |
| Daily closes | `get_equity_historicals` | Risk report only (§12). |
| Reported equity | `get_portfolio` | Cross-check, below. |

**Equity cross-check.** Positions valued at the quotes, plus cash, must agree with the equity
the broker reports:

$$\Bigl\lvert \sum_m q_m p_m + \text{cash} - E_{\text{reported}} \Bigr\rvert \le \max\bigl(25,\ 0.02 \cdot E_{\text{reported}}\bigr) \text{ USD}$$

Otherwise the plan is blocked (`EQUITY_MISMATCH`). A position missing from the positions
response would make that fund look empty, and the contribution rule would overbuy it. The
tolerance absorbs prices that move between the two calls.

Money is Python `Decimal`. Order amounts are whole cents.

## 4. Bands

$$L_i = (1-\delta) w^{\star}_i \qquad H_i = (1+\delta) w^{\star}_i$$

$$\text{fund } i \text{ breaches} \iff \left\lvert \frac{w_i - w^{\star}_i}{w^{\star}_i} \right\rvert > \delta \iff w_i < L_i \ \text{ or } \ w_i > H_i$$

| Fund | Target | Allowed range at $\delta = 0.20$ |
|---|---|---|
| VTI | 50% | 40% to 60% |
| VXUS | 20% | 16% to 24% |
| QQQM | 15% | 12% to 18% |
| AVUV | 15% | 12% to 18% |

The inequality is strict and evaluated in exact decimal arithmetic, so a weight exactly on an
edge is inside. There's no band test when $V = 0$. Because the band is relative, the small
funds have the tightest absolute tolerance (±3 percentage points for QQQM and AVUV against ±10
for VTI), so they'll be the usual breachers.

## 5. Contribution rule: the only rule that buys

$$s_i = \max\bigl(0,\ w^{\star}_i (V + C) - V_i\bigr) \qquad b_i = C \cdot \frac{s_i}{\sum_j s_j}$$

**Why it's safe.** Since $\sum_i w^{\star}_i = 1$ and $\sum_i V_i = V$, the unclipped gaps sum
to exactly $C$, and clipping at zero can only increase the sum:

$$\sum_i \bigl(w^{\star}_i (V + C) - V_i\bigr) = C \quad \text{and} \quad \max(0, z) \ge z \quad \Longrightarrow \quad \sum_j s_j \ge C$$

$$\Longrightarrow \quad b_i \le s_i \ \ \text{(no fund ends above its target } w^{\star}_i (V + C) \text{)} \qquad \sum_i b_i = C$$

For $C > 0$ the denominator is at least $C$, so it can't be zero.

**Deployable cash.** The rule runs when $K - R_t > c_{\min}$ (default 25 USD) and deploys

$$C = \min\bigl(K - R_t,\ N_{\text{left}}\bigr)$$

where $N_{\text{left}}$ is what remains of the daily gross-notional limit (3,000 USD).

**Details** (§14 says why):

1. *Cents.* Amounts are rounded to cents with the largest-remainder method, so
   $\sum_i b_i = C$ to the cent and each $b_i \le s_i + 0.01$.
2. *Minimum order.* Lines under 1 USD are dropped. That cash waits for the next run.
3. *Per-fund limits.* Each fund can take at most a cap: 0 if it is blocked (wash-sale window
   §7.4, not fractional-tradable), otherwise the order limit (1,500 USD). A limited fund's share
   is never pushed into the other funds. Instead the rule deploys the largest $d \le C$ for which
   every fund still ends at or below its target **relative to the core actually produced**,
   $V + d$:

$$h(d) = \sum_i \min\Bigl(\text{cap}_i,\ \max\bigl(0,\ w^{\star}_i (V + d) - V_i\bigr)\Bigr) - d, \qquad d^{\star} = \max\lbrace d \le C : h(d) \ge 0 \rbrace$$

   $h(0) \ge 0$ and $h$ is continuous, piecewise linear and non-increasing, so $d^{\star}$ is
   $C$ or found exactly on one linear segment. Without binding limits $h(C) = \sum_j s_j - C \ge 0$
   and this is the spec's formula. Example: 6,000 USD into an empty core with a 1,500 USD order
   limit deploys 3,000 USD (VTI 1,500, VXUS 600, QQQM 450, AVUV 450); the other 3,000 waits for
   the next run. Orders are never split to get around a limit. *(v0.1 cut a large line at the
   limit and kept the other lines, which left the other funds above target relative to the
   smaller core and could trigger needless band sales; found while building, fixed, and covered
   by property tests.)*

**Properties the tests assert** (with `hypothesis`): $\sum_j s_j \ge C$;
$0 \le b_i \le s_i + 0.01$; $\sum_i b_i = C$ exactly in cents before the filters; $b_i = s_i$
when no fund is above target; no fund's distance to its post-contribution target increases;
the result doesn't depend on the order in which funds are listed.

**Withdrawal rule (mirror image, used by the backtest to pay taxes).** To raise $W$ in cash:

$$u_i = \max\bigl(0,\ V_i - w^{\star}_i (V - W)\bigr) \qquad x_i = W \cdot \frac{u_i}{\sum_j u_j} \qquad \sum_j u_j \ge W$$

The same argument shows no fund is sold below its post-withdrawal target.

## 6. Band rebalance: the only rule that sells

### 6.1 When it runs

Contribution buys are computed first. Projected values are $\tilde V_i = V_i + b_i$ and
$\tilde V = \sum_i \tilde V_i$, with weights $\tilde w_i = \tilde V_i / \tilde V$. If no
$\tilde w_i$ breaches, the contribution covered the gap and nothing is sold. Otherwise the run
adds a band rebalance computed on the projected state.

### 6.2 Full rebalance (default)

$$e_j = \max\bigl(0,\ \tilde V_j - w^{\star}_j \tilde V\bigr) \qquad d_i = \max\bigl(0,\ w^{\star}_i \tilde V - \tilde V_i\bigr) \qquad D = \sum_j e_j = \sum_i d_i$$

Sell $x_j = e_j$ from each fund above target, buy $y_i = d_i$ for each fund below target, and
assert $\sum_i y_i = \sum_j x_j$.

### 6.3 Sell today, buy at the next run

In a Robinhood Cash account, sale proceeds can't be reinvested until they settle (T+1, the US
standard since 28 May 2024). Spending them sooner needs instant settlement, which is a
margin-account feature. So a band rebalance executes only its sell legs. The buy legs are
written to the plan as `deferred_orders` (for the $\sum y = \sum x$ check and the report). At
the next run the settled proceeds are ordinary cash and the contribution rule deploys them.

That reproduces the buy legs exactly, apart from overnight price moves. After the sales every
overweight fund sits at its target and the core is worth $\tilde V - D$. With $C = D$:

$$s_i = \max\bigl(0,\ w^{\star}_i \tilde V - \tilde V_i\bigr) = d_i \qquad \sum_i s_i = D = C \quad \Longrightarrow \quad b_i = d_i = y_i$$

So deferring costs nothing in targeting, and it leaves one invariant to audit: every buy is
cash moving a fund toward its target, never past it.

### 6.4 Lots and the short-term-gain cap

Sale lots follow §7. Let $G_{\text{ST}}$ be the net short-term gain the whole rebalance would
realize: short-term gains minus short-term losses over every lot it relieves, at plan prices,
under the broker's actual lot-relief method (§7.3).

- If $G_{\text{ST}} > G_{\max}$ (`max_st_gain_per_rebalance`, default 100 USD), the plan is
  downgraded to the band-edge version (§6.5) and flagged `BAND_EDGE_DOWNGRADE`.
- If the band-edge version still exceeds $G_{\max}$, it's flagged `ST_CAP_EXCEEDED` and needs
  manual approval (§9). It is never executed automatically.

### 6.5 Band-edge rebalance

The spec says to trade only enough to bring the breaching fund back to its nearest band edge.
Buys go through the contribution rule, which spreads cash over all underweight funds in
proportion to $d_i$. So "enough" is defined as **the smallest total sale $X$ after which, once
the proceeds are redeployed, every fund is inside its band.**

A fund above its upper edge must sell at least $\tilde V_j - H_j \tilde V$:

$$X_{\text{over}} = \sum_{j:\ \tilde w_j > H_j} \bigl(\tilde V_j - H_j \tilde V\bigr)$$

A fund below its lower edge receives $X d_i / D$ at redeployment. That gets it to the edge when

$$\tilde V_i + X \cdot \frac{d_i}{D} \ge L_i \tilde V \iff X \ge D \cdot \frac{L_i \tilde V - \tilde V_i}{w^{\star}_i \tilde V - \tilde V_i}$$

$$X_{\text{under}} = D \cdot \max_{i:\ \tilde w_i < L_i} \frac{L_i \tilde V - \tilde V_i}{w^{\star}_i \tilde V - \tilde V_i} \qquad X = \max\bigl(X_{\text{over}},\ X_{\text{under}}\bigr) \le D$$

Each fund above its upper edge first sells down to that edge. The rest, $X - X_{\text{over}}$,
comes from every fund still above target in proportion to its remaining excess (the withdrawal
rule, §5), so no fund is sold below target. With $X = D$ this is exactly the full rebalance.

### 6.6 What can limit a sale

- A lot can't be sold when the wash-sale guard (§7.4) or a compliance holding period (§7.5)
  blocks it, or when the fund's lots don't reconcile.
- A fund that can't sell its full amount sells what it can and is flagged `SELL_CONSTRAINED`.
- Each sell line is capped at 1,500 USD and the day's gross notional at 3,000 USD.
  Contribution buys use the daily budget first.
- Anything left undone is picked up at the next run, because the band test runs on every run.

## 7. Tax lots and the wash-sale guard

### 7.1 Definitions

A lot $\ell$ has acquisition date $a_\ell$, shares $q_\ell$ and cost per share $c_\ell$. Sold at
price $p$ on date $t$, its gain per share is $g_\ell = p - c_\ell$. It's long-term iff
$t > a_\ell + 1$ year: held *more* than one year, with one year after 29 February being
28 February. A sale's realized gain is $\sum_\ell \hat q_\ell g_\ell$, where $\hat q_\ell$ is the
number of shares relieved from lot $\ell$.

### 7.2 Preferred order (tax-aware)

| Rank | Lots | Order inside the bucket |
|---|---|---|
| 1 | short-term, $g_\ell \le 0$ | largest loss per share first |
| 2 | long-term, $g_\ell \le 0$ | largest loss per share first |
| 3 | long-term, $g_\ell > 0$ | smallest gain per share first |
| 4 | short-term, $g_\ell > 0$ | smallest gain per share first |

Ties go to the older lot, then the lot id. All lots of one fund share the price $p$, so inside
each bucket this is highest-cost-first. Short-term losses come before long-term losses because
they offset gains taxed at the higher short-term rate. Zero-gain lots go with the losses: they
cost nothing to sell and have no loss to disallow.

### 7.3 What the broker actually does

Robinhood relieves lots first-in-first-out unless the customer picks specific lots. Its lot
picker (launched December 2024) asks for a share count per lot. Nothing I could find says
whether `place_equity_order` accepts lot ids, or whether a dollar-based sell can carry them. So
the planner never assumes its preferred lots are the ones sold. Config `lot_method` takes one
of two values:

- **`fifo` (default until verified).** Realized gains, the short-term-gain cap and the
  wash-sale check all use FIFO relief, which is what will really happen. The tax-aware order
  then only shapes fund-level decisions (which fund, how much), not which lots go.
- **`tax_aware`.** The §7.2 order. Used by the backtest headline and in dry runs; a live plan
  with `tax_aware` is blocked (`LOT_METHOD_NOT_SUPPORTED_LIVE`) until the MCP schema shows that
  specific lots can be sent with an order. *(v0.1 called this value `specific`.)*

The backtest reports both (§13.3).

### 7.4 Wash-sale guard

Calendar days, with both window ends inclusive. For a symbol $s$, let $G(s)$ be its group of
substantially identical symbols (config; for example QQQM with QQQ, VTI with VTSAX and VTSMX,
VXUS with VTIAX and VGTSX).

- **W1 (sell side).** A sale on date $t$ may not realize a loss on any lot if any member of
  $G(s)$ was bought on a date $\tau$ with $0 \le t - \tau \le 30$.
- **W2 (buy side).** No buy of $s$ on date $t$ if any member of $G(s)$ was sold at a loss on a
  date $\tau$ with $0 \le t - \tau \le 30$.

Inputs are the fills from `get_equity_orders` plus the ledger.

- Under FIFO, W1 can block a fund's whole sale, because the oldest lot can't be skipped.
- Sales whose gain is unknown (for example a manual sale) count as loss sales.
- Dividend reinvestment is a purchase, so DRIP must be off in the agentic account.
- **Limit:** buys in accounts the system can't see (IRA, 401(k), a spouse's account, other
  brokers) can still cause a wash sale.

### 7.5 Compliance mode (for the future bank policy; off by default)

- `compliance.min_holding_days` (set to 30 when needed) makes lots younger than that unsellable.
- `compliance.approval_for_all_trades` makes every plan with orders need approval, and
  `rebalancer approve` then requires `--reference <pre-clearance id>`, which is stored with the
  approval.

## 8. Phased initial deployment

**Amounts are arbitrary.** You choose how much to phase in (`initial_deployment.total_usd`,
any amount, or `null` to deploy cash as it arrives), how many tranches, and how much cash
should never be invested (`cash.cash_reserve_usd`). You don't have to put, or invest, the
whole 9,000 USD. The system only ever looks at the cash actually in the agentic account.

The phased amount $K_0$ (8,100 USD in the original example) goes in as $n$ equal tranches
$\kappa = K_0 / n$ (default $n = 3$, so 2,700 USD) on dates $d_k = d_0 + k$ calendar months,
$k = 0, \dots, n-1$. Month ends are clipped: 31 January plus one month is 28 or 29 February.
The schedule is a reserve that shrinks over time (computed as $K_0 \cdot n_t / n$, which is
exact for any amount):

$$R_t = R_{\text{cfg}} + \kappa \cdot n_t \qquad n_t = \text{number of tranche dates } d_k \text{ later than } t$$

On day 0, $n_t = 2$, so 5,400 USD is held back. After month 1, 2,700 USD is held back. After
month 2, nothing is. So:

- Each tranche goes out through the ordinary contribution rule at the first run on or after its
  date. Weekends, holidays and pauses only delay it.
- Contributions made during the phase are deployed at once.
- There's no price-based timing.
- The schedule is a pure function of date and config, with no hidden state.

If cash falls below $R_t$ (for example after a withdrawal, or if you fund the account with
less than `total_usd`), $C = 0$ and the report warns (`CASH_BELOW_RESERVE`); set `total_usd` to
what you actually deposit.

## 9. Annual reset

- **Due** from the configured date (default 15 January) until it's executed or dismissed for
  that year. The daily report says when it's due. You see the proposal by running
  `rebalancer plan --annual-reset` in a terminal; `rebalancer dismiss-reset <year>` skips it. The
  agent can't start a reset: `settings.json` denies any command containing `--annual-reset`.
- **Content.** A full reset to target ($X = D$ in §6), whether or not a band is breached. Sells
  happen that day with the same lot logic; buys happen at the next run through the contribution
  rule. There's no short-term-gain downgrade. The plan shows the estimated realized short- and
  long-term gains instead. A proposal with nothing to sell means the core is already on target;
  dismiss the reset for that year.
- **Approval, never automatic.** The reset plan has `requires_approval: true`.
  `rebalancer approve <run_id>` only works in an interactive terminal. It prints the plan and
  asks you to type the first 8 hex characters of the plan's hash. The approval is bound to that
  hash and expires at the end of the day, because prices move. The hook rejects every line of an
  unapproved plan. The same mechanism covers `ST_CAP_EXCEEDED` plans (§6.4).
- **Why 15 January:** gains realized then fall in the new tax year, so tax is due about
  15 months later, and the date avoids the holidays. It's configurable.

## 10. Run procedure, snapshot and plan

### 10.1 One run

1. The agent calls the read tools. A capture hook stores every response.
2. `rebalancer snapshot` builds `snapshot.json` from the captured files and validates it.
3. `rebalancer plan` writes the plan, the markdown report and a ledger row. Any blocker (kill
   switch, open orders, a stale or missing quote, an equity mismatch, a snapshot from another
   day, a failed check, or a live plan requested outside the execution window) makes the plan
   dry-run. Lots that don't reconcile block that fund's sales.
4. **Live only.** `rebalancer pending` prints the lines of today's live plan that are still to
   be placed. It prints none, and says why, in a dry-run session, with the kill switch on, once
   the plan is older than 60 minutes, or until the plan is approved, if it needs approval. For
   each line, sells first: `review_equity_order`, then `place_equity_order` with exactly the
   line's values. The guard hook checks every call.
5. The agent calls `get_equity_orders`, then `rebalancer reconcile` records the outcomes.

### 10.2 Snapshot capture

A `PostToolUse` hook on `mcp__robinhood-trading__.*` writes each response verbatim to
`state/raw/<date>/<time>_<tool>_<id>.json`. `rebalancer snapshot` parses those files into
`snapshot.json` and records each file's SHA-256 next to it. The planner then cross-checks the
data:

- lots against positions (§3);
- positions times quotes plus cash against the reported equity (§3);
- every core symbol quoted.

The spec has the agent write `snapshot.json`. This keeps the same file and the same contract
but takes the model out of the data path. If MCP responses turn out not to reach the hook, I'll
fall back to an agent-written snapshot with the same cross-checks, and say so.

### 10.3 `plan.json`

A real plan (first tranche, 2,700 USD into an empty core; `portfolio` and the input hashes
shortened):

```json
{
  "schema_version": 1,
  "date": "2026-11-02",
  "run_id": "2026-11-02-live-34d165e9",
  "mode": "live",
  "action": "contribution",
  "requires_approval": false,
  "orders": [
    {"line_id": "L1", "symbol": "AVUV", "side": "buy", "notional_usd": 405.0, "reason": "contribution"},
    {"line_id": "L2", "symbol": "QQQM", "side": "buy", "notional_usd": 405.0, "reason": "contribution"},
    {"line_id": "L3", "symbol": "VTI", "side": "buy", "notional_usd": 1350.0, "reason": "contribution"},
    {"line_id": "L4", "symbol": "VXUS", "side": "buy", "notional_usd": 540.0, "reason": "contribution"}
  ],
  "deferred_orders": [],
  "checks": {
    "targets_sum_to_one": true, "orders_whitelisted": true, "orders_within_size_limits": true,
    "daily_notional_within_limit": true, "contribution_sum_matches": true,
    "deferred_buys_equal_sells": true, "no_symbol_bought_and_sold": true,
    "sells_covered_by_eligible_lots": true, "no_buy_above_target": true,
    "no_sell_below_target": true, "wash_sale_clear": true
  },
  "flags": [],
  "blockers": [],
  "tax_estimate": {"lot_method": "fifo", "lots": {}, "lt_gain_usd": 0.0, "st_gain_usd": 0.0},
  "totals": {"buys": 2700.0, "sells": 0.0},
  "portfolio": {"core_value": 0.0, "cash": 2700.0, "...": "..."},
  "inputs": {"code_version": "0.2.0", "config_sha256": "...", "created_at": "2026-11-02T15:00:00+00:00", "snapshot_sha256": "..."},
  "plan_sha256": "1febf06e9916bfc6f08d4bb7540a0d3a9cf9e5d59788c948b4dc2680fa0638c3"
}
```

Sells always come before buys in `orders`. The spec's fields are `date`, `run_id`, `mode`,
`orders` (`symbol`, `side`, `notional_usd`, `reason`), `checks` and `totals`. The rest are
additions. Any failed check becomes a blocker (`CHECK_FAILED:<name>`). `plan.py` stays pure: it returns
canonical JSON text, the CLI writes the file, and the creation time is passed in as an argument.

Every plan is written to `state/plans/<run_id>.json` (and copied to `state/plan.json` for
reading). The guard does not trust whatever `plan.json` happens to be on disk: it loads the
plan file of the one live run the ledger registered for today and checks its hash against the
ledger, so a later dry run (or an edited file) can't change what may be executed.

### 10.4 Ledger (SQLite) and the life of a plan line

Tables: `runs` (at most one live run per New York date, enforced by a unique index),
`plan_lines`, `approvals`, `trades` (fills, for the wash-sale guard), `annual_resets`, and an
append-only `events` log.

| Status | Set by | Meaning |
|---|---|---|
| `PENDING` | `rebalancer plan` | not yet submitted |
| `CLAIMED` | guard hook, atomically, *before* the call | can't be matched again; the amount actually sent is recorded and counts toward the daily limit |
| `SUBMITTED` | `PostToolUse` hook | broker returned an order id |
| `SUBMIT_FAILED` | `PostToolUseFailure` hook | the call errored |
| `FILLED` / `PARTIAL` / `CANCELLED` / `REJECTED` | `rebalancer reconcile` | broker's final state |
| `NOT_PLACED` | `rebalancer reconcile` | claimed 5 minutes ago and no matching order exists, e.g. you declined the permission prompt. A later reconcile still links the order if it shows up. |

Nothing is retried automatically. Re-arming a line (`rebalancer rearm`) is a human command, in a
terminal, after checking the broker's order history.

### 10.5 Execution adapters and code layout additions

- **Robinhood:** agent-mediated through the MCP and guarded by the hook (§11).
- **Alpaca paper:** pure Python, no model in the loop. It implements `broker/base.py`, calls the
  same guard functions in code, and keeps tax lots from its own fills, because Alpaca doesn't
  expose lots.
- **Modules beyond the spec's layout:**
  - `src/rebalancer/config.py`: load and validate the YAML files.
  - `src/rebalancer/guard.py`: the checks shared by the hook and the Alpaca executor. This is
    how "enforced in code AND in the hook" stays one implementation.
  - `src/rebalancer/cli.py`: the `rebalancer` commands.
  - `hooks/capture_mcp.py`: the capture hook from §10.2.
  - `.claude/commands/rebalance.md`: the agent's run procedure.

## 11. Hard constraints and guardrails

| Constraint | Planner | Guard hook | `settings.json` | Outside the code |
|---|---|---|---|---|
| Core-ETF whitelist | yes | yes | | |
| Long only (sell no more than eligible lots) | yes | must match a plan line | | Cash account |
| No margin | $K$ = min(cash, buying power) | | | Cash account or margin off |
| No options or crypto | | server-wide default deny | deny by name | |
| No extended hours | live plan only inside the window | flag false, time window | | |
| Order min/max, daily notional | yes | yes | | |
| One live run per date | ledger unique key | yes | | |
| Wash-sale guard | W1, W2 | W2 re-checked on buys | | DRIP off |
| Kill switch, `TRADING_ENABLED` | plan marked non-live | yes | | |
| `DRY_RUN` (default true) | plan marked non-live | yes | | |
| Approvals (reset, ST cap) | flag | yes | | Robinhood trade approval, if offered |

**The guard hook** (`hooks/guard_order.py`):

- **Fails closed.** It's registered on `PreToolUse` with matcher `mcp__robinhood-trading__.*` and
  `"onFailure": "block"`. This matters: Claude Code only blocks on exit code 2. Any other
  non-zero exit, a crash or a timeout lets the call through unless `onFailure` is `block`
  (needs Claude Code 2.1.295 or later). So the hook denies with `permissionDecision: "deny"`
  and exit code 2, and every exception path denies.
- **Deny by default for the whole server.** Any robinhood-trading tool not on the allowlist is
  denied. The allowlist is the read tools plus `review_equity_order`, `place_equity_order` and
  `cancel_equity_order`. This also covers tools added after the settings deny list was written.
- **Deny-only.** The hook never returns `allow`. A permitted order still goes through the normal
  permission rules. `place_equity_order` is `ask` in `.claude/settings.json`, so an interactive
  run shows you each order. An unattended run needs you to change that deliberately.

**Checks on `place_equity_order`**, in order. The first failure denies.

1. **Order shape** (parsing the call's arguments).
   - Symbol present, side buy or sell, a positive dollar amount (no share quantity).
   - Market order, time in force day, regular hours only (`extended_hours` false or absent).
   - No parameter the guard doesn't recognize, and none given twice.
2. **Integrity.** Every protected file matches its SHA-256 in `state/armed.json`: `config/*.yaml`,
   `hooks/*.py`, `src/rebalancer/**/*.py`, `.claude/settings.json`, `.claude/commands/*.md`,
   `pyproject.toml` and `uv.lock`. That file is written by `rebalancer arm`, which only works in an
   interactive terminal.
3. **Switches.** There's no `KILL` file in the project root, `TRADING_ENABLED` is exactly
   `true`, and `DRY_RUN` is exactly `false`. Unset means dry run.
4. **Time.** It's inside the execution window: 09:35 to 15:55 America/New_York on an NYSE trading
   day, and never later than 5 minutes before the close (so 12:55 on a half-day).
5. **Run.** The ledger has a live run for today.
6. **Plan file.** That run's plan file parses, its hash verifies and matches the ledger, it has
   `mode: live`, today's date and no blockers, and it was created in the last 60 minutes.
7. **Approval.** Recorded today for this plan hash, whenever `requires_approval` is set.
8. **Whitelist.** The symbol is a core fund.
9. **Size.** 1 USD ≤ amount ≤ 1,500 USD.
10. **Account.** If `agentic_account_id` is set and the order names an account, they're equal.
11. **Wash sales.** For buys, W2 is re-checked against the ledger.
12. **Review.** A successful `review_equity_order` with the same symbol and side, and an amount
    within 1 USD, was captured in the last 10 minutes.
13. **Claim.** In one SQLite `BEGIN IMMEDIATE` transaction:
    - the order matches exactly one `PENDING` line of today's live run (same symbol and side,
      amount within 1 USD; `match_tolerance_usd` can't be set above 1);
    - today's gross notional, counting the amounts actually sent and this order, is at most
      3,000 USD;
    - the line goes from `PENDING` to `CLAIMED`.

    So two parallel calls can't both pass, and the 1 USD tolerance can't stretch the daily
    limit.

**`.claude/settings.json`:**

- The read tools and `review_equity_order` are in `allow`, with the read-only `rebalancer`
  commands (`status`, `snapshot`, `plan`, `pending`, `reconcile`, `report`).
- `place_equity_order` and `cancel_equity_order` are in `ask`. Claude Code prompts for an
  explicit `ask` rule in every permission mode, so you confirm each order.
- Every other robinhood-trading tool is denied by name pattern: news, options, crypto, stop,
  limit, trailing and bracket orders, watchlists, scanners, alerts, margin, transfers, deposits,
  withdrawals, cards and search.
- The agent is denied the commands that grant authority (`approve`, `arm`, `rearm`,
  `dismiss-reset`) and any command containing `--annual-reset`.
- Claude's file tools are denied on `config/`, `state/`, `hooks/`, `src/`, `.claude/`, `.venv/`,
  `pyproject.toml` and `uv.lock`. That covers `.claude/settings.local.json`, which could
  otherwise turn the hooks off.
- `disableAllHooks: false` overrides a user-level switch. Bypass mode and auto mode are disabled
  for the project.
- The hooks above are registered.

**Threat model.** These layers stop:

- mistakes: a wrong symbol, side or amount, a duplicate order, a mistyped number;
- prompt injection through tool output;
- planner bugs.

The integrity check makes edits to the limits, the plan code or the settings visible. It runs
inside the guard, so it can't notice a rewritten guard: that's what the file-tool deny rules
are for. None of this stops a process running with your shell rights. Such a process could
rewrite files, or start Claude Code with flags that turn hooks off (RUNBOOK.md lists what not
to do). The hard boundary is outside the machine: the agentic account only holds what you fund
it with, and Robinhood's own controls, such as its order review, need you.

## 12. Risk monitoring (report only, never trades)

The return series applies today's invested holdings (core, satellite, unmanaged) at their
current value weights $\omega_m$ to the last 63 trading days of closes:

$$r_t = \sum_m \omega_m r_{m,t}$$

Volatility is an EWMA over a finite 63-day window, normalized, with the RiskMetrics zero-mean
convention:

$$\hat\sigma_t^2 = \frac{\sum_{k=0}^{62} \lambda^k r_{t-k}^2}{\sum_{k=0}^{62} \lambda^k} \qquad \sigma^{\text{ann}}_t = \sqrt{252} \cdot \hat\sigma_t \qquad \lambda = 0.94$$

Since $\lambda^{63} \approx 0.02$, the window keeps about 98% of the weight an infinite EWMA would
have.

Drawdown is measured on the same holdings over the trailing 756 trading days (3 years):

$$N_t = \prod_{s \le t} (1 + r_s) \qquad \text{DD}_t = \frac{N_t}{\max_{s \le t} N_s} - 1$$

Flags: $\sigma^{\text{ann}}_t > 0.20$, $\text{DD}_t < -0.25$, and $V_{\text{sat}} / A > 0.15$.

- Cash is left out of $\omega$. Otherwise the deployment phase would hide the risk of the fund
  mix. Cash share is reported separately.
- These are **current-mix** metrics, not the account's own history, because the broker doesn't
  give a flow-adjusted account return series. The backtest uses true time-weighted returns.

## 13. Backtest methodology

### 13.1 Data and proxies

The data is daily split-adjusted closes, adjusted closes (total return) and distributions
(dividends, and capital-gain distributions for mutual funds). It's downloaded once and cached
with a manifest: source, download time, first and last date, row count, SHA-256. The raw data
isn't committed (provider terms); the manifest is. The source is Yahoo Finance via `yfinance`,
which needs no key. It's currently blocked by this environment's network policy (§15).

| Sleeve | Real fund (approx. inception) | Pre-inception proxy | Notes |
|---|---|---|---|
| VTI | VTI (2001) | VTSMX, Vanguard Total Stock Market Index Fund, Investor shares (1992) | Same strategy; higher expense ratio. |
| VXUS | VXUS (2011) | VGTSX, Vanguard Total International Stock Index Fund, Investor shares (1996) | Its benchmark had narrower, large/mid-cap coverage before about 2010. |
| QQQM | QQQM (2020) | QQQ (March 1999) | Same index (Nasdaq-100); 0.20% vs 0.15% fee. |
| AVUV | AVUV (2019) | VISVX, Vanguard Small-Cap Value Index Fund, Investor shares (1998) | AVUV is actively managed (value and profitability screens) and smaller-cap, so expect real tracking error. Sensitivity: DFSVX (DFA US Small Cap Value). |
| VT (S1) | VT (2008) | 50/50 VTSMX/VGTSX blend, not rebalanced | Drifts roughly like a cap-weighted world index. The US weight at the splice date is reported as a sanity check. |

- **Splice rule.** The proxy is used through the last trading day of the ETF's inception month,
  and the real ETF from the first trading day of the next month.
- **How it's spliced.** In return space: total-return and price-return series are chained, and
  the synthetic price level is scaled to equal the real ETF's price from the splice on.
- **Overlap check.** For each splice the report shows the daily-return correlation, annualized
  tracking error and annual return gap between the ETF and its proxy.
- **Sample.** The binding start is QQQ's launch in March 1999. The simulation starts after a
  21-trading-day warm-up (for the EMA) and runs to the last available close.

### 13.2 Mechanics (identical for every strategy)

| Item | Assumption |
|---|---|
| Decision and execution | Decide at close $t$ using data through $t$; trade at close $t+1$. Optional: open $t+1$, only on dates where all four real ETFs have opening prices (from November 2020). Mutual-fund proxies only have NAVs. |
| Frequency | One decision per trading day. Live runs may be less frequent. |
| Settlement | Sale proceeds are usable at the next decision, consistent with T+1. |
| Initial capital | 9,000 USD of cash on day 0, deployed in three 3,000 USD tranches by every strategy. The composition of the 900 USD of existing equities is unknown, so it's treated as cash. |
| Contributions | A seeded random stream (§13.4), identical across strategies, known at the close of its date. |
| Fractional shares | Yes, 6 decimals. |
| Costs | 5 bp of notional on every buy and sell (spread and slippage). No commissions. |
| Dividends and distributions | Paid as cash (DRIP off) 5 trading days after the ex-date, counted in $A_t$ as a receivable until then, then deployed by each strategy's buy rule. |
| Cash yield | 0%. Sensitivity: the 3-month T-bill rate. |
| Order and daily dollar limits | Not applied. They're sized for a ~9,000 USD account and meant to be raised as it grows. The 1 USD minimum order and 25 USD deploy threshold are applied. |
| Short-term-gain cap | Scaled with the account: $G_{\max,t} = 100 \cdot A_t / 9000$ USD. Sensitivity: fixed 100 USD. |
| Wash-sale guard | The same code as live, for every strategy's buys (S5 included). An independent counter in the simulator checks that zero wash sales occur. |
| Lot method | Tax-aware (§7.2) in the headline results; FIFO as a sensitivity. |
| Plans needing approval | Band-edge plans still over the short-term-gain cap, and S6's annual resets, are assumed approved and executed (live they wait for you); counted in the report's diagnostics. |
| Tax shortfall | If paying tax leaves cash negative, the withdrawal rule (§5) sells the shortfall at the next close. |
| Distributions | Reported dividends plus capital-gain distributions, used when they agree with the provider's adjusted close to within 0.1%/yr; otherwise the distributions implied by the adjusted close, with a note in the report. |

**Taxes.** Each calendar year, $S$ is net short-term and $L$ is net long-term realized gains,
including carried-forward losses by character. They're cross-netted:

$$\text{tax} = 0.22 \cdot \max\bigl(0,\ S + \min(0, L)\bigr) + 0.15 \cdot \max\bigl(0,\ L + \min(0, S)\bigr) + 0.15 \cdot \text{Div} + 0.22 \cdot \text{Int}$$

- **Losses.** Net losses carry forward with their character. The 3,000 USD-a-year offset against
  ordinary income is ignored, which is conservative.
- **Simplifications.** All dividends are treated as qualified (15%) and capital-gain
  distributions as long-term. No state tax, no NIIT, no foreign tax credit. Rates are fixed over
  the whole sample, as specified.
- **Payment.** Paid from the portfolio on the first trading day on or after 15 April of the next
  year: cash first, then the withdrawal rule (§5), or VT lots for S1. The same lot logic and
  wash-sale guard apply.
- **Return measures.** Tax payments are an external outflow for the time-weighted return, so the
  TWR is pre-tax. Taxes enter terminal wealth and IRR.

### 13.3 Strategies

| ID | Buys | Sells (besides taxes) |
|---|---|---|
| S1 | 100% VT (proxy) with all cash | none |
| S2 | contribution rule, core targets | none |
| **S3** | contribution rule | 20% bands; full rebalance, band-edge when the ST-gain cap binds |
| S4a / S4b | as S3 | 10% / 30% bands |
| S5 | EMA signal (below) | none |
| S6 | contribution rule | annual full reset on the first trading day on or after 15 January; no bands |

S3 doesn't execute annual resets: live they need your approval, and I assume they're declined.
S6 shows what they would do.

**S5, "the original idea", made precise.** The signal is computed per fund on total-return prices:

$$E_{i,t} = \alpha P_{i,t} + (1-\alpha) E_{i,t-1} \qquad \alpha = \tfrac{2}{9} \qquad E_{i,0} = P_{i,0} \qquad \text{signal}_{i,t} = \mathbf{1}\left[P_{i,t} < E_{i,t}\right]$$

At close $t$, with deployable cash $C$, the contribution gaps $s_i$ are computed only for funds
whose signal is on. S5 buys at close $t+1$:

$$b_i = s_i \cdot \min\Bigl(1,\ \frac{C}{\sum_{j \text{ signalled}} s_j}\Bigr)$$

That is, each signalled fund is restored to target as far as cash allows, and unused cash waits.
Total-return prices keep ex-dividend drops from firing false signals. Their backward adjustment
doesn't leak future information, because a later dividend rescales every earlier price by the
same factor, which leaves the comparison $P_t < E_t$ unchanged.

**Sensitivities** (S3 unless noted): FIFO lots; fixed 100 USD short-term-gain cap; DFSVX instead
of VISVX; cash earning the T-bill rate (S3 and S5); execution at the next open (sub-sample from
November 2020).

### 13.4 Contribution stream

- On each trading day a contribution arrives with probability 1/21, so about monthly, with
  irregular gaps.
- The amount is LogNormal with median 300 USD and log-standard-deviation 0.75, rounded to whole
  dollars and clipped to the range 50–5,000 USD. That's a mean of about 400 USD, or about
  4,800 USD a year.
- numpy `default_rng`. The headline seed is 0; seeds 1–20 are used for robustness.
- Every strategy gets the same stream.

### 13.5 Metrics

The time-weighted daily return uses end-of-day external flows $F_t$ (contributions in, tax
payments out):

$$r_t = \frac{A_t - F_t}{A_{t-1}} - 1 \qquad N_t = \prod_{s \le t} (1 + r_s)$$

| Metric | Definition |
|---|---|
| CAGR | $N_T^{365.25/\text{days}} - 1$: time-weighted, pre-tax, after costs |
| After-tax terminal wealth | $A_T$, minus unpaid tax for the final year, minus the tax due if every lot were sold at $T$ |
| IRR (extra) | the money-weighted return that equates all contributions (including the initial 9,000 USD) to after-tax terminal wealth |
| Annualized vol | $\sqrt{252} \cdot \operatorname{sd}(r_t)$ |
| Sharpe | $\sqrt{252} \cdot \overline{(r_t - r^f_t)} / \operatorname{sd}(r_t - r^f_t)$, where $r^f$ is the 3-month T-bill (FRED DTB3) |
| Max drawdown and duration | $\min_t \text{DD}_t$ on $N_t$; the longest stretch below a previous peak, in trading days, marked if still unrecovered |
| Turnover | sales per year divided by average $A_t$ (buys are mostly new money, so they're excluded); gross turnover also shown |
| Number of trades | count of orders |
| Realized ST / LT gains | totals (per year in an appendix), plus total taxes paid |
| Average cash drag | mean of $c_t = K_t / A_t$; also its return cost, $252 \cdot \operatorname{mean}_t\bigl[c_{t-1} (r^{\text{inv}}_t - r^{K}_t)\bigr]$ |
| Days in cash | number of days with $K_t / A_t > 0.01$ |

### 13.6 S5 signal study

For each fund and each horizon $h \in$ {1, 5, 20} trading days there are two forward returns:

- from the signal close: $R^{(h)}_t = P_{t+h} / P_t - 1$;
- from the execution close: $P_{t+1+h} / P_{t+1} - 1$, which is what S5 can actually earn.

The study reports:

- the share of days the signal fires;
- the mean forward return on signal days, on other days and unconditionally;
- the difference $\gamma$ from the regression below, with its Newey–West (Bartlett) t-statistic;
- the Newey–West t-statistic of the conditional mean $a + \gamma$ against zero.

$$R^{(h)}_t = a + \gamma \cdot \text{signal}_t + \varepsilon_t \qquad \text{NW lag} = \max\bigl(h - 1,\ \lfloor 4 (T/100)^{2/9} \rfloor\bigr)$$

The lag covers the overlap of multi-day returns.

### 13.7 Splits and windows

- **Walk-forward blocks.** The sample is cut into 5 consecutive blocks of equal length in trading
  days. Each block is a fresh start: 9,000 USD, three tranches, and the same contribution process
  restricted to the block. Every metric is reported per block as well as for the full sample.
- **Walk-forward band choice.** For blocks 2–5, the band width (10, 20 or 30%) with the best
  after-tax terminal wealth in the previous block is applied and compared with a fixed 20%. That
  is the only fitting anywhere, and it only looks backward.
- **Crisis windows,** read from the full-sample run with portfolio state carried in: 2000–2002,
  2008–2009, 2020 Q1, 2022 (calendar boundaries). Each reports return, max drawdown, vol, trades,
  realized gains and the largest weight deviation.

### 13.8 Checks on the backtester itself

- Constant prices give zero return apart from costs.
- One fund with no dividends: the TWR equals that fund's return.
- No look-ahead: changing prices after date $t$ changes no order decided on or before $t$.
- Determinism: the same inputs give byte-identical outputs.
- Hand-worked tax cases (netting, carry-forward).
- Total return rebuilt from closes and distributions matches the provider's adjusted close.
- Zero wash sales.
- Proxy/ETF overlap statistics.

### 13.9 Pre-registration

Everything in §13.1–13.7 is fixed now, before any data is loaded:

- proxies and the splice rule;
- costs, tax rates and payment dates;
- the dividend lag and cash yield;
- the contribution process and seeds;
- band widths and the cap scaling;
- blocks, crisis windows and the Newey–West lag rule.

If something has to change after the data is seen (for example a data error), the change and the
reason go in the report's change log. No parameter is ever chosen by full-sample performance.

## 14. Ambiguities resolved

| # | Spec point | Resolution | § |
|---|---|---|---|
| A1 | "15% of total value" | Total account value $A$, including cash and unmanaged positions. | 2 |
| A2 | Positions that are neither core nor satellite | Unmanaged: excluded from weights, never traded, flagged. | 2 |
| A3 | Which price to value at | Last trade, at most 15 min old, during regular hours; otherwise dry-run only. | 3 |
| A4 | "Uninvested cash" | Min(cash, buying power) minus the reserve; never margin or instant credit; any open order blocks the run. | 3, 5 |
| A5 | Band boundary | Strict inequality, exact decimals; no test when $V = 0$. | 4 |
| A6 | Rounding vs "sum equals C" | Cents with largest remainder: the sum is exact and each buy is at most its gap plus 1 cent. | 5 |
| A7 | Contribution larger than the limits | $C$ capped by the daily limit; the order limit is a per-fund cap inside the rule (A40); leftovers wait. Orders are never split. | 5 |
| A8 | Wash-sale-blocked fund inside the contribution rule | A cap of 0 (A40); unspent cash waits rather than overfilling other funds. | 5 |
| A9 | "Only if no contribution covers the gap" | Contribution first; bands tested on the projected post-contribution weights. | 6.1 |
| A10 | Buying with same-day sale proceeds | Sell today, buy at the next run via the contribution rule (no margin, Cash-account settlement). Shown to reproduce the buy legs; the sum check runs on the deferred legs. | 6.3 |
| A11 | "Band edge" for a fund breaching on the low side | The smallest total sale that leaves every fund in band after redeployment; closed form. | 6.5 |
| A12 | "A sell would realize a ST gain > 100" | Net short-term gain of the whole rebalance, at plan prices, under the broker's real lot relief. | 6.4 |
| A13 | Band-edge still over the cap | Requires manual approval; never automatic. | 6.4 |
| A14 | Order within the loss bucket, zero-gain lots, long-term definition | ST losses, LT losses, LT gains, ST gains; highest cost first inside; zero-gain lots with the losses; long-term = held more than a year. | 7.1, 7.2 |
| A15 | Tax-aware lots vs Robinhood's FIFO | `lot_method: fifo` until the MCP is shown to accept lot ids; the planner models what the broker will really do. | 7.3 |
| A16 | Wash-sale windows | Calendar days, inclusive; groups of substantially identical symbols; sales with unknown P/L count as losses. | 7.4 |
| A17 | Future compliance rules | Minimum holding days and approval for all trades; off by default. | 7.5 |
| A18 | Tranche dates | Calendar months, month-end clipped, executed at the first run on or after; a shrinking reserve; new money during the phase deployed at once. | 8 |
| A19 | Annual reset mechanics | Full reset when due, same sell-then-redeploy path, approval bound to the plan hash, same day only. | 9 |
| A20 | `plan.py` is pure but "writes plan.json" | It returns canonical JSON; the CLI writes the file. | 10.3 |
| A21 | Agent "produces snapshot.json" | Code builds it from hook-captured raw responses. | 10.2 |
| A22 | Hook "marks each matched line as executed" | Claimed atomically before the call, outcome recorded after, reconciled against the broker; no automatic retry. | 10.4 |
| A23 | Hook blocks with "non-zero exit" | Exit code 2 plus a JSON deny, registered with `onFailure: block`, because other non-zero codes don't block in Claude Code. | 11 |
| A24 | Robinhood tools missing from the deny list | The hook denies any robinhood-trading tool not on its allowlist. | 11 |
| A25 | One run per date | Key = New York date of a live run. Dry runs are unlimited. A live plan can be resumed that day but not replaced. | 10.4, 11 |
| A26 | No extended hours | The flag must be false and the call must fall between 09:35 and 15:55 ET on an NYSE trading day. | 11 |
| A27 | Daily notional | Gross buys plus sells for the day. | 5, 11 |
| A28 | `DRY_RUN` and `TRADING_ENABLED` | Exact strings: only `DRY_RUN=false` and `TRADING_ENABLED=true` enable trading; checked when planning and again for each order. | 11 |
| A29 | "Realized portfolio vol" in live reports | Current-mix vol from trailing returns; cash excluded; finite-window normalized EWMA. | 12 |
| A30 | Starting capital in the backtest | 9,000 USD of cash and the same three tranches for every strategy. | 13.2 |
| A31 | Dividends (not in the spec) | Paid as cash, taxed at 15%, deployed by each strategy's buy rule. | 13.2 |
| A32 | Tax mechanics | Annual netting with carry-forward, paid on 15 April from the portfolio, liquidation tax at the end. | 13.2 |
| A33 | Fixed-dollar limits over a 27-year backtest | Order and daily limits off; the ST-gain cap scales with the account (fixed-cap sensitivity). | 13.2 |
| A34 | S3 and the annual reset | Not executed in S3; S6 covers it. | 13.3 |
| A35 | S5 per fund or per portfolio, and its sizing | Per fund; restore signalled funds toward target; buy-only. | 13.3 |
| A36 | "Walk-forward" with no fitted parameters | Fresh-start blocks, plus a walk-forward band-width choice. | 13.7 |
| A37 | "Next open" execution | Only where real ETF opening prices exist; the mutual-fund proxies only have NAVs. | 13.2 |
| A38 | "Next h-day return" for the signal | Measured from the signal close and from the execution close; OLS on a dummy with Newey–West errors. | 13.6 |
| A39 | "Amounts invested are arbitrary" (your request) | Phased total, tranche count and a never-invested cash reserve are config; the system acts only on the cash in the account. | 8 |
| A40 | Per-fund limits in the contribution rule (found while building) | Deploy the largest amount that keeps every fund at or below target relative to the core actually produced; closed form on a piecewise-linear function. | 5 |
| A41 | Decimal noise (found while building) | Comparisons that are equalities in exact arithmetic use a 1e-9 USD tolerance; sales that lots fully cover are not flagged as constrained. | 5, 6.6 |
| A42 | Which plan file the guard trusts | The ledger's live run for today and its hash, never a loose `plan.json`. | 10.3 |
| A43 | Bypass and auto modes | Both disabled in project settings, and `disableAllHooks: false` overrides a user-level switch. Explicit `ask` rules prompt for every order in any mode that remains. | 11 |
| A44 | S5 and the wash-sale rule (found while building) | S5's own buy rule respects W2 like every other strategy. | 13.2 |
| A45 | The 1 USD match tolerance and the daily limit (found in review) | The daily total counts the amount actually sent, not the plan line's, so the limit holds to the cent. The tolerance can't be configured above 1 USD. | 11 |
| A46 | Snapshot completeness (found in review) | Positions × quotes + cash must match the broker's reported equity within max(25 USD, 2%), or the plan is blocked. | 3 |
| A47 | Who starts an annual reset | Only you, from a terminal. The agent is denied any command containing `--annual-reset`. A proposal with nothing to sell is dismissed by hand. | 9 |
| A48 | Protecting the guard itself (found in review) | The integrity check runs inside the guard and can't detect a rewritten guard, so `settings.json` denies Claude's file tools on code, hooks, config, state, settings and the virtualenv. | 11 |
| A49 | When a live plan may be made (found in review) | Only inside the execution window (`OUTSIDE_TRADING_WINDOW` otherwise), so a plan made before the open can't use up the day's one live run and then expire. | 10.1 |

## 15. Open questions

Defaults were accepted; the code uses them. Still open, because only you can answer:

1. **MCP tool schemas.** Paste the `place_equity_order` input schema and one real response of
   each read tool (RUNBOOK.md, "Before the first live run"). The guard's parameter parsing and
   the snapshot parser are written against the likely field names and fail closed on anything
   else; real samples turn "likely" into "verified".
2. **Market data for the backtest.** Allow the data hosts in this environment's network
   settings, or run `python -m backtest.compare --download` on your own machine.
3. **Robinhood-side trade approval for MCP orders.** If the agentic account offers it, turn it on.
4. **Agentic account type (Cash or Instant)** and **where the existing 900 USD of equities
   are.** Neither changes the code; both are worth knowing.

## 16. Known limitations

- **No backtest results yet.** Market data can't be downloaded from the environment this was
  built in. The backtester is verified end to end on synthetic data and on fake files in
  Yahoo's exact format, but `backtest/report.md` has not been generated.
- Robinhood hasn't published request and response formats for the MCP tools (as far as I could
  find). The hook and the snapshot parser are written against the likely field names and fail
  closed (deny, stop) on anything else, so the first dry run may stop with a "keys were ..."
  message that needs a small parser update.
- Specific-lot sells through the MCP are unconfirmed. Until they're confirmed, sales relieve
  lots FIFO.
- The wash-sale guard only sees accounts the system can read.
- Live risk numbers describe today's mix over the past 63 and 756 days, not the account's
  realized history.
- **Backtest:**
  - The proxies are imperfect, especially for AVUV.
  - Fund selection has hindsight: QQQM and AVUV are being chosen today, knowing 1999–2026.
  - Provider data quality is a risk.
  - Taxes are simplified.
  - It assumes a run every trading day.
- The guardrails are tamper-evident, not tamper-proof (§11). Hooks can still be turned off from
  outside the project's settings: a `--settings` flag, `--bare`, `--safe-mode`, a
  `.claude/settings.local.json` you edit by hand, or managed settings. Start trading sessions
  with plain `claude` in the project folder (RUNBOOK.md).
- The guard checks the account only when the order names one and `agentic_account_id` is set.
  The agentic account is meant to be the only one the MCP can trade.
- `NOT_PLACED` relies on `get_equity_orders` listing every recent order. Before re-arming a
  line, check the order history in the Robinhood app.
- This cloud environment can't reach Robinhood. Live trading runs on your machine, where the MCP
  OAuth session lives.

## References

- Robinhood Support, "Trading with your agent" and "Tax lots"; Robinhood Newsroom, "Introducing
  tax lots" (2 Dec 2024). Read via search summaries, because this environment can't open
  robinhood.com.
- Claude Code documentation, "Hooks reference": exit-code semantics, `permissionDecision`,
  `onFailure`, `PostToolUse` and `PostToolUseFailure`.
- IRS Publication 550, *Investment Income and Expenses*: holding period, wash sales.
- J.P. Morgan/Reuters, *RiskMetrics Technical Document* (1996): EWMA with λ = 0.94.
- Newey, W. and West, K. (1987, 1994): HAC covariance and the automatic lag rule.

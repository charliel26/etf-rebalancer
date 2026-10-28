# Runbook

How to set up, run, pause, fund and recover the rebalancer. STRATEGY.md says what the rules
are and why; this file says what to type.

Commands below run from the project folder. `rebalancer ...` is short for
`uv run rebalancer ...`.

## Contents

1. [How it fits together](#1-how-it-fits-together)
2. [One-time setup](#2-one-time-setup)
3. [First dry run](#3-first-dry-run)
4. [Before the first live run](#4-before-the-first-live-run)
5. [Dry runs](#5-dry-runs)
6. [Going live](#6-going-live)
7. [Pausing](#7-pausing)
8. [Adding (or holding back) money](#8-adding-or-holding-back-money)
9. [Approvals](#9-approvals)
10. [Annual reset](#10-annual-reset)
11. [When a run fails](#11-when-a-run-fails)
12. [Changing config or code](#12-changing-config-or-code)
13. [Paper trading with Alpaca](#13-paper-trading-with-alpaca)
14. [Backtest](#14-backtest)
15. [Compliance mode](#15-compliance-mode)
16. [Security: what to do and not do](#16-security-what-to-do-and-not-do)
17. [Command and file reference](#17-command-and-file-reference)

## 1. How it fits together

```
you ── claude (Claude Code) ── /rebalance ──► robinhood-trading MCP (read tools)
                │                                    │
                │                  PostToolUse hook saves every response → state/raw/<date>/
                │
                ├─ rebalancer snapshot   raw responses → state/snapshot.json   (code, not the model)
                ├─ rebalancer plan       snapshot + config → plan, report, ledger row (pure code)
                ├─ rebalancer pending    today's live lines still to place
                │
                └─ review_equity_order / place_equity_order for each pending line
                         │
                         PreToolUse guard hook: checks everything, claims the line, or denies
                         Claude Code asks you to confirm each order
```

- The agent never decides an amount. It only places lines that `rebalancer pending` prints,
  and the guard refuses anything that doesn't match one of them.
- **Dry run is the default.** Orders are only possible when you start Claude Code with
  `TRADING_ENABLED=true DRY_RUN=false`, there is no `KILL` file, the project is armed, and it's
  inside the trading window.
- Commands that grant authority (`approve`, `arm`, `rearm`, `dismiss-reset`, and
  `plan --annual-reset`) are for you, in your own terminal. The agent is denied them.

## 2. One-time setup

Everything here happens on your own computer (macOS or Linux), where the Robinhood login
lives. This cloud environment can't reach Robinhood.

**Prerequisites:** git, [uv](https://docs.astral.sh/uv/), and Claude Code 2.1.295 or later
(the guard relies on `"onFailure": "block"`). Check with `claude --version`.

1. **Get the code and its environment.** The hooks run `.venv/bin/python`, so `uv sync` must
   have been run before Claude Code starts.

   ```bash
   git clone https://github.com/charliel26/etf-rebalancer.git ~/etf-rebalancer
   ```

   ```bash
   cd ~/etf-rebalancer && uv sync --extra dev && uv run pytest -q
   ```

   All tests should pass. If any fail, stop there.

2. **Prepare the Robinhood agentic account** (in the Robinhood app; follow Robinhood's
   "Trading with your agent" help article for the account itself).
   - Orders can only go to the dedicated agentic account. Fund it only with money you want
     this system to manage.
   - No margin: a Cash account, not Instant or Gold. The planner also never spends more than
     the smaller of cash and buying power.
   - Turn **dividend reinvestment off**. A reinvested dividend is a purchase the wash-sale
     guard can't see coming.
   - Don't trade in this account by hand, and don't pick tax lots by hand. The planner assumes
     Robinhood relieves lots first-in-first-out (its default).
   - If Robinhood offers its own approval or confirmation for agent orders, turn it on.
   - Any position already in the account should be a core fund (VTI, VXUS, QQQM, AVUV) or be
     listed in `satellite_symbols`. Anything else is reported as `UNMANAGED_POSITION`, never
     traded.

3. **Register the MCP server** with the default *local* scope, from inside the project folder.
   Local scope means the Robinhood tools exist only in this folder, where the guard hook is.
   Never add it with `--scope user`.

   ```bash
   cd ~/etf-rebalancer && claude mcp add --transport http robinhood-trading https://agent.robinhood.com/mcp/trading
   ```

4. **Log in (OAuth).** Start Claude Code in the project folder and accept the workspace-trust
   prompt (the project's allow rules only apply after it). Then type `/mcp`, pick
   `robinhood-trading`, choose **Authenticate**, and finish the Robinhood login in the browser.
   Check it from a shell:

   ```bash
   claude mcp list
   ```

   `robinhood-trading` should show `✔ Connected`. To log out later: `claude mcp logout robinhood-trading`.

5. **Set your config by hand** in your editor (Claude Code is denied write access to
   `config/`). The defaults are the spec's. At minimum, decide on money (§8 below):
   `cash.cash_reserve_usd` and `initial_deployment` in `config/targets.yaml`. If you know the
   agentic account's number, set `agentic_account_id` in `config/limits.yaml`.

6. **Arm.** This records the hashes of every file that decides or guards an order. The guard
   refuses all orders until it's done, and again after any of those files changes.

   ```bash
   uv run rebalancer arm
   ```

   It lists what changed since the last arming and asks you to type `ARM`. Then:

   ```bash
   uv run rebalancer status
   ```

   It should say `armed: yes`, `DRY_RUN=(unset: dry run)` and `kill switch: off`.

## 3. First dry run

The MCP's response formats aren't published, so the parsers in
`src/rebalancer/broker/robinhood_raw.py` are written against the likely field names and stop on
anything else. The first dry run is where that gets checked.

1. In the project folder, with no trading variables set:

   ```bash
   claude
   ```

2. Type `/rebalance`. The agent runs `rebalancer status`, calls the read tools (no prompts;
   a hook saves each response under `state/raw/<date>/`), then `rebalancer snapshot`, `plan`
   and `report`. Nothing can be ordered.
3. Possible stops and what they mean:
   - `snapshot` fails with `... none of [...] present; keys were [...]`: a response has
     different field names. Fix the parser (§12) using the saved file in `state/raw/<date>/`.
     If you want help, share that file with the account number removed.
   - `missing captured responses for: [...]`: the agent skipped a tool or the call failed.
     Run `/rebalance` again.
   - Blocker `EQUITY_MISMATCH`: positions × quotes + cash don't match the equity Robinhood
     reports. Usually a missing position or a misread field. Don't go live until it's explained.
4. Compare the report with the Robinhood app: cash, every position's shares and value, and the
   tax lots. They should agree.

## 4. Before the first live run

1. **Check the order tools' parameters.** Ask Claude Code to show the input schemas of
   `mcp__robinhood-trading__place_equity_order` and `review_equity_order` (that reads the tool
   definitions; it calls nothing). The guard accepts only the spellings in `FIELDS` in
   `src/rebalancer/guard.py`: symbol, side, a dollar amount, order type, time in force,
   extended hours, trading session, account and a reference id. It refuses any order with a
   parameter it doesn't know. If the real names differ, update `FIELDS` and its tests (§12).
2. **Check how a placed order reports its id**, after the first live order:
   `rebalancer reconcile` should move the line to `SUBMITTED` or `FILLED`. If lines stay
   `CLAIMED` and then become `NOT_PLACED` although the order exists, the id field differs from
   what `find_order_id` in `src/rebalancer/broker/mcp_payload.py` expects.
3. **Start small.** Make the first live run a contribution of, say, 50–100 USD, so a surprise
   costs little.
4. **Know the brakes** (§7): `touch KILL` stops all orders at once.

## 5. Dry runs

Use these any time, for example after a deposit, to see what a live run would do.

```bash
claude
```

Then `/rebalance`. The report (`rebalancer report`, or `state/reports/<run_id>.md`) shows:

- portfolio, weights, bands and projected weights after buys;
- the orders a live run would place;
- flags, blockers and the planner's checks;
- the tax estimate and the report-only risk numbers.

Dry runs are unlimited and never block a later live run.

## 6. Going live

Trading window: 09:35 to 15:55 New York time on NYSE trading days, ending 5 minutes before an
early close. One live run per trading day. A live plan is only made inside the window, so
starting too early costs nothing: the plan comes out as a dry run, and you can try again later.

```bash
cd ~/etf-rebalancer && TRADING_ENABLED=true DRY_RUN=false claude
```

Then `/rebalance`. What happens:

1. Status, read tools, snapshot and a **live** plan (if there are no blockers). The plan is
   registered in the ledger as today's live run.
2. `rebalancer pending` prints the lines to place, sells first. When it prints `[]` it adds a
   `#` line saying why: a dry-run session, the kill switch, a plan older than 60 minutes, or a
   missing approval.
3. For each line the agent calls `review_equity_order`, then `place_equity_order`. **Claude
   Code asks you to confirm every order.** Before you approve, check that the symbol, side and
   dollar amount are exactly one of the pending lines. If anything looks off, decline; nothing
   is placed.
4. The guard checks the call (integrity, switches, window, plan, approval, limits, wash sales,
   review) and claims the line atomically. The agent stops at the first refusal or error.
5. `get_equity_orders` and `rebalancer reconcile` record what the broker did.

If the session ends halfway (closed terminal, lost connection), start the same way again within
60 minutes of the plan's creation and run `/rebalance`: its plan step reports that today's live
run exists, and `pending` lists only the lines not yet placed.

## 7. Pausing

| To... | Do this |
|---|---|
| Stop all orders now, from any terminal | `touch KILL` in the project folder |
| Resume | `rm KILL` |
| Run without any chance of orders | start `claude` without the two variables (dry run) |
| Pause for weeks or months | leave a `KILL` file; dry runs still work |
| Cancel an open order | in the Robinhood app, or ask the agent to call `cancel_equity_order` (you'll be asked to confirm) |

`KILL` is checked when planning and again by the guard for every order. Nothing runs on a
schedule, so between your sessions the system does nothing at all.

## 8. Adding (or holding back) money

**Amounts are arbitrary.** The system only looks at the cash in the agentic account. Deposit any
amount, at any time.

**What a run deploys.** With cash $K$ (the smaller of cash and buying power) and the amount held
back $R_t$, the deployable cash is $K - R_t$, invested only when it's above
`min_cash_to_deploy_usd` (25 USD). The contribution rule (STRATEGY.md §5) spreads it so that no
fund is pushed above its target:

$$s_i = \max\bigl(0,\ w^{\star}_i (V + C) - V_i\bigr) \qquad b_i = C \cdot \frac{s_i}{\sum_j s_j}$$

Per run, at most 1,500 USD goes into one fund and 3,000 USD is traded in total. The rest of the
cash waits for the next run (flags `DAILY_LIMIT_CONTRIBUTION` and
`CASH_WAITING_FOR_LIMITED_FUNDS`). A 6,000 USD deposit into an empty account takes two runs.

**Keeping part of the cash uninvested.** Set `cash.cash_reserve_usd` in `config/targets.yaml`.
That amount is never invested.

**Phasing in a lump sum.** Set `initial_deployment` in `config/targets.yaml`:

```yaml
initial_deployment:
  start_date: 2026-11-02   # first tranche date
  total_usd: "8100"        # any amount
  tranches: 3              # monthly: start, +1 month, +2 months
```

Each run holds back the tranches not yet released:

$$R_t = R_{\text{cfg}} + K_0 \cdot \frac{n_t}{n} \qquad n_t = \text{number of tranche dates after today}$$

where $K_0$ is `total_usd`, $n$ is `tranches` and $R_{\text{cfg}}$ is `cash_reserve_usd`. So
8,100 USD in 3 tranches releases 2,700 USD on the start date and 2,700 USD a month later and
again two months later. Deposits on top of $K_0$ are invested right away.

**Example: 9,000 USD in the account, invest 6,000 USD over three months, keep 3,000 USD:**

```yaml
cash:
  min_cash_to_deploy_usd: "25"
  cash_reserve_usd: "3000"
initial_deployment:
  start_date: 2026-11-02
  total_usd: "6000"
  tranches: 3
```

If the account holds less than the amount held back, nothing is invested and the report says
`CASH_BELOW_RESERVE`; lower the reserve or `total_usd` to match what you actually deposited.

**After editing the config, re-arm** (`uv run rebalancer arm`), or the guard refuses orders.

**Raising the limits as the account grows.** Edit `max_order_usd` and
`max_daily_notional_usd` in `config/limits.yaml` (the daily limit must be at least the order
limit), then re-arm.

**Taking money out.** The system never sells to raise cash. Withdraw from the reserve, or raise
`cash_reserve_usd` first so new deposits stop being invested. If you sell by hand, the system
sees the sale in `get_equity_orders` but can't know its gain, so it treats it as a loss sale
and won't buy that fund (or its substantially identical group) for 30 days.

## 9. Approvals

A live plan needs your approval when:

- a band rebalance, already reduced to the band edge, would still realize more than 100 USD of
  net short-term gain (`ST_CAP_EXCEEDED`);
- it's an annual reset (§10);
- compliance mode requires approval for every plan with orders (§15).

The agent's `rebalancer pending` then prints `[]` and says the plan needs approval. In your own
terminal:

```bash
uv run rebalancer approve <run_id>
```

It shows every order and the estimated short- and long-term gains, then asks you to type the
first 8 characters of the plan's hash. The approval covers that exact plan, today only. Then, in
the live Claude Code session, run `/rebalance` again (within 60 minutes of the plan's creation):
`pending` now lists the lines.

If you don't approve, nothing happens. The next trading day's run plans again from scratch.

## 10. Annual reset

From 15 January (configurable), the daily report says **Annual reset is due**. The reset sells
every overweight fund back to target, whether or not a band is breached, with the same lot
logic. The proceeds are reinvested by the contribution rule at the next run. It always needs your
approval, and only you can start it, from a terminal.

On a trading day, inside the window, and before any live run that day:

1. **Fresh data:** `claude` (dry run), then `/rebalance`. This captures today's prices and
   positions. Exit Claude Code.
2. **The proposal**, within 15 minutes (prices go stale after that):

   ```bash
   TRADING_ENABLED=true DRY_RUN=false uv run rebalancer plan --annual-reset
   ```

   It prints the sell lines and becomes today's live run. Read the report
   (`uv run rebalancer report`) for the estimated realized gains.
3. **Approve**, if you want it: `uv run rebalancer approve <run_id>`.
4. **Execute**, within 60 minutes of step 2: `TRADING_ENABLED=true DRY_RUN=false claude`, then
   `/rebalance`. The plan step says today's live run already exists, and `pending` lists the
   reset's lines.
5. Over the next runs, the contribution rule reinvests the settled proceeds.

To skip this year: `uv run rebalancer dismiss-reset 2027` (the year). If the proposal has no
sales, the core is already on target; dismiss it.

## 11. When a run fails

Nothing is retried automatically. Find the cause, then decide.

**Plan blockers** (shown in the plan summary and report; any blocker makes the plan dry-run):

| Blocker | Meaning | What to do |
|---|---|---|
| `KILL_SWITCH` | `KILL` file present | `rm KILL` if you mean to trade |
| `OUTSIDE_TRADING_WINDOW` | a live plan was requested outside 09:35–15:55 ET on a trading day | run again inside the window (no live run was used up) |
| `LIVE_RUN_EXISTS_TODAY` | today's live plan already exists | nothing; `pending` still lists its unplaced lines |
| `OPEN_ORDERS` | an order is still open at the broker | wait for it to fill, or cancel it in the app |
| `STALE_QUOTE:<sym>` / `MISSING_QUOTE:<sym>` | price older than 15 minutes, or missing | run during market hours; quote every held symbol |
| `SNAPSHOT_NOT_FROM_TODAY` | old snapshot | run `/rebalance` again |
| `EQUITY_MISMATCH` | positions × quotes + cash ≠ reported equity | compare the report with the app; usually a parsing problem |
| `NEGATIVE_CASH` | broker reports negative cash | resolve in the app |
| `LOT_METHOD_NOT_SUPPORTED_LIVE` | `lot_method: tax_aware` in a live run | set `fifo` (STRATEGY.md §7.3) |
| `CHECK_FAILED:<name>` | a planner invariant failed | don't trade; this is a bug to report |

**Guard refusals** (the agent reports `rebalancer guard: ...`):

| Message starts with | What to do |
|---|---|
| `integrity check failed` / `not armed` | review the listed files; if the change is yours, `uv run rebalancer arm` |
| `TRADING_ENABLED is not 'true'` / `DRY_RUN is not 'false'` | restart Claude Code with both variables |
| `outside the execution window` / `not an NYSE trading session` | try again in the window |
| `no live run is registered for today` | run `/rebalance` in a live session first |
| `plan is older than 1:00:00` | today's live plan can't be executed any more; it will be planned again on the next trading day |
| `plan requires approval` | §9 |
| `no matching review_equity_order` | the agent must review the exact order first; run `/rebalance` again |
| `... matches 0 pending plan lines` | that line was already claimed or placed, or the amount differs from the plan |
| `daily notional limit` | today's limit is used up |
| `wash-sale guard` | the fund was sold at a loss within 30 days; it can't be bought yet |
| `... is not on the robinhood-trading allowlist` | the agent tried a tool the system never uses |
| `guard error, failing closed` | a bug or a broken environment; check `uv sync`, then report it |

**Line outcomes** (from `rebalancer reconcile` or `rebalancer status`):

| Status | Meaning | What to do |
|---|---|---|
| `SUBMIT_FAILED` | the order call returned an error | check the order history in the app; if there is no order, re-arm the line |
| `REJECTED` | the broker rejected the order | read the reason in the app; re-arm only once the cause is fixed |
| `NOT_PLACED` | claimed, but no order showed up within 5 minutes (e.g. you declined the prompt) | check the app; re-arm if you want it placed |
| `CANCELLED` / `PARTIAL` | cancelled or partly filled | nothing; the next run's rules pick up any difference |
| `SUBMITTED` for hours | still open at the broker | the next plan is blocked by `OPEN_ORDERS` until it fills or you cancel it |

**Re-arming a line** makes it `PENDING` again. Before you do it, check in the Robinhood app that
no order exists for it:

```bash
uv run rebalancer rearm <run_id> <line_id>
```

Then run `/rebalance` in a live session, within 60 minutes of the plan's creation. After that,
the line waits for the next trading day's plan.

**Hook problems.** The capture hook never blocks; its errors go to `state/hook_errors.log`. If
snapshots are missing data, look there first. The guard hook blocks on any error of its own, so
a broken environment means no orders, not unchecked orders.

## 12. Changing config or code

**Config** (`config/*.yaml`): edit by hand in your editor, run `uv run pytest -q`, then re-arm.
`rebalancer arm` lists exactly which protected files changed.

**Code.** In this folder Claude Code is denied write access to the code, hooks, config,
state, settings and virtualenv; that keeps a trading session from editing its own guard.
Develop in a separate clone where the Robinhood MCP isn't registered, with the project lock
turned off for that session only:

```bash
git clone https://github.com/charliel26/etf-rebalancer.git ~/etf-rebalancer-dev
```

```bash
cd ~/etf-rebalancer-dev && uv sync --extra dev --extra backtest && claude --setting-sources user
```

Test (`uv run pytest -q`), commit and push there. Then bring the change into the trading folder
and re-arm:

```bash
cd ~/etf-rebalancer && git pull && uv sync --extra dev && uv run pytest -q
```

```bash
uv run rebalancer arm
```

Never use `--setting-sources`, `--settings`, `--bare` or `--safe-mode` in the trading folder:
they can switch off the guard hook while the Robinhood tools are connected.

## 13. Paper trading with Alpaca

The same planner and guard, with no language model: Python places the orders.

1. Create an Alpaca **paper** account and API keys. In the paper account settings, set the
   margin multiplier to 1 and turn shorting off.
2. The project must be armed (§2). Run inside the trading window:

   ```bash
   APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... TRADING_ENABLED=true DRY_RUN=false uv run rebalancer paper-run
   ```

   Without the two trading variables it's a dry run. It keeps its own ledger in `state/paper/`
   and only ever talks to Alpaca's paper endpoint. Alpaca has no tax-lot data, so lots are
   rebuilt first-in-first-out from fills.

## 14. Backtest

Research only; nothing here trades. The parameters are fixed in `backtest/config.yaml` before
any data is loaded (STRATEGY.md §13).

```bash
uv sync --extra dev --extra backtest
```

```bash
uv run python -m backtest.compare --download
```

That downloads daily histories from Yahoo Finance and the 3-month T-bill rate from FRED into
`backtest/data/raw/` (not committed), records hashes in `backtest/data/manifest.json`, and writes
`backtest/report.md` with figures. It can take several minutes. Later runs on the cached data
skip `--download`.

`uv run python -m backtest.compare --synthetic` runs the whole pipeline on generated prices to
check the plumbing. Its output, in `backtest/out/synthetic/`, says on every page that it isn't a
backtest result.

## 15. Compliance mode

For a future employer's personal-trading rules, in `config/limits.yaml`:

```yaml
compliance:
  min_holding_days: 30          # lots younger than this are never sold
  approval_for_all_trades: true # every plan with orders needs your approval
```

With approval required for all trades, `approve` also needs the pre-clearance reference, which is
stored with the approval:

```bash
uv run rebalancer approve <run_id> --reference PC-12345
```

Pre-clear the orders with your employer *before* approving. The approval is only valid that day,
so pre-clear and trade on the same day. Re-arm after changing the file.

## 16. Security: what to do and not do

**Do:**

- Start trading sessions with plain `claude` (plus the two variables) in the project folder.
- Read every order confirmation and every unexpected permission prompt. The project allows only
  the read tools, the order review and the read-only `rebalancer` commands without asking.
- Keep the agentic account funded only with what the system manages.
- Run `rebalancer status` at the start of a session: it shows the switches and whether the
  armed hashes still match.

**Don't:**

- Don't register the Robinhood MCP with `--scope user`, or in any folder but this one.
- Don't start Claude Code here with `--dangerously-skip-permissions`, `--settings`,
  `--setting-sources`, `--bare` or `--safe-mode`.
- Don't create `.claude/settings.local.json` rules that allow `place_equity_order` or turn hooks
  off.
- Don't approve Bash commands that mention `approve`, `arm`, `rearm`, `KILL`, `state/` or
  `script`; the agent has no business running them.

**What protects what** (details in STRATEGY.md §11):

| Layer | Stops |
|---|---|
| Planner (pure code, tested) | any trade outside the rules; limits; wash sales; stale or inconsistent data |
| Guard hook (`PreToolUse`) | any order that doesn't match a pending line of today's checked, armed, approved plan |
| Claude Code permissions | unneeded Robinhood tools (denied); orders without your confirmation (ask); the agent editing code, settings or state |
| Integrity manifest | trading with edited config, code, hooks, settings or dependencies until you re-arm |
| The agentic account | anything beyond the money in it |

These stop mistakes and prompt injection. They don't stop a process that runs with your own
shell rights.

## 17. Command and file reference

| Command | Who | What it does |
|---|---|---|
| `rebalancer status` | agent or you | switches, arming, config summary, today's live lines |
| `rebalancer snapshot` | agent or you | builds `state/snapshot.json` from today's captured responses |
| `rebalancer plan` | agent or you | plans; live only with both variables set and no blockers |
| `rebalancer plan --annual-reset` | you | annual-reset proposal (§10) |
| `rebalancer report [run_id]` | agent or you | prints a report (latest by default) |
| `rebalancer pending` | agent or you | today's live lines still to place, as JSON |
| `rebalancer reconcile` | agent or you | updates lines from the captured `get_equity_orders` |
| `rebalancer approve <run_id> [--reference R]` | you, terminal | approves today's live plan |
| `rebalancer arm` | you, terminal | trusts the current protected files |
| `rebalancer rearm <run_id> <line_id>` | you, terminal | makes a failed line pending again |
| `rebalancer dismiss-reset <year>` | you, terminal | skips that year's annual reset |
| `rebalancer paper-run` | you | snapshot, plan and execution against Alpaca paper |

| Path | Contents |
|---|---|
| `config/targets.yaml`, `config/limits.yaml` | strategy and hard limits (edit by hand, then arm) |
| `.claude/settings.json` | permissions and hook registration |
| `.claude/commands/rebalance.md` | the agent's procedure (`/rebalance`) |
| `hooks/guard_order.py`, `hooks/capture_mcp.py` | the guard and capture hooks |
| `state/armed.json` | armed file hashes |
| `state/ledger.sqlite3` | runs, lines, approvals, trades, resets, event log |
| `state/raw/<date>/` | every Robinhood response, verbatim |
| `state/snapshot.json` | the parsed snapshot |
| `state/plans/<run_id>.json`, `state/plan.json` | every plan; the latest one |
| `state/reports/<run_id>.md` | every report |
| `state/hook_errors.log` | capture-hook errors |
| `state/paper/` | Alpaca paper trading's own ledger and plans |

`state/` holds account data and is never committed. Back it up: the ledger is the record of what
the system did.

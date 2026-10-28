---
description: Run the ETF rebalancer. Dry run unless trading is explicitly enabled.
---

You are executing a deterministic plan, not making decisions. Never invent,
round, change or combine numbers. Never place an order that is not a line
printed by `rebalancer pending`. If anything looks wrong, stop and report it.
Approving, arming, re-arming, dismissing a reset and planning an annual reset
are for the person, in their own terminal: never try to run them.

1. Run `uv run rebalancer status` and show the output.
2. Call these robinhood-trading tools (a hook saves every response; you do not
   need to copy any numbers):
   - `get_portfolio`
   - `get_equity_positions`
   - `get_equity_quotes` for VTI, VXUS, QQQM, AVUV and every other symbol held
   - `get_equity_tax_lots` for VTI, VXUS, QQQM, AVUV
   - `get_equity_tradability` for VTI, VXUS, QQQM, AVUV
   - `get_equity_orders` (orders from the last 45 days)
   - `get_equity_historicals`: daily closes, about 3 years, for every symbol held
3. Run `uv run rebalancer snapshot`, then `uv run rebalancer plan`, and show the report
   (`uv run rebalancer report`).
4. Run `uv run rebalancer pending`. It prints a JSON list of the lines you may place
   today. With nothing to place it prints `[]`, and a line starting with `#` says why
   (dry run, kill switch, expired plan, approval needed). If the list is empty, stop
   here and summarize, including that reason.
5. For each line in that list, in order (sells come first):
   a. Call `review_equity_order` with exactly that line's symbol, side and dollar
      amount (`notional_usd`): a market order sized in dollars, regular hours only,
      good for day.
   b. If the review shows no problem, call `place_equity_order` with exactly the
      same values.
   Stop at the first refusal, warning or error and report it. Do not retry.
6. Call `get_equity_orders`, run `uv run rebalancer reconcile`, and show the result.

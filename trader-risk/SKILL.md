---
name: trader-risk
description: >-
  Risk Manager for NSE. Checks every trade against your limits and stops
  anything over them, before it can reach the broker. For a day trade: a
  stop is required, the loss at the stop plus the round-trip charges (STT,
  brokerage, exchange, SEBI, stamp duty, GST) must fit your max loss per
  trade (measured from the higher of the plan's entry and a fresh price),
  reward:risk must meet your minimum, the day's total risk must fit your
  daily loss limit, no more than your trades per day, and no new entries
  outside your entry window or when the market is closed. It also keeps the
  basics: blocked symbols, order size, position cap, cash buffer, margin,
  short sales, F&O, malformed smart orders, prices off the tick, duplicates
  and a daily order limit. Getting out is never blocked by a day limit.
  Prints PASS or BLOCK with the reason and records the verdicts so
  trader-broker only shows an order that passed. Use when the user says
  "check my order", "is this safe", "risk check", "does this break my
  rules", "can I take this trade", "how much have I risked today", or before
  /trader-broker, always.
argument-hint: "[path to orders.json]"
---

# trader-risk

The Risk Manager is the gate between a proposal and the broker. Every order
written by trader-budget or trader-broker's flatten passes through it. A
BLOCK is final for that order: it does not go to the user for a yes, and it
does not go to the broker. Most people who day trade lose money (SEBI's own
study: 7 in 10 intraday traders lose). These limits keep each loss and each
day's losses small. They do not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py"                           # trader/ files; writes trader/state/risk.json
python3 "${CLAUDE_SKILL_DIR}/check.py" --json
python3 "${CLAUDE_SKILL_DIR}/check.py" --no-write                # look without recording
python3 "${CLAUDE_SKILL_DIR}/check.py" --now "2026-11-03 10:01"  # pin the clock (IST)
```

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget, or trader-broker's flatten) |
| `trader/config.json` | the limits (trader-budget ships `config.example.json`), including the charge rates |
| `trader/portfolio.json` | cash and holdings; if missing, a fresh account funded with the budget |
| `trader/prices.json` | a fresh price raises the entry it measures risk from; also values holdings |
| `trader/state/broker.json` | what was placed today: duplicates, trades per day, daily loss |
| `trader/state/risk.json` | the verdicts it writes, with a fingerprint of each exact order |

## Entries and exits

An **entry** opens or adds to a position: a buy, or a sell that would go
short. An **exit** only reduces what you already hold. Getting out must
always be possible, so an exit is checked for format, asset and short sales
only. Every other rule (day limits, entry window, loss, reward:risk, blocked
list, size, duplicates) turns into a warning for an exit, never a BLOCK.

## The checks

Orders are checked in file order, as if each passed order had filled, so a
batch that is fine order by order but breaks a limit together is caught.

| check | blocks when |
| --- | --- |
| format | no symbol, txn_type not BUY or SELL, no whole-share qty (INDstocks takes no rupee-notional orders), unknown order_type / product / validity / segment / exchange, a LIMIT order with no limit_price, a stop-loss or target leg without its limit price or with the limit on the wrong side of its trigger, a price off the stock's tick |
| asset | an option or futures symbol, or segment DERIVATIVE, while `allow_fno` is false; a commodity; anything that is not an NSE equity symbol |
| short | a sell of more than you hold (`allow_short` false) |
| blocked | (entries) the symbol is on the `blocked` list |
| size | (entries) under `min_order_inr` or over `max_order_inr` |
| position | (entries) the stock would be over `max_position_pct` of the account |
| cash | (entries) the buy would leave less than the cash buffer, or need margin: MIS leverage is not used while `allow_margin` is false, the trade is fully funded |
| stop | (entries) no stop while `require_stop` is true (a smart order's `sl_trigger_price`, or the order's plan stop); a stop on the wrong side of the entry |
| loss | (entries) qty x (entry minus stop) PLUS the round-trip charges over `max_loss_per_trade_inr` |
| reward:risk | (entries) (target minus entry) / (entry minus stop) under `min_reward_risk` |
| daily loss | (entries) the max losses at the stops of today's entries, charges included, plus this one, over `max_daily_loss_inr` |
| trades | (entries) `max_trades_per_day` entries already placed today |
| window | (entries) before `no_entries_before` or after `no_entries_after`, or the market is closed (weekend, NSE holiday, outside 09:15 to 15:30, the Muhurat session) |
| duplicate | (entries) the same symbol and side twice in one batch, or already placed today |
| daily limit | (entries) `max_orders_per_day` orders already placed today |

How the numbers work:

- **The entry it measures from**: the limit price for a limit order, the
  trigger limit for a trigger order. For a market order, the HIGHER of the
  plan's entry and a fresh price from `trader/prices.json` (under 15 minutes
  old). A plan written at 10:00 cannot understate the risk at 10:30 after
  the price ran up. An older price is not used, and it says so.
- **Charges** are the whole round trip, buy and sell, at the stop: brokerage
  (INDstocks, a flat Rs 10 an order), STT (0.025 % of the sell), NSE
  transaction charge (0.00307 % both sides), SEBI fee and IPFT (Rs 10 a
  crore each), stamp duty (0.003 % of the buy) and 18 % GST on brokerage,
  exchange and SEBI charges. Rates as published on zerodha.com/charges, read
  2026-11-04; change them under `charges` in the config when they change.
- **Daily loss** counts the full max loss, charges included, of every entry
  placed today, win or lose, as trader-broker logged it. Three full-size
  trades use up a Rs 3,000 day.
- **The window** is IST, worked out in the tool, never from the computer's
  clock zone. Weekends and NSE holidays are closed. Set both `no_entries_*`
  to null to switch the window off.
- **No pattern-day-trader rule.** India has none. What it has instead is the
  broker's MIS square-off from about 15:20: `flat_by` (15:15) is the plan's
  own exit, earlier than the broker's.

Warnings do not block, but say them to the user: a LIVE account, a price
sent as a string instead of a number, a stop that lives only in the plan and
not at the broker, a price too old to use, an after-market order.

## The paper-month limits

Two checks that are off until the config sets them (`paper.example.json`
in trader-budget turns them on):

- `breadth_gate_pct` (65) with `gate_time` (10:00): an entry needs today's
  `trader/state/breadth.json` from `scan.py breadth`, read at or after the
  gate time, with the gate open. No reading, an old reading or a closed gate
  blocks it, and the reason names the reading.
- `stop_after_loss`: once a trade closes at or under zero before charges
  today (a sell in `trader/paper.json` with a `pnl`), every further entry is
  blocked for the day. Live fills are not read yet; with `"paper": false` the
  tool says so.
- `max_charges_pct_of_risk`: the cost check. Round-trip charges at the
  stop over that share of the rupees at risk block the entry ("cost: ...").
  At Rs 10,000 a trade the charges are about Rs 27, so a 15 % cap needs
  about Rs 180 at risk: a stop of 2.4 % on a Rs 7,500 position. Tight stops
  on a small account are mostly charges.
- `warn_charges_pct_of_risk` (15 in the paper config): the same check, logged
  instead of blocking ("cost (logged, not blocking): ... would have blocked
  it"). The paper config uses this one and leaves `max_charges_pct_of_risk`
  null: the opening-range stops are mostly under 1 %, so a blocking 15 % cap
  stops almost every paper trade (it would have blocked 410 of the rule's 476
  train signals) and the 200 paper trades could never come. The replay reads
  the same keys from the same config.

## The journal and the review

```bash
python3 "${CLAUDE_SKILL_DIR}/check.py" journal --note "took the second breakout late; the gate had closed"
python3 "${CLAUDE_SKILL_DIR}/check.py" review --days 7
```

`journal` writes `trader/journal/DATE.json` from the day's files: the
breadth reading, the scan, the entry, the plan, the Time Machine's verdict
and history, the risk verdicts, every paper fill with its result, the day's
trades (closed, won, lost, gross, charges, net), the evening replay's summary
and baseline from `trader/replays/DATE.json`, and your notes, which
accumulate across runs. Each closed paper trade is kept with its net and its
rupee risk (`paper_trades`), and the replay's ledger trades with theirs.

`review` rolls the journals up over the last N days: trades and net, the
gate's open days, the replay's rule against its baseline, results by symbol,
then the metrics for the paper trades and for the replay rule's ledger: net
R a trade, expectancy (net rupees a trade) with its 95 % range, rupees won
over rupees lost, max drawdown and net by week.

The last line is the go/no-go, decided 2026-11-16 and tightened 2026-11-17,
before any real money: on the held-out replay days (`timemachine.py multiday
--write trader/replays/multiday.json`) every signal of the rule, on at least
10 days, with the low end of its by-day 95 % range above zero; AND net
expectancy above zero on at least 200 paper trades since `paper_start`.
Until both hold it prints NOT MET and why.

## Steps

1. If the price may have moved since the plan, refresh it first
   (trader-alerts: `alerts.py quote TATACONSUM --write`).
2. Run `check.py`. Exit code 0 means every order passed, 1 means at least one
   was blocked.
3. Tell the user the result in plain words, order by order: the max loss at
   the stop with its charges, reward:risk, and how much of today's limits it
   uses. For a BLOCK, give the reason and the fix (fewer shares, a closer
   stop from trader-exit, wait for the window, or skip the trade). Never
   change a limit yourself.
4. If the order changes in any way, run the check again. trader-broker matches
   each order to its verdict by fingerprint, so an edited order is not ready
   until it passes again, and a verdict from yesterday does not count today.

## Example: TATACONSUM, Tue Nov 3 2026

The smart order from trader-budget at 10:01, on a fresh Rs 100,000 paper
account with the example config (71 shares, bound by the 75 % position cap):

```
PASS  #1 BUY TATACONSUM 71 shares, INTRADAY, entry
          entry 1050.20 (the plan's entry), stop 1039.40, target 1061.00: risk 10.80 a share
          max loss Rs 766.80 + Rs 50.01 charges = Rs 816.81 (limit Rs 1,000), reward:risk 1.00 (min 1)
          today before this: 0 of 3 trades, Rs 0 of Rs 3,000 daily loss

1 passed, 0 blocked at 2026-11-03 10:01 IST. Cash after the passed orders: Rs 25,435.80 (paper account).
```

One share over the cap (72) and it is blocked on position and on the max
order. With the cap, the max order and the cash buffer lifted, 88 shares are
blocked on the loss alone: 88 x 10.80 = Rs 950.40 at the stop, plus Rs 56.33
charges = Rs 1,006.73, over the Rs 1,000 max loss; 87 shares (Rs 995.56)
pass. The same 71 shares checked at 10:30 with a fresh quote of 1,058.00 in
`trader/prices.json` are measured from 1,058.00: 18.60 a share of risk,
Rs 1,320.60 at the stop plus charges, reward:risk 0.16, and Rs 75,118 of
stock, so four reasons block it. At 09:29 they are blocked by the window (no
new entries before 09:30). A 15:10 sell that closes the position passes,
even though it is after the 15:00 entry cutoff: it is an exit.

## Rules

- A PASS only means the order is allowed to be shown to the user. It is not a yes.
- Every number comes from the config, the order, the portfolio, the prices or the broker log.
- INDstocks prices can lag; the exchange's price is the one that fills, and a stop can fill
  worse than its trigger in a fast market, or not at all through a circuit halt.

## What it never does

It never places an order, never edits an order to make it pass, never
loosens a limit, never blocks a sell that only gets the user out, and never
treats a PASS as the user's yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, F&O, margin beyond MIS intraday, short selling or leverage unless the config allows it and the user confirms.
- Broker tokens live in environment variables or a .env file, never in the chat.

---
name: trader-budget
description: >-
  Budget Manager for NSE. Sizes every position to your budget and your max
  loss per trade, charges included. Reads the trade plan from trader-exit
  (entry, stop, target, tick), finds the whole shares that fit inside four
  limits (max loss per trade net of STT, brokerage, exchange, SEBI, stamp
  duty and GST; position cap; max order; cash after your buffer), says which
  limit decided it, and writes ONE smart order (entry with stop-loss and
  target legs) to trader/orders.json in the exact shape of INDstocks'
  POST /smart/order. A stop too far away for your max loss is a plain
  refusal, not a smaller order. Nothing is placed. Use when the user says
  "how many shares", "size this trade", "how much should I buy", "I have 1
  lakh", "I only want to risk 1,000 a trade", "set my rules", "keep 10% in
  cash", or after /trader-exit writes a plan.
argument-hint: "[budget in rupees]"
---

# trader-budget

The Budget Manager turns a plan ("buy TATACONSUM at 1,050.20, stop
1,039.40") into an exact order ("buy 71 shares"), inside rules the user sets
once. The stop decides the size: the further away the stop, the fewer
shares. Charges are part of the loss, so they are part of the size. It
writes a proposal only. trader-risk checks it and trader-broker asks the user
yes or no. Day trading loses most people money (SEBI's own study: 7 in 10
intraday traders lose). Sizing to a max loss keeps each loss small; it does
not make a trade win.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py"                        # trader/plan.json + trader/config.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --write                # write trader/orders.json
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit --write        # a limit order at the plan's entry
python3 "${CLAUDE_SKILL_DIR}/size.py" --limit 1050.00 --json # a limit order at 1050.00
python3 "${CLAUDE_SKILL_DIR}/size.py" --weights HDFCBANK=40,RELIANCE=60 --prices trader/prices.json   # weights mode, below
```

`--now "2026-11-03 10:00"` pins the clock (IST). `--master FILE` fills in the
INDstocks security_id from a saved instrument master (trader-broker resolves
it otherwise). A plan for another day is refused: make a new one with
trader-exit.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | the rules (start from `config.example.json` in this folder) |
| `trader/plan.json` | the trade: entry, stop, target, tick, setup, flat-by time (trader-exit writes it) |
| `trader/prices.json` | optional: a fresh price above the plan's entry means a bigger risk (trader-alerts writes it) |
| `trader/portfolio.json` | optional: its cash caps the size (trader-broker keeps it) |
| `trader/orders.json` | the one proposed smart order it writes with `--write` |

## Step 1: set the rules once

If `trader/config.json` does not exist, copy `config.example.json` into
`trader/config.json` and ask the user for each value in plain words:

- **budget**: how much money this is for, in rupees. Only money they can afford to lose.
- **cash_buffer_pct**: how much always stays in cash (default 10).
- **max_loss_per_trade_inr**: the most one trade may lose if the stop fills at the stop,
  charges included (default 1,000: 1 % of a 1 lakh budget).
- **max_position_pct**, **max_order_inr**, **min_order_inr**: the biggest position (75% of the
  budget), the biggest order (Rs 75,000) and the smallest order (Rs 500).
- The day limits trader-risk enforces: **max_daily_loss_inr** (3,000), **max_trades_per_day** (3),
  **min_reward_risk** (1.0), **require_stop** (true), **no_entries_before** / **no_entries_after**
  ("09:30" / "15:00") and **flat_by** ("15:15"), all IST.
- **product**: INTRADAY (MIS, squared off by the broker from about 15:20) for a day trade.
- **charges**: the rates (zerodha.com/charges as read 2026-11-04, brokerage as INDstocks
  publishes it). Change them when they change.
- **blocked**: symbols they never want to trade.
- Keep **paper** true until they have watched it work on the paper ledger.

## Step 2: size the trade

```bash
python3 "${CLAUDE_SKILL_DIR}/size.py" --write
```

Whole shares only: NSE has no fractions. The size is the smallest of four
limits:

| limit | shares |
| --- | --- |
| max loss | the most shares whose loss at the stop, risk a share x shares PLUS the round-trip charges, stays inside `max_loss_per_trade_inr` |
| position cap | budget x `max_position_pct` / entry |
| max order | `max_order_inr` / entry |
| cash | (budget minus the cash buffer) / entry, fully funded; never more than the cash in `trader/portfolio.json` |

The entry it sizes at: the limit price with `--limit`. For a market order, the
plan's entry, or the price in `trader/prices.json` if it is fresh (under 15
minutes old) and higher. A price that ran up since the plan means a bigger
loss at the same stop, so fewer shares. A lower price never shrinks the risk.

Charges on an intraday round trip: brokerage Rs 10 an order (buy and sell),
STT 0.025 % of the sell, NSE transaction charge 0.00307 % of both sides,
SEBI fee and IPFT Rs 10 a crore each, stamp duty 0.003 % of the buy, 18 %
GST on brokerage, exchange and SEBI charges. On one share of a Rs 1,050
stock that is Rs 23.97, almost all of it the two brokerages; on 71 shares,
Rs 50.01.

MIS leverage (the master's INTRADAY_LEVERAGE, often 5x) is not used: the
pack sizes fully funded, so a position is never bigger than the cash behind
it. `allow_margin` stays false.

It refuses, and writes no order, when: the size is zero shares, the order is
under the minimum, reward:risk is under `min_reward_risk`, the stop is not
below the entry, the target is not above it (the move already happened), a
price is off the stock's tick, the plan is for another day, the symbol is
blocked, or the plan is a short. Say the reason to the user in one line.

## Step 3: show the user

Show the four limits, which one decided the size, the shares, the rupees,
the max loss at the stop and the charges on top. Say plainly that a stop is
an order, not a promise: in a fast market it can fill below the stop. Then
hand over to `/trader-risk`.

## Example: TATACONSUM, Tue Nov 3 2026

Opening-range breakout: entry 1,050.20 at 10:00, stop 1,039.40 (the
opening-range midpoint), target 1,061.00 (1R). Risk 10.80 a share, tick
0.10. The example config: Rs 100,000 budget, 10 % cash buffer, Rs 1,000 max
loss, 75 % position cap.

| limit | the math | shares |
| --- | --- | --- |
| max loss | Rs 1,000 / (10.80 + charges) | 87 |
| position cap | 75 % of Rs 100,000 = Rs 75,000 / 1,050.20 | **71 (decides it)** |
| max order | Rs 75,000 / 1,050.20 | 71 |
| cash | Rs 100,000 minus Rs 10,000 = Rs 90,000 / 1,050.20 | 85 |

71 shares = Rs 74,564.20. Max loss at the stop: 71 x 10.80 = Rs 766.80, plus
Rs 50.01 charges = Rs 816.81. It writes:

```json
{"symbol": "TATACONSUM", "security_id": "3432", "exchange": "NSE", "segment": "EQUITY",
 "product": "INTRADAY", "txn_type": "BUY", "order_type": "MARKET", "validity": "DAY", "qty": 71,
 "sl_trigger_price": 1039.4, "sl_limit_price": 1036.8, "tgt_trigger_price": 1061.0, "tgt_limit_price": 1061.1,
 "remarks": "day-trader-india/orb/2026-11-03",
 "why": "opening-range breakout on TATACONSUM: 71 shares, bound by your position cap (71 x 10.80 = Rs 766.80 at the stop, plus Rs 50.01 charges = Rs 816.81)",
 "plan": {"entry": 1050.2, "stop": 1039.4, "target": 1061.0, "tick": 0.1, "risk_per_share": 10.8,
          "max_loss": 766.8, "charges_at_stop": 50.01, "max_loss_net": 816.81, "setup": "orb",
          "entry_time": "10:00", "flat_by": "15:15"}}
```

The stop-loss leg triggers at 1,039.40 with its limit 0.25 % lower on the
tick (1,036.80, `stop_limit_buffer_pct`), so it can fill through a fast
drop; the target leg triggers at 1,061.00 with its limit one tick above.
`why` and `plan` are notes for trader-risk and trader-broker, never sent to
INDstocks. With the stop at the range low (1,030.00, risk 20.20) the max
loss decides it: 47 shares, 47 x 20.20 = Rs 949.40 plus Rs 40.96 charges =
Rs 990.36. With the stop at 900.00, six shares fit the max loss but
reward:risk is 0.07: refused. At 10:30 with a fresh quote of 1,058.00 the
same plan sizes to 51 shares at 18.60 a share, and reward:risk falls to
0.16: refused, because the move it was waiting for has mostly happened.

## Weights mode (kept from v1)

`--weights HDFCBANK=30,ICICIBANK=25` or `--targets FILE`
(`targets.example.json` here) splits a budget across a list by weight, whole
shares, so it needs prices (`--prices trader/prices.json`): the cash buffer
comes off the top, weights are renormalised to 100, any stock over
`max_position_pct` is pinned at the cap and its excess shared out, slices
under the minimum order are dropped, and one more share at a time goes to
the stock furthest under its slice while cash lasts. With a 25 % cap and the
Nov 4 2026 closes, the example file gives HDFCBANK 33 shares (Rs 24,311.10,
capped), ICICIBANK 16, RELIANCE 14, INFY 13 and TCS 4: Rs 89,861.30
invested, Rs 138.70 idle. These are plain CNC buys with no stop: trader-risk
blocks them while `require_stop` is true and counts each one against
`max_trades_per_day`. Use a separate config (`--config`) for a long-term
split.

## Rules

- Proposals only. This skill never calls a broker tool.
- Never size money the user has not said is for this. Ask for the budget and the max loss.
- Every number comes from the config, the plan or the prices. Nothing is guessed.
- No projections: never say what a trade will make. A target is a plan, not a forecast.
- INDstocks prices can lag. The exchange's price is the one that fills.

## What it never does

It never places, approves or schedules an order, never raises a limit on its
own, never shrinks a stop to fit more shares, and never sizes F&O,
margin or short positions.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, F&O, margin beyond MIS intraday, short selling or leverage unless the config allows it and the user confirms.
- Broker tokens live in environment variables or a .env file, never in the chat.

---
name: trader-alerts
description: >-
  Price Alerts for NSE. Rings when a price, a stop, a circuit band or the
  opening bell hits. Turns the trade plan into three alerts in one go (the
  stop, the target and "flat by close" at your flat-by time), plus above /
  below / stop / target levels, percent moves from the previous close, a
  circuit-band lock, any time of day, and the NSE opening bell, each ringing
  once so you are not spammed. Knows NSE hours with the 2026 holidays and
  the Muhurat session, works in IST, fetches INDstocks quotes in one request
  and writes trader/prices.json for sizing and the risk check. Use when the
  user says "alert me when", "tell me if RELIANCE hits", "watch my stop",
  "watch my trade", "remind me to close by 3:15", "is the market open", "when
  does the market open", "get me a price", or right after /trader-exit writes
  a plan.
argument-hint: "[symbol] [price]"
---

# trader-alerts

Price Alerts watches the numbers so the user does not have to stare at a
chart. An alert is information, never an order: when a stop alert rings,
nothing is sold by this skill. With a smart order the stop-loss and the
target legs already sit at the broker (or on the paper ledger) and do the
work; the alerts tell the user what happened and when it is time to be flat.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Quotes come through `trader_data.py` one folder up (INDstocks, `INDSTOCKS_TOKEN`).

```bash
python3 "${CLAUDE_SKILL_DIR}/alerts.py" clock                          # NSE open or closed, next open
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan                # stop, target, flat by close from trader/plan.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --opening-bell
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TATACONSUM --stop 1039.40
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol TATACONSUM --target 1061.00
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --at 15:15 --label "flat by close"
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --pct 3
python3 "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol RELIANCE --circuit
python3 "${CLAUDE_SKILL_DIR}/alerts.py" check                          # exit 0 = something rang
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote TATACONSUM --write       # -> trader/prices.json
python3 "${CLAUDE_SKILL_DIR}/alerts.py" quote --holdings --write       # portfolio + plan symbols
python3 "${CLAUDE_SKILL_DIR}/alerts.py" list
python3 "${CLAUDE_SKILL_DIR}/alerts.py" reset tataconsum-plan-stop
```

`--now "2026-11-03 15:15"` pins the clock (IST); `--offline DIR` reads saved
quote files instead of the network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the trade from trader-exit, for `add --from-plan` |
| `trader/alerts.json` | the rules (`alerts.example.json` in this folder) |
| `trader/state/alerts.json` | which rules rang and when, so each rings once |
| `trader/prices.json` | the latest quotes, merged in, for trader-budget, trader-risk and the paper ledger |
| `trader/portfolio.json` | the symbols held, for `quote --holdings` |

## Rule types

| type | rings when | rings again |
| --- | --- | --- |
| `above` / `target` | price at or above `price` | after `reset`, or on its own if `"rearm": true` and the price fell back |
| `below` / `stop` | price at or below `price` | same |
| `pct_move` | moved `pct` percent from the previous close (or `ref`), optional `direction` up or down | once per day at most |
| `circuit` | the price sits at the day's upper or lower circuit band (from the quote itself) | once per day at most |
| `time` | the IST clock passed `at` ("15:15") on a trading day | once per trading day |
| `opening_bell` | the NSE is open | once per trading day |

A rule with `"date"` is watched on that date only; `--from-plan` rules carry
the plan's date, so yesterday's stop never rings today. A `circuit` alert
matters here more than it would in the US: a stock locked at its upper band
has no sellers' counterparty for a buy and no buyers for a sell, so a stop
under a lower-band lock cannot fill.

## Quotes

- INDstocks' full-quote endpoint, through `trader_data.py`: one request for
  every symbol in today's rules (up to 1,000), last price, previous close,
  day high and low, and the day's circuit band. A few seconds of lag; the
  API allows 5 requests a second and the tool makes one.
- Quotes are for alerts and for checking a size, not for timing an order.
  The exchange's price is the one that fills. Each price in
  `trader/prices.json` keeps the time it was read, and trader-risk and
  trader-budget only use one under 15 minutes old.
- If the token has expired (they last 24 hours), the check says so in one
  line; the time and opening-bell rules still run without a quote.

## The NSE clock

Regular session 09:15 to 15:30 IST, Monday to Friday, closed on the NSE
holidays in `trader_data.py` (nseindia.com's list, read 2026-11-04; 2027 is
not published yet). No daylight saving, no early closes. Diwali's Muhurat
session (Sun Nov 8 2026) is a special one-hour evening session the clock
names and the pack does not trade. Brokers square off MIS intraday positions
from about 15:20, so a flat-by of 15:15 is the plan's own exit, before theirs.

## Steps

1. Right after trader-exit writes the plan, run `add --from-plan`. Confirm
   the three alerts back to the user: the stop, the target and the flat-by time.
2. Run `check` while the trade is open: every few minutes from a Claude
   scheduled task or `/loop`. Each run fetches only the symbols in today's
   rules and rings each alert once. A check reads one quote; it is not a live
   feed, and a price can pass a level between two checks.
3. Tell the user what rang in one line each. A stop or target alert means the
   smart order's leg has probably filled: check the order book (live) or run
   `ticket.py paper tick` (paper) and record the fill. "Flat by close" means
   close anything still open: trader-broker `flatten`, then trader-risk, then
   the user's yes. Never act for them.

## Example: TATACONSUM, Tue Nov 3 2026

Entry 1,050.20 at 10:00, stop 1,039.40, target 1,061.00, flat by 15:15.
`add --from-plan` writes `tataconsum-plan-stop`, `tataconsum-plan-target`
and `tataconsum-flat-by` for that day. A `check` while the price sits between
them reads:

```
NSE OPEN (2026-11-03 10:53 IST, regular session).
Nothing hit. 3 rule(s) watched today, TATACONSUM Rs 1055.00.
```

At 15:15:

```
ALERT Flat by close: it is 15:15 IST (set for 15:15), the market closes at 15:30 and the broker squares off
MIS from about 15:20. If TATACONSUM is still open, close it: trader-broker flatten --symbol TATACONSUM
builds the sell ticket, then trader-risk and your yes.
```

It rings once. The next morning the plan's three alerts are no longer
watched; with `add --opening-bell`, the bell rings at 09:15.

## Rules

- An alert is a fact about a price or the clock. Say it as a fact.
- Never present a quote as live to the tick. It lags by a few seconds.
- Confirm every new rule back to the user: symbol, type, level, day.

## What it never does

It never places, cancels or changes an order, never claims a quote is
live, and never predicts where a price is going.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, F&O, margin beyond MIS intraday, short selling or leverage unless the config allows it and the user confirms.
- Broker tokens live in environment variables or a .env file, never in the chat.

---
name: trader-exit
description: >-
  Exit Planner for NSE. Sets your stop and your target before you enter, so
  you know when to get out. Reads the entry from trader-entry (or a price you
  give), puts the stop at the opening-range midpoint by default (or the range
  low, your price, a percent, or a multiple of the ATR), rounds it down to the
  stock's tick, and sets the target at 2R (or 1R, or your price). Refuses a
  stop at or above the entry, a target at or below it, or a stop too tight to
  survive the spread, and says why. Writes trader/plan.json for the Time
  Machine, the Budget Manager and Price Alerts. Long side only. Use when the
  user says "where's my stop", "set my stop and target", "plan the exit",
  "when do I get out", "stop loss", "what's my R", "risk reward", or right
  after /trader-entry triggers, before /trader-timemachine.
argument-hint: "[--stop or-mid|or-low|price:X|pct:X|atr:K|supertrend] [--target 2R|1R|price:X]"
---

# trader-exit

The Exit Planner decides how a trade ends before it starts. The stop is where
the setup has failed. The target is where you take what you came for. Both
are set BEFORE the entry, written down and handed on, so nobody has to decide
anything in the middle of a fast move. A stop is a plan, not a guarantee: a
gap or a fast market can fill past it. Day trading loses most people money,
and a neat plan does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan                          # trader/entry.json: midpoint stop, 2R target
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop or-mid --target 1R --write
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --stop atr:1.5           # needs the session's bars (INDstocks or --offline)
python3 "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol TATACONSUM --entry 1050.20 --or-high 1048.80 --or-low 1030.00
python3 "${CLAUDE_SKILL_DIR}/exit.py" show                          # the current trader/plan.json
```

Other flags: `--flat-by 15:15`, `--json`, `--now "2026-11-03 10:00"` (IST),
`--entry PRICE` with a waiting entry.json to plan ahead, `--setup level`,
`--tick 0.05` or `--master FILE` for the tick, `--offline FILE` (a saved
bundle for an ATR stop).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger from trader-entry: symbol, entry price, time, bar, opening range, tick |
| `trader/config.json` | `flat_by` (15:15) and `min_reward_risk` (1.0: a note here, trader-risk enforces it) |
| INDstocks, through `trader_data.py` | only for an `atr:K` stop: the session's bars up to the entry bar; and the instrument master for the tick when entry.json has none |
| `trader/plan.json` | with `--write`: entry, stop and its rule, target and its R, risk a share, reward:risk, flat by, tick. trader-timemachine, trader-budget and trader-alerts read it |

## Stops and targets

| stop | where it goes |
| --- | --- |
| `or-mid` | the opening-range midpoint (the default for an opening-range breakout) |
| `or-low` | the opening-range low: wider, so fewer shares for the same max loss |
| `price:X` | your price |
| `pct:X` | X percent under the entry |
| `atr:K` | K times the 14-bar ATR under the entry, on the session's bars up to the entry bar. With fewer than 14 bars by then it averages all of them and says so |
| `supertrend` | the Supertrend(10, 3.0) line under the entry on the same bars: the lower band, which only rises while the price stays above it. With fewer than 10 bars the ATR averages what there is, and it says so. Refused when the line sits above the entry (a downtrend): a long has no stop there |

A level setup has no opening range to lean on, so it needs a stop you name.

- **The tick.** NSE prices move in ticks set by the price band: 0.01 under
  Rs 250, 0.05 to 1,000, 0.10 to 5,000, 0.50 to 10,000, 1.00 to 20,000, 5.00
  above. A limit or stop order off the grid is rejected by the exchange. The
  tick comes from entry.json (trader-entry reads it from the INDstocks
  master), else `--tick`, `--master FILE`, the downloaded master, or the
  price band (with a note to check it).
- **Rounding.** The stop rounds AWAY from the entry onto the tick (down, for
  a long). Risk = entry minus stop. Target = entry + R x risk, rounded half up
  onto the tick. `2R` by default; `1R`, `1.5R` or `price:X` also work.
- **Refused, with the reason.** A stop at or above the entry. A target at or
  below it. A risk under 0.05% of the price or under one tick: too tight to
  survive the spread, normal wiggle would stop you out. An entry.json that
  has not triggered (unless you give `--entry`). A short: v2 plans longs only.
- **Flat by.** 15:15 by default (or `flat_by` in the config). Out by then,
  win or lose, no overnight hold. Brokers square off MIS intraday positions
  from about 15:20, so a flat-by after that hands the exit to the broker (it
  says so). A flat-by at or after the 15:30 close, or an entry at or after
  the flat-by time, is refused.
- **Charges.** The risk a share here is price only. STT, brokerage, exchange
  and SEBI fees, GST and stamp duty come on top; `/trader-budget` adds them
  before it sizes the trade.

## Steps

1. Run it right after `/trader-entry` triggers. Before that, `--entry PRICE`
   plans ahead at the level you expect.
2. Use the default stop for an opening-range breakout unless the user names
   another. A level setup needs one: ask.
3. Run `exit.py plan --write`. Read back the entry, the stop and its rule,
   the target and its rule, the risk a share, reward:risk, flat by and the tick.
4. If it refuses, say the reason and the fix (a wider stop, a farther
   target). Never loosen anything on your own.
5. Say once that the stop is a plan, not a guarantee.
6. Hand off to `/trader-timemachine` to replay the plan, then `/trader-budget`
   to size it to the max loss per trade, charges included. Offer
   `/trader-alerts` for the stop, the target and the flat-by time. Claude can
   keep `entry.py watch` running in the background or on a `/loop` until a
   setup triggers, then come here.

## Example: TATACONSUM, Tue Nov 3 2026

The entry is 1,050.20 at 10:00 (the 09:55 bar, bar 8) and the opening range
is 1,030.00 to 1,048.80. TATACONSUM trades on a 0.10 tick. `exit.py plan
--stop or-mid --target 1R --write`:

```
Exit plan: TATACONSUM long, Tue 2026-11-03, opening-range breakout on 5-minute bars
  Entry         Rs 1,050.20 at 10:00 (bar 8)
  Stop          Rs 1,039.40  opening-range midpoint: (Rs 1,048.80 + Rs 1,030.00) / 2 = Rs 1039.4
  Target        Rs 1,061.00  1R (one times the risk)
  Risk a share  Rs 10.80 (1.03% of the entry; before charges, trader-budget adds them)
  Reward:risk   1.0
  Flat by       15:15 IST, win or lose: no overnight hold (MIS is squared off from about 15:20 anyway)
  Tick          0.1
```

The default 2R target would be 1,050.20 + 2 x 10.80 = 1,071.80. With
`--stop atr:1.5` the 14-bar ATR over the 9 bars to the entry is 5.8889, so
the stop is 1,050.20 - 8.83 = 1,041.37, rounded down to the tick: 1,041.30.
With `--stop supertrend` the Supertrend(10, 3.0) line on those 9 bars is
1,030.38 (fewer than 10 bars, so the ATR is their plain average), stop
1,030.30: 19.90 a share of risk, nearly twice the midpoint stop, and the 1R
target moves out to 1,070.10. On the 1-minute bars the same stop at the
09:31 entry (1,051.30) is 1,039.50. trader-budget then sizes it: a Rs 1,000
max loss per trade, after charges, buys about 90 shares at Rs 10.80 of risk
each, or about 48 at Rs 19.90.

## Rules

- The stop and the target are set before the entry, never after.
- Never move a stop further from the entry once the trade is on.
- Plain numbers only: entry, stop, target, risk a share, reward:risk. It
  never prints a P&L or a return.
- Nothing here promises winning trades or money. A 1R target is not a
  prediction that the price gets there.
- Long side only. v2 plans longs only.
- Every price is on the stock's tick. Never hand trader-broker a price the
  exchange will reject.

## What it never does

It never places an order, never sizes a position, never widens a stop to
make a plan pass, and never calls a stop a guarantee.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, F&O, margin beyond MIS intraday, short selling or leverage unless the config allows it and the user confirms.
- Broker tokens live in environment variables or a .env file, never in the chat.

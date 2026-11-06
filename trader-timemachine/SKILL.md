---
name: trader-timemachine
description: >-
  Time Machine for NSE. Replays every possible outcome of a trade before you
  risk a rupee: 10,000 simulated sessions plus every past time the setup
  fired. Takes the plan from trader-exit (entry, stop, target), rebuilds the
  rest of the day 10,000 times from the stock's own 5-minute bars over the
  last 100 days, and re-runs the same rule on every past session. Prints the
  odds of target first, stop first, or neither by the close, where the price
  sits at the close, the average R, the history, and one honest verdict, then
  hands off to /trader-budget. Use when the user says "run the time machine",
  "replay this trade", "what are the odds", "simulate this trade", "monte
  carlo", "backtest this setup", "how often does this hit the target", "is
  this a coin flip", or after /trader-exit writes a plan, before
  /trader-budget.
argument-hint: "[path to plan.json, or SYMBOL]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if today plays out like the stock's own last 100
days, how often does this plan hit the target first, the stop first, or
neither? It answers with 10,000 replays and with the real record of the same
rule on every past session. It is a simulation built from the stock's own
recent bars, not a prediction. Day trading loses most people money, and a
good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Bars come through `trader_data.py` one folder up (INDstocks, `INDSTOCKS_TOKEN`).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:15      # end each replay at your flat-by time
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol TATACONSUM --date 2026-11-03 \
    --entry 1050.20 --stop 1039.40 --target 1061.00 --entry-time 10:00
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--tick 0.05` (default: the plan's tick), `--offline FILE` (a saved
bundle, `.json` or `.json.gz`), `--now "2026-11-03 10:00"` (IST) to pin the
clock.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range, tick |
| INDstocks, through `trader_data.py` | 100 calendar days of 5-minute bars, 7 days a request (15 requests, a few seconds); can lag |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the close |

## How it works

1. **The pool.** It keeps only FULL sessions before the plan's date: all 75
   five-minute bars, 09:15 to 15:30 IST. Gaps, the Muhurat session and today
   are left out. Every bar becomes four ratios to the bar before it: open,
   high, low and close over the previous close.
2. **One replay.** Start at the entry price. For each bar left in the day,
   pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (default 15:30, the
   close; run it with `--until 15:15`, your flat-by, since brokers square
   off MIS positions from about 15:20). Green, which means target first or
   above the entry at `--until`.
   R is +1 at a 1R target, -1 at the stop, and otherwise the distance from
   the entry at `--until` divided by the risk. A gap through the stop still
   counts as -1R here. A real fill can be worse, and a circuit halt can hold
   the price away from both.
4. **The history.** The plan's own rule (the same opening range, stop rule
   and target R, stops rounded to the stock's tick) runs again on every one
   of those past sessions. That is what really happened, not a simulation.
   A `supertrend` stop is recomputed on each session's bars up to its entry
   bar; a session whose line sits above the entry has no stop and is counted
   separately, not taken.
5. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them.
6. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R when its sign goes against the lean. Those can only point the
   other way because of the replays that hit neither.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** A pool of 1-minute bars would be single trades, not
sessions. It refuses, in one plain line, when there are fewer than 20 full
sessions, when the entry bar is not in the data (wrong date, not closed yet,
a future day), or when the plan does not make sense (a stop above a buy's
entry, an entry off the 5-minute grid). A live in-progress bar is never used.

## The evening replay: every chart, one rule

```bash
python3 trader-scanner/scan.py orb --now "2026-11-04 15:00" --save trader/replays/orb5m-2026-11-04.json.gz --no-trend
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" universe trader/replays/orb5m-2026-11-04.json.gz --compare --others --write
```

After the close, the scanner saves the day's 5-minute bars for the whole
universe (about 450 requests, ten minutes) and `universe` runs the rule on
every chart: the first close above the 15-minute opening range, filled at
the next bar's open, stop at the range midpoint on the tick, target 2R, flat
at the 15:15 close, entries from the gate time to the cutoff. It prints two
tallies. EVERY breakout at the day's budget each, with charges, is what the
rule itself did that day. THE LEDGER is the paper month's rules on the same
breakouts: the budget, at most `max_trades_per_day` (2), one position at a
time, the most traded breakout first on the same bar, the day over after a
loss. `--compare` adds the baseline (1R, no gate, no cutoff, 3 trades).
`--write` saves `trader/replays/DATE.json`, which trader-risk's journal
picks up. `--min-score 5 --daily FILE` filters by the daily score card.

`--strategy` swaps the entry rule, so a popular intraday strategy can be
tested on the same days against the same costs before anyone believes a
claimed win rate (the pages that list them give none, and the one backtest
openalgo.in shows, EMA 9/21 on a NIFTY future, lost with a 28 % win rate):

| strategy | entry | stop | target |
| --- | --- | --- | --- |
| `orb` | the first 5-minute close above the 15-minute opening range | the range midpoint | R x risk |
| `vwap` | a close at least 0.4 % (`--vwap-pct`) under the session VWAP: "buy the dip" | the same distance under the entry | that bar's VWAP |
| `ema` | EMA 9 crosses above EMA 21 on the day's 5-minute closes (both need 21 bars, so no cross before 11:00: use `--cutoff none`) | one ATR(14) under the entry | R x risk, or the crossunder |
| `supertrend_rsi` | 5-minute Supertrend(10, 3) turns up with RSI(14) between 50 and 70 | the Supertrend line | R x risk |

On the 174-chart Nov 3 fixture, gate off, all day: vwap 131 signals, 59
target, 63 stop, net Rs -3,539.05; ema 106 signals, 31 target, 58 stop, 11
exits on the crossunder, net Rs -2,707.61; supertrend_rsi 61 signals, 2
target, 18 stop, 41 flat, net Rs -1,720.44. None beat ORB's Rs -2,868.30
by enough to matter on a soft day; the whole-universe table for Nov 3 to 5
is in `trader/replays/strategies-nov3-5.txt` (not in the tests).

On the 174-chart Nov 3 fixture: the gate is closed at 62.6 %, so the rule
takes nothing; the baseline took 90 breakouts, 34 target, 34 stop, 22 flat,
average R +0.03, and lost Rs 2,868.30 after Rs 2,345.90 of charges. Over
the whole universe on Nov 3, 4 and 5 (bundles in `trader/replays/`, not in
the tests) the rule sat out the two soft days and took 234 breakouts on
Nov 4 for Rs 5,399.95 net; the baseline lost Rs 32,773 and Rs 34,618 on the
soft days and made Rs 30,930 on Nov 4. Charges were larger than the gross
loss on both soft days. Three days are a baseline for the journal to beat,
not evidence.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write --until 15:15` (the flat-by), so
   "neither" means still open at the time you would get out.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the close, the average R, the history line, then the
   verdict line and any line under it exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 100 days and is not a
   prediction. Results, news and circuit halts can do things those days
   never did. Check `/trader-news` for the stock before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

## Example: TATACONSUM, Tue Nov 3 2026

The 15-minute opening range is 1,030.00 to 1,048.80. The 09:55 bar closes
above it: entry 1,050.20 at 10:00 (bar 8), stop 1,039.40 (the range
midpoint), target 1,061.00 (1R, risk 10.80 a share). On the 68 full sessions
from Jul 27 to Nov 2 (`tests/fixtures/trader-timemachine/TATACONSUM-100d5m.json.gz`):

```
10,000 replays of the rest of the session, 10:00 to 15:30 IST (66 five-minute bars), seed 19.
  target first   29.8 %
  stop first     35.9 %
  neither        34.3 %   still open at 15:30
  green          47.9 %   target first, or above the entry at 15:30
  Price at 15:30: median 1050.53, 90 % of replays between 1032.85 and 1069.09.
  Average R: -0.045
History: the same rule on each of those 68 real sessions fired 26 times:
15 target, 7 stop, 4 neither. Average R +0.306.
Close to a coin flip: target first 29.8 % vs stop first 35.9 %.
```

At 1R the target has to come first in half of the replays that hit one side.
It did in 45 %: a coin flip, leaning to the stop. The real history was better
than even, 15 targets to 7 stops. The replays and the history disagree, and
the user should hear both before `/trader-budget` sizes anything. With
`--until 15:15` the lean to the stop grows (23.6 % to 33.2 %, 43 % still
open), and the 2R target at 1,071.80 came first in only 5.6 % of replays.

The same entry with trader-exit's `supertrend` stop (1,030.30, risk 19.90,
1R target 1,070.10) is a different trade: the wide stop and the far target
leave 84.5 % of replays open at 15:30, target first 7.8 %, stop first 7.7 %,
average R +0.012. The history fired 23 times: 8 target, 3 stop, 12 neither,
average R +0.190; 3 more breakouts had the line above the entry and were
not taken. Fewer stops, but most sessions end at the close with whatever
the price is, and that is what the user should hear.

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- INDstocks bars can lag. Bars are 5-minute bars, checked bar by bar, never
  tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, F&O, margin beyond MIS intraday, short selling or leverage unless the config allows it and the user confirms.
- Broker tokens live in environment variables or a .env file, never in the chat.

---
name: trader-scanner
description: >-
  Chart Scanner for NSE. Scans thousands of charts for setups: opening-range
  breakouts, flags, oversold bounces. Builds the list of NSE EQ-series equity
  shares from the INDstocks instrument master minus NSE's ASM and GSM
  surveillance lists, runs a pre-market scan of daily bars (20-day closing-high
  breakout, bull flag, reclaimed the 20-day average, oversold, oversold bounce,
  EMA 10/20 cross, Supertrend turn, volume spike, Bollinger squeeze and release,
  with a 6-point score per chart) and an intraday opening-range breakout scan
  on 5-minute bars flagged with the daily trend, then writes
  trader/watchlist.json for trader-news and trader-entry. INDstocks data, bar
  by bar. Use when the user says "scan the market", "find me setups", "what is
  breaking out", "opening range breakouts", "scan for bull flags", "oversold
  stocks", "pre-market scan", "build my watchlist", "what should I look at
  today", or at the start of a trading day before /trader-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade. It reads thousands of
charts and lists the ones that match a rule. That is all it does. A scan is a
list of charts that match a rule, not a list of stocks to buy, and a setup on
a chart is not a prediction. Most people who day trade lose money (SEBI's own
study: 7 in 10 intraday traders lose); the scanner does not change that. It
only saves you from flipping through 2,000 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
Data comes through `trader_data.py` one folder up (INDstocks, `INDSTOCKS_TOKEN`
in the environment or a `.env` next to it; the token lasts 24 hours).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                              # -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                                 # before the open -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,TCS,INFY     # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                                   # after 09:35 IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:00                        # cut the clock at 10:00 IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Flags: `--min-price 50` (INR) and `--min-volume` (daily 500,000 shares, orb 0),
`--or-minutes 15`, `--now "2026-11-03 10:00"` (IST) to pin the clock,
`--offline PATH` to replay a saved bundle, `--save PATH` to save what was
fetched as one, `--no-write`, `--show N`, `--json`. Exit code 0 when the scan
found something, 1 when it found nothing or could not run (the reason is one
plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name` a line, read by `daily` and `orb` |
| `trader/surveillance.json` | written by `universe`: NSE's ASM and GSM lists as read, dated |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `circuit`, `hits[]`. trader-news and trader-entry read `hits[].symbol` |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 15:00) |

## The universe

`universe` downloads the INDstocks equity instrument master (NSE and BSE cash
lines) and NSE's ASM and GSM reports (nseindia.com's JSON, read with a
browser's headers; if NSE refuses, the scan says so and excludes nothing), and
keeps a row when, in this order:

1. It is an NSE row. The BSE line of the same share is out.
2. Its series is EQ. BE and BZ are trade-for-trade (delivery only, no
   intraday); SM and ST are SME platforms; everything else is debt, ETFs,
   mutual funds, REITs, InvITs, sovereign gold bonds and such.
3. Its ISIN is a share's: INE..., or IN9... for a DVR share. The EQ series
   also carries ETFs and fund units (INF...) and NSE's test symbols (DUMMY...).
   The master's own instrument type is not used: it tags 283 real companies
   (ROLEXRINGS, PINELABS, recent listings) "Other".
4. It is not on the GSM list, nor on the ASM list (long or short term).
   Surveillance names trade with 100 % margin, tight price bands and often
   trade-for-trade, so an intraday plan cannot be executed on them.

On the Nov 4 2026 files this keeps 2,233 of 23,044 rows (12,995 BSE lines,
6,460 other series, 581 SME, 235 trade-for-trade, 381 fund units and test
symbols, 131 ASM and 28 GSM names dropped). NSE's lists that day held 190 ASM
and 76 GSM symbols; the rest were not EQ-series shares anyway.

## The daily scan (before the open)

INDstocks answers daily bars for 5 symbols a request, 100 calendar days back,
so 2,233 symbols take 447 requests at 4 a second: about two minutes. Liquidity
comes from the last complete bar: close at least `--min-price` and volume at
least `--min-volume`. Today's bar is left out until 15:30, so a scan during
the session reads through yesterday's close. A chart whose last bar is locked
at a circuit band (high equals low, or the close sits on today's upper or
lower band from the master) is counted as `circuit` and skipped: no fill is
possible there.

The setups are computed from daily closes, the same rules as the US original:

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

Five indicator setups, added from the rules in marketcalls/openalgo-indicator-skills
(textbook definitions; that repo has no backtests, and neither claim is made
here), computed on the daily highs, lows, closes and volumes:

| setup | the rule |
| --- | --- |
| `ema_cross` | EMA 10 crossed above EMA 20 on one of the last 3 sessions (EMAs seeded with the SMA) |
| `supertrend_flip` | Supertrend(10, 3.0) turned up on the last bar: the close crossed its upper band |
| `volume_spike` | the last session's volume is over twice the average of the 20 before it |
| `bb_squeeze` | the Bollinger(20, 2.0) band width, (upper - lower) / middle, is at its lowest in 20 bars |
| `bb_release` | the width was at a 20-bar low yesterday and is not today: the squeeze ended |

Every hit also carries a **score out of 6**, a state and not a signal: close
above EMA 20, EMA 20 above EMA 50, RSI 14 above 50, RSI 14 under 70,
MACD(12, 26, 9) above its signal, ADX 14 above 20. It needs 51 sessions of
bars, else it reads n/a. The maths (Wilder smoothing for RSI, ATR and ADX)
lives in `trader_data.py`, shared with trader-exit and trader-timemachine.

Hits are listed most traded first (price times volume, in rupees).

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the 09:15,
09:20 and 09:25 five-minute bars. A hit is the first completed 5-minute bar to
CLOSE above the range high, at that bar's close time. INDstocks bars carry
real highs and lows, so there is one stage: 5-minute bars of the day, 5
symbols a request. The whole universe is 447 requests, about two minutes; the
daily watchlist or a list of names you trade (`--watchlist`, `--symbols`) is
seconds.

A chart is usable only when every 5-minute bar from 09:15 to the cutoff is
there; a missing bar means a thin stock or a halt. A bar off the 09:15 grid
is dropped, and a bar counts only once it is complete (start + 5 minutes). A
breakout bar that closed at or above the day's upper circuit band, or a range
locked at a band, is not a hit: the chart is reported as locked, since a buy
cannot fill at an upper circuit. Before 09:35, on a weekend or holiday, on
the Muhurat session, after the close, or after `no_entries_after` (an expired
setup) it prints one plain line and writes nothing.

## The breadth gate (at 10:00)

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" breadth                 # live: one quote sweep of the universe, 12 requests
python3 "${CLAUDE_SKILL_DIR}/scan.py" breadth --offline orb5m-2026-11-03.json.gz --now "2026-11-03 10:00"
```

Breadth is the share of charts trading above their 09:15 open right now,
from one sweep of INDstocks quotes over the universe (200 symbols a request;
charts under Rs 50 or with no volume are not counted). The gate is open when
the reading, taken at or after `gate_time` (10:00), is at or over
`breadth_gate_pct` (65). It writes `trader/state/breadth.json`, which
trader-risk reads: while the gate is closed, or no reading exists for today,
every entry is blocked. A reading before 10:00 is "too early" and the gate
stays closed. Re-run it before each entry; breadth moves.

Why 10:00 and 65: on the three whole-universe days replayed on 2026-11-05
(Nov 3, 4 and 5; the bundles live in `trader/replays/`, not in the tests),
breadth at 09:30 told nothing, while 10:00 breadth of 60 %, 73 % and 49 %
went with a losing, a winning and a losing day for the opening-range rule.
Three days fitted a threshold; the evening replay keeps testing it. On the
174-chart Nov 3 fixture the reading is 107 of 171 (62.6 %): closed.

## Data and limits

- INDstocks' data APIs allow 5 requests a second and 100,000 a day; the module
  paces itself at 4 a second and retries once after a 429. Bars are 5-minute
  bars, bar by bar, never tick by tick, and never real time.
- Partial results are said as partial: "Fetched 2,220 of 2,233 charts (13
  failed or had no data)". Never round a partial scan up to "the whole market".
- NSE's 2026 trading holidays are embedded (nseindia.com's holiday master, read
  2026-11-04). 2027 is not published yet: until it is, weekdays in 2027 count
  as trading days. Diwali 2026 (Sun Nov 8) has a Muhurat session the scanner
  does not run on.
- The circuit band of each name comes from the instrument master as of the
  download, so it is today's band.

## Steps

1. Once a week or so: `scan.py universe`.
2. Before the open: `scan.py daily`. Tell the user the counts (fetched,
   liquid, locked at a band, scanned, hits) and the top few by setup.
3. After 09:35 IST (10:00 is a reasonable first look): `scan.py orb`. Tell the
   user how many usable charts broke their range and when, and that a later
   hit is closer to the price now.
4. The user picks one chart. Hand it on: `/trader-news SYMBOL` (what is moving
   it), then `/trader-entry SYMBOL` (the entry bar by bar). Never pick for them.

## Example: Tue Nov 3 2026

The 10:00 scan over 174 large NSE names (`tests/fixtures/trader-scanner/orb5m-2026-11-03.json.gz`,
real INDstocks 5-minute bars):

```
python3 scan.py orb --offline orb5m-2026-11-03.json.gz --daily daily-2026-11-04.json.gz --now "2026-11-03 10:00"
Fetched 174 of 174 charts; 171 usable (3 under the price / volume floor).
48 of 171 charts closed above their opening-range high by 10:00:
  09:35  ADANIENSOL     1,395.20  range 1,366.80 to 1,389.90  +0.38% over  daily below EMA50
  ...
  10:00  TATACONSUM     1,050.20  range 1,030.00 to 1,048.80  +0.13% over  daily above EMA50
Daily trend: 16 of 48 hits closed above their 50-day EMA the day before (a flag, not a filter).
```

The trend flag comes from the daily bars dated before the scan day (live,
one small fetch for the hits; offline, `--daily FILE`; `--no-trend` skips
it). It is shown so the user can see which breakouts run with the daily
trend; it never removes a hit.

RELIANCE was not among them: its range was 1,282.50 to 1,307.00 (the 09:15
bar's high) and no bar closed above 1,307 until the very last one, the 15:25
bar at 1,307.90, which a scan at `--at 15:30` reports and a scan before
`no_entries_after` (15:00) never sees.

The pre-market scan the next morning (daily bars through Wed Nov 4,
`daily-2026-11-04.json.gz`, the same 174 names) tags 57 charts a 20-day
closing-high breakout (HDFCBANK, RELIANCE, BHARTIARTL and ITC among them), 2 a
bull flag (INDHOTEL, TECHM), 22 a reclaim of the 20-day average, SWIGGY
oversold (RSI 26.6) and SWIGGY and POLICYBZR an oversold bounce. The
indicator setups add 34 EMA 10/20 crosses (HDFCBANK, RELIANCE, SBIN, ITC), 22
Supertrend turns (BAJFINANCE, M&M, BEL), 18 volume spikes, 15 Bollinger
squeezes (SUNPHARMA, HINDALCO, SAIL) and 7 releases (BEL, TITAN, TATASTEEL):
108 of the 149 liquid charts match something, after a strong week. BEL alone
carries four tags and a 5/6 score; ADANIPORTS is one of five charts at 6/6;
65 of the 108 hits score 5. A score is a state, not a signal.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.
- A locked chart is reported as locked. Never list a circuit-locked name as a hit.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, F&O, margin beyond MIS intraday, short selling or leverage unless the config allows it and the user confirms.
- Broker tokens live in environment variables or a .env file, never in the chat.

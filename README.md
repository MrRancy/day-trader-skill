# The Day Trader skill, India edition

11 Claude skills for day trading NSE stocks: scan the exchange for setups,
track news and filings, time every entry and exit, and replay every outcome
before you risk a rupee.

A port of [Jake Schincariol's day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT, US market) to the Indian market. Same eleven jobs, same chain of files,
same offline test suite; the data, the clock, the currency, the charges and
the rules are India's. The tools are plain Python that read free public data:
INDstocks for bars and quotes, NSE's own feeds for the holiday list, the
surveillance lists, corporate announcements, insider disclosures, bulk and
block deals and quarterly results, and Google News for headlines.

One scans every NSE stock for setups. One watches a setup bar by bar and says
the moment to get in; another sets the stop and the target before you do. The
Time Machine replays the rest of the session 10,000 times from the stock's
own recent bars, plus every past day the same setup fired, and tells you the
honest odds. The budget tool sizes the trade with STT, brokerage, exchange
charges, GST and stamp duty inside the max loss.

And no trade goes through until you say yes. Nothing in this pack can place
an order on its own. Paper trading is the default, and there is no broker
sandbox in India, so paper trading here is a local ledger that fills at the
quoted price.

## Install

```bash
git clone <this repository> day-trader-india
cp -r day-trader-india/trader-* day-trader-india/trader_data.py ~/.claude/skills/
```

Project-local instead of global: copy the same files into your repo's
`.claude/skills/`. The skill folders import `trader_data.py` from the folder
above them, so keep it next to them. No Claude Code at all? Paste any single
`SKILL.md` at the top of a chat and it runs as a mode. You lose the Python
tools, but the method works.

The tools need Python 3.8 or newer. Nothing to pip install. Bars and quotes
come from [INDstocks](https://api-docs.indstocks.com): the API is free, the
token lives in the `INDSTOCKS_TOKEN` environment variable or a `.env` file
next to `trader_data.py`, and it expires every 24 hours (regenerate it at
indstocks.com, API trading, access tokens). The token is never written to
disk by the tools and never printed. Everything that reads NSE's site sends
a browser's headers and pauses between calls; no key, no login.

## The eleven

| command | job | what it does |
| --- | --- | --- |
| `/trader-scanner` | Chart Scanner | Scans the NSE universe for setups: opening-range breakouts, 20-day breakouts, flags, oversold bounces, EMA crosses, Supertrend turns, volume spikes, Bollinger squeezes, with a 6-point score per chart. Skips ASM/GSM names and circuit-locked charts. |
| `/trader-news` | News Monitor | Headlines (Google News, Yahoo when it answers) and the company's own NSE filings, with today's move. |
| `/trader-entry` | Entry Timer | Watches a setup bar by bar, IST, and says the moment to get in. |
| `/trader-exit` | Exit Planner | Sets the stop (range midpoint, ATR or Supertrend) and the target on the stock's tick before you enter; flat by 15:15. |
| `/trader-timemachine` | Time Machine | Replays the rest of the session 10,000 times plus every past day the setup fired. |
| `/trader-risk` | Risk Manager | Checks every order against your limits, charges included, and blocks anything over them. No PDT rule: India has none. |
| `/trader-budget` | Budget Manager | Sizes the position to your budget and max loss in rupees, net of charges, as an INDstocks bracket order. |
| `/trader-broker` | Order Executor | Shows the ticket in plain words, takes your yes, fills it in the paper ledger; INDstocks live only when you turn it on. |
| `/trader-alerts` | Price Alerts | Rings on a price, a stop, a circuit band, the opening bell or the flat-by time, on the NSE clock. |
| `/trader-analyst` | Results Analyst | Reads the quarterly Ind-AS XBRL a company filed with NSE and sums up what changed; finds the next results date. |
| `/trader-insiders` | Insider Tracker | Reads SEBI insider-trading disclosures and NSE's bulk and block deals the evening they post. |

And one conductor, `/trader-day`: it knows the clock and the state files,
says where the day stands and what the next step is, and runs the
mechanical steps (the morning scan and news, the 10:00 gate, the watch
while a trade is open, the evening fetch, replay and journal) by calling
the others. It never picks a chart, sets a stop or says yes.

## How to use it

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote:

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by 15:15                                            (while it is open)
news / analyst / insiders                                                      (on a schedule)
```

An example, Tata Consumer Products on Tuesday November 3 2026, real
INDstocks data, all times IST.

1. **`/trader-scanner`** at 10:00: of 174 large NSE names in the scan, 171
   were usable and 48 had closed above their 15-minute opening range.
   TATACONSUM is one: range 1,030.00 to 1,048.80, the 09:55 bar closed at
   1,050.20, 0.13 % over the top, and its daily close sat above the 50-day
   EMA, as 16 of the 48 did. The universe itself is 2,233 NSE stocks from
   INDstocks' instrument master, minus the ASM and GSM lists. The next
   morning's daily scan tags 108 of 149 liquid charts, with the indicator
   setups (EMA crosses, Supertrend turns, volume spikes, Bollinger
   squeezes) and a score out of 6 on each.
2. **`/trader-news`**: four new items in the last 48 hours (Moneycontrol on
   Nomura flagging margin pressure across consumer names, Global Coffee
   Report on Tata Starbucks' quarter) and the company's own NSE filings. The
   results were out on October 26, so nothing is due inside the trade.
3. **`/trader-entry`** watched TATACONSUM bar by bar. At 09:57 it said
   waiting (the 09:55 bar was still forming). At 10:00 it said triggered: the
   first 5-minute close above the range, 1,050.20.
4. **`/trader-exit`** set the plan before the entry: stop at the range's
   midpoint, 1,039.40, so 10.80 a share at risk; target one times that
   risk, 1,061.00; flat by 15:15 either way, because the broker squares off
   intraday positions from about 15:20. Every price sits on the stock's
   0.10 tick. A Supertrend stop instead would sit at 1,030.30, 19.90 a
   share: a different trade, and the Time Machine says so.
5. **`/trader-timemachine`** replayed the rest of the day 10,000 times, each
   5-minute candle drawn from TATACONSUM's own candles at that time of day
   over the previous 68 sessions (seed 19, so anyone can repeat it): target
   first 29.8 %, stop first 35.9 %, still open at the close 34.3 %. It also
   re-ran the same rule on those 68 real sessions: it fired 26 times, 15 hit
   the target, 7 the stop, 4 neither. Its verdict: close to a coin flip,
   leaning to the stop. That is the point of looking before you risk a rupee.
6. **`/trader-budget`** with a Rs 100,000 account, a 75 % position cap and a
   Rs 1,000 max loss per trade: the cap decides it, 71 shares
   (Rs 74,564.20). If the stop fills at 1,039.40 the loss is 71 x 10.80 =
   Rs 766.80, plus Rs 50.01 of charges, Rs 816.81. It writes one INDstocks
   bracket order: buy 71 TATACONSUM intraday, stop-loss trigger 1,039.40
   (limit 1,036.80), target trigger 1,061.00 (limit 1,061.10).
7. **`/trader-risk`** checks it against your limits: the max loss net of
   charges, the daily loss limit, trades per day, reward to risk, the entry
   window (09:30 to 15:00), the tick, position size and cash. PASS or BLOCK,
   with the reason.
8. **`/trader-broker`** shows you the ticket in plain words. You type yes for
   that exact order. The paper ledger buys 71 shares at the quoted 1,050.50
   for Rs 74,585.50 plus Rs 16.92 of charges, and the stop and target legs
   wait; a later quote of 1,061.20 fills the target at 1,061.00. Live orders
   go through INDstocks only with `"paper": false` in the config and
   `--live` on the command, from a whitelisted static IP.
9. **`/trader-alerts`** rings on the stop, the target and at 15:15 to flatten
   whatever is still open.

## The tools

All standard-library Python. The data tools read public endpoints and are
polite about it.

```bash
python3 trader_data.py whoami                                          # is the INDstocks token good?
python3 trader_data.py quote RELIANCE TATACONSUM                       # live quotes
python3 trader-scanner/scan.py universe                                # ~2,200 NSE stocks, minus ASM/GSM
python3 trader-scanner/scan.py daily                                   # pre-market setups, whole universe
python3 trader-scanner/scan.py orb --at 10:00                          # opening-range breakouts so far
python3 trader-news/news.py --symbols TATACONSUM --hours 12            # headlines, NSE filings, today's move
python3 trader-entry/entry.py check TATACONSUM --setup orb             # waiting / triggered / expired
python3 trader-entry/entry.py watch TATACONSUM --every 30              # bar by bar until it triggers
python3 trader-exit/exit.py plan --stop or-mid --target 1R --write     # -> trader/plan.json
python3 trader-timemachine/timemachine.py run --write                  # 10,000 replays + the history
python3 trader-budget/size.py --write                                  # -> trader/orders.json
python3 trader-risk/check.py                                           # PASS / BLOCK per order
python3 trader-broker/ticket.py show                                   # the tickets, READY or not
python3 trader-broker/ticket.py paper show                             # the paper ledger
python3 trader-alerts/alerts.py add --from-plan                        # stop, target, flat by 15:15
python3 trader-alerts/alerts.py check                                  # rings what hit, once
python3 trader-analyst/results.py TATACONSUM                           # quarters, margins, EPS, next results
python3 trader-insiders/insiders.py pit TATACONSUM                     # insider disclosures
python3 trader-insiders/insiders.py deals                              # today's bulk and block deals
```

```bash
python3 -m unittest discover -s tests -v
```

The tests run offline, on fixtures cut from real data: INDstocks' instrument
master and TATACONSUM's 5-minute bars for the 100 days to November 3 2026,
the 10:00 opening-range scan over 174 names and the next morning's daily
scan, NSE's holiday and surveillance lists, Google News and NSE announcement
feeds, insider disclosures, a day of bulk and block deals, and twelve
quarterly XBRL filings. They reproduce every number in the example above.

## The paper month

The pack's answer to "does this make money" is a month of paper trading
with a journal, not a backtest. `trader-budget/paper.example.json` is the
config for it: Rs 10,000 a day, at most two trades, Rs 300 max loss a
trade, a 2R target, entries only between 10:00 and 11:00 and only when the
10:00 breadth gate is open, the day over after one loss.

```bash
python3 trader-day/day.py status                                       # where the day stands, the next step
python3 trader-day/day.py morning                                      # 08:45: scan and news
python3 trader-day/day.py gate                                         # 10:00: breadth, then the breakouts
python3 trader-day/day.py evening --note "what went wrong"             # after 15:30: bars, replay, journal
python3 trader-scanner/scan.py breadth                                 # 10:00: is the market broad enough?
python3 trader-risk/check.py                                           # blocks entries while the gate is closed
python3 trader-scanner/scan.py orb --now "2026-11-04 15:00" --save trader/replays/orb5m-2026-11-04.json.gz --no-trend
python3 trader-timemachine/timemachine.py universe trader/replays/orb5m-2026-11-04.json.gz --compare --others --write
python3 trader-risk/check.py journal --note "what went wrong today"    # -> trader/journal/DATE.json
python3 trader-risk/check.py review --days 7                           # the week in counts
```

Why those rules: on three whole-universe days replayed on 2026-11-05 the
opening-range rule lost about Rs 31,000 and Rs 32,000 across every
breakout on two soft days and made Rs 31,500 on a broad up day, at Rs
10,000 a breakout. Charges were larger than the gross loss on the soft
days. A 10:00 breadth gate sat out both soft days; a 2R target beat 1R; the
6-point score card did not help. Three days fitted those settings, which is
why the evening replay re-scores them every day against the baseline.

### Evidence before money

The paper month alone is too slow to answer whether the rule works: two
trades on the days the gate opens is ten or so a month. So the evidence
comes from the multi-day replay over every saved session, and the paper
month checks that the live routine behaves like the replay.

```bash
python3 trader-timemachine/timemachine.py multiday --test 20 --write trader/replays/multiday.json
python3 trader-risk/check.py review --days 7
```

The replay trades the paper config's own rule, changes one setting at a time,
chooses on the earlier days and shows the last 20 only for the rule and that
one choice (the rest stay sealed). Slippage is 0.05 % a side for a stock
trading Rs 50 crore a day and more for thinner ones (`slippage_model`
"turnover", up to 0.5 %), and a stop gapped through fills at the open. The
95 % ranges of the every-signal numbers are computed from one total per day,
because signals on one day rise and fall together. The review reports net R
a trade, expectancy with a 95 % range, profit factor, max drawdown and weekly
net.

The 72 saved sessions (Aug 3 to Nov 16) say the long opening-range breakout
does not cover its costs in any shape tried, so it is not tuned further. Two
last intraday tests are written down in
`trader-timemachine/registered.json` (a short side on days at or under 30 %
breadth, only stocks INDstocks allows intraday; and the top 20 by relative
volume with no gate) and are scored only on sessions saved from 2026-11-17,
every evening, by `timemachine.py registered`.

The routine runs itself on weekdays once `python3 trader-day/day.py schedule
install` has set up the morning, gate and evening tasks (Windows Task
Scheduler, logs in `trader/logs/`), and the token renews itself when
`.env` has `INDSTOCKS_CLIENT_ID`, `INDSTOCKS_MPIN` and
`INDSTOCKS_TOTP_SECRET`. Entries still wait for a person.

**Go/no-go, decided 2026-11-16, tightened 2026-11-17.** No real money until
both hold: on the held-out replay days, every signal of the paper rule on at
least 10 days with the LOW end of its by-day 95 % range above zero (not just
a positive average); and net expectancy above zero over at least 200 paper
trades. `check.py review` prints the verdict.

## Fine print

**Not investment advice.** This is software that reads public data and does
arithmetic. It is not a SEBI-registered investment adviser or research
analyst, it does not know your situation and it cannot see the future. SEBI's
July 2024 study of individual intraday traders in the equity cash segment
found that seven out of ten lost money. You can lose money. Decide for
yourself, and talk to a registered professional if you need advice.

**No trade goes through until you say yes.** Only `/trader-broker` can lead to
an order, and only after `/trader-risk` passes that exact order and you type
yes for it in the chat. Keep the order step on its own permission prompt in
Claude; never allow-list it. No F&O, margin funding or short selling unless
you turn them on in the config.

**Paper first, and paper is a ledger.** No Indian broker offers a sandbox, so
`"paper": true` (the default) fills orders in `trader/paper.json` at the
quoted last price, including charges, and squares off intraday positions at
15:20 like a broker would. A real fill can differ. Going live needs
`"paper": false` in the config and `--live` on the command, an INDstocks
token that you regenerate every 24 hours, and a static IP whitelisted with
the broker, which the exchanges require for API order placement (NSE circular
NSE/INVG/67858). Read-only calls need no IP.

**Intraday (MIS) positions are force-closed.** An intraday order is squared
off by the broker from about 15:20 at the market price, charges included,
whether you planned it or not. `flat_by` defaults to 15:15 so the exit is
yours. Carrying a position overnight is a different product (CNC, delivery)
with different margins and charges; the config's `product` decides.

**Margins are upfront.** SEBI's peak-margin rules require the full VaR plus
ELM margin to be in the account before an order, and intraday leverage is
whatever your broker allows within that. The budget tool sizes to the cash
you give it, not to leverage.

**Charges are inside every number.** STT at 0.025 % on the sell side of an
intraday equity trade, the exchange transaction charge on both sides, the
SEBI fee, stamp duty on the buy side, GST on brokerage and exchange charges,
and a flat brokerage (Rs 10 an order at INDstocks). The max loss shown is the
loss at the stop price plus the charges of the round trip. Rates are as
published in November 2026 and change with Budgets; `config.example.json`
holds them where you can edit them.

**Circuit limits, not halts.** NSE stocks move inside daily price bands of
2, 5, 10 or 20 %. A stock pinned at its band does not trade through it: a
stop cannot fill until it unlocks, and the scanner never lists a locked chart
as a hit. Stocks on the ASM or GSM surveillance lists are out of the universe.

**A stop is a plan, not a guarantee.** In a fast market, across a gap or at a
circuit band, a stop can fill well past its trigger, or not at all. The
stop-loss leg is a stop-limit with its limit 0.25 % below the trigger.

**The Time Machine is a simulation, not a prediction.** It is built from the
stock's own last 100 days of 5-minute bars. The future can do things those
days never did: a results day, a circuit lock, a SEBI order, a headline.
Treat its odds as a reality check, never as a promise.

**Indicators are rules, not evidence.** The EMA, Supertrend, Bollinger,
volume and score rules in the scanner and the exit planner are textbook
definitions, taken from a public indicator catalogue that carries no
backtests. Nothing here says a setup works. The only numbers about outcomes
are the Time Machine's, on the stock's own past sessions, and they are
quoted as counts.

**The data is free and can lag.** INDstocks' bars and quotes are a few
seconds behind the exchange and the tools read 1-minute or 5-minute bars,
not every tick. NSE's site is not an API: it wants browser headers and can
change or block without notice. BSE refuses automated clients, so BSE-only
filings are not read. Check anything that matters against your broker before
you act on it. The holiday calendar covers 2026 and Muhurat trading; for
other years the tools say so.

**Disclosures are public, not inside information.** Insiders must report
trades under SEBI's insider-trading regulations within two trading days,
and the company has two more to tell the exchange. Bulk and block deals are
published after the close. The insiders skill reads those public filings the
evening they post; it has no inside information and says so.

**Taxes.** In India, intraday equity gains are speculative business income
taxed at your slab rate, and intraday losses can be set off only against
speculative gains. Nothing here is tax advice.

## Files

```
trader_data.py      the shared data layer: INDstocks bars, quotes, instruments, the NSE calendar, ticks, charges
trader-scanner/     SKILL.md, scan.py
trader-news/        SKILL.md, news.py
trader-entry/       SKILL.md, entry.py
trader-exit/        SKILL.md, exit.py
trader-timemachine/ SKILL.md, timemachine.py
trader-risk/        SKILL.md, check.py
trader-budget/      SKILL.md, size.py, config.example.json, paper.example.json, targets.example.json
trader-broker/      SKILL.md, ticket.py
trader-alerts/      SKILL.md, alerts.py, alerts.example.json
trader-analyst/     SKILL.md, results.py, metrics.example.json
trader-insiders/    SKILL.md, insiders.py
trader-day/         SKILL.md, day.py (the conductor)
tests/              unittest suite + offline fixtures
upstream/           the original US-market skill, read-only, for reference
```

MIT. The original is by [Jake Schincariol](https://opusjake.ai); this port
keeps his structure and his licence.

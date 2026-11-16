# day-trader-india

Port of github.com/Jakeschincariol/day-trader-skill (MIT, US market) to the
Indian market (NSE/BSE). The original is cloned read-only at ./upstream/.
Keep its structure: 11 skill folders, each a SKILL.md plus one stdlib-only
Python script, sharing state through a trader/ folder (universe.txt ->
watchlist.json -> plan.json -> orders.json), plus one conductor folder,
trader-day (added 2026-11-05: day.py reads the clock and the state files,
names the next step, and runs the mechanical steps by calling the other
scripts; it never decides a trade). Keep its offline test suite pattern:
tests/ with real-data fixtures that reproduce every README number.
The skills are installed as directory junctions under .claude/skills
(gitignored), so the scripts resolve their folder with os.path.realpath.

## Decisions already made

- Data layer: INDstocks API (api-docs.indstocks.com). Free, bearer token
  in INDSTOCKS_TOKEN env var, token expires every 24h, TOTP headless
  regeneration later. Historical 1m/5m/15m/1h/1d candles, REST quotes,
  instruments master CSV. Never write the token to disk or print it.
- Second backend: OpenAlgo (github.com/marketcalls/openalgo, AGPL-3.0,
  self-hosted Flask app, Python 3.12+), decided 2026-11-05. It fronts
  Zerodha and 35 other brokers (not INDstocks) behind one REST API at
  http://127.0.0.1:5000/api/v1 with the OpenAlgo API key as "apikey" in the
  JSON body (OPENALGO_API_KEY, OPENALGO_HOST env vars or .env; never the
  broker's credentials). Talk to it with urllib only: no openalgo pip
  package, no WebSocket. A direct Zerodha Kite client is not built. Its
  schemas reject unknown fields (HTTP 400), so every request carries exactly
  the documented fields. Market data through it depends on the broker's
  own entitlement (Zerodha candles still need the paid Kite Connect plan).
  The other marketcalls repos (openalgo-charts, openalgo-indicator-skills,
  openquest) are out: Node, numpy/pandas/plotly or QuestDB dependencies.
- Yahoo Finance with .NS suffix: last-resort fallback for daily closes only.
- One shared module trader_data.py with backends indstocks | openalgo | yahoo
  exposing instruments(), candles(symbol, interval, from, to), quote(symbols).
  Every skill reads bars through it, never a vendor endpoint directly.
- No broker offers a sandbox. Build paper mode as a local simulated ledger
  (trader/paper.json, fills at quoted LTP). paper=true is the default.
  OpenAlgo's Analyzer mode (sandbox capital, live data, simulated order ids,
  "mode": "analyze" on every response) is a second paper target: with
  broker "openalgo" and paper true, every placement first checks POST
  /analyzer and refuses unless analyze_mode is true. Live order placement
  additionally requires a whitelisted static IP (NSE circular
  NSE/INVG/67858) at the broker behind OpenAlgo; read-only endpoints do not.
- OpenAlgo has no MIS bracket: its GTT/OCO order takes CNC or NRML only. An
  intraday stop and target through OpenAlgo are two resting orders (SL-M
  for the stop, LIMIT for the target) that the broker tool reconciles,
  cancelling the other when one fills. CNC plans use GTT OCO.
- Pelosi tracker -> trader-insiders: SEBI PIT insider-trading disclosures
  plus NSE/BSE bulk and block deals.
- Analyst: BSE XBRL quarterly results (no EDGAR equivalent). Weakest link;
  do this last.

## Market constants (replace every ET/NYSE assumption)

- Timezone Asia/Kolkata, no DST. Session 09:15-15:30. Pre-open 09:00-09:08.
- flat_by default 15:15 (brokers auto-square-off MIS intraday around 15:20).
- no_entries_before 09:30, no_entries_after 15:00 as defaults.
- Tick size 0.05 INR. Currency INR everywhere; config keys *_inr not *_usd.
- No PDT rule: delete pdt_guard entirely.
- Circuit limits 2/5/10/20 percent replace US halts; scanner must detect
  a bar pinned at the band. Exclude GSM/ASM surveillance-list symbols.
- NSE holiday calendar, plus Muhurat trading as a special short session.
- Budget/risk math must include charges: STT (sell side intraday),
  brokerage (INDstocks flat 10 INR/order), exchange txn charge, GST, SEBI
  fee, stamp duty. Max loss at the stop must be net of charges.

## Order of work

1. trader_data.py INDstocks backend + fixtures (pull one real day of
   RELIANCE 5m bars and the instruments master, save under tests/fixtures).
2. trader-scanner (NSE universe from the instruments master, daily + ORB).
3. trader-entry, trader-exit, trader-timemachine (mostly clock changes).
4. trader-budget, trader-risk (INR config, charges, no PDT).
5. trader-broker (paper ledger first, then INDstocks live with GTT/OCO
   bracket). Nothing places an order without "yes" in chat.
6. trader-alerts, trader-news (Yahoo RSS .NS + NSE/BSE announcements).
7. trader-insiders, trader-analyst.
8. README with Indian fine print: SEBI intraday margin rules, MIS
   force-close, STT, not investment advice, most day traders lose money.
9. OpenAlgo as the second backend (steps 1-8 done 2026-11-05). PAUSED
   2026-11-05 at 9a: OpenAlgo v2.0.3.2 is installed at ../openalgo and runs
   with `uv run app.py` from a real terminal (Flask-SocketIO refuses a
   non-tty), but the Zerodha login never got past Kite's "Invalid api_key";
   no broker keys are stored anywhere. Resume at 9a when a Kite app works.
   In order:
   a. Probe a local OpenAlgo in Analyzer mode with a broker connected (the
      user installs it; the tools never store the API key). Save real
      answers as fixtures: GET /instruments?exchange=NSE&format=csv,
      POST /history for RELIANCE 5m and D, /quotes, /intervals,
      /analyzer, and one Analyzer-mode /placeorder, /orderstatus,
      /openposition, /cancelorder round trip. Record the candle timestamp
      unit and timezone against the INDstocks bars for the same day.
   b. trader_data.py: class OpenAlgo behind the same interface (token()
      reads OPENALGO_API_KEY and OPENALGO_HOST like INDSTOCKS_TOKEN;
      instruments() maps symbol/name/token/tick_size/lotsize onto the
      INDstocks column names the scanner reads; candles() maps intervals
      1m/5m/15m/1h/D and the YYYY-MM-DD date window; quote() maps
      ltp/open/high/low/prev_close/volume, and leaves upper/lower circuit
      None because OpenAlgo's quote has no bands). backend("openalgo").
      Tests on the fixtures; the INDstocks tests stay untouched.
   c. trader-budget: smart_order() gains a broker shape. For "openalgo" the
      entry is a /placeorder body (strategy "day-trader-india", symbol,
      exchange NSE, action BUY, product MIS|CNC, pricetype MARKET|LIMIT,
      quantity as a string) plus two exit legs (SL-M with trigger_price =
      stop, LIMIT at the target) or, for CNC, one /placegttorder OCO
      (triggerprice_sl/stoploss, triggerprice_tg/target). orders.json keeps
      why/plan; the fingerprint in trader-risk covers the new fields.
   d. trader-risk: a second OPENALGO_FIELDS fingerprint; the same loss,
      charges, window and tick checks; brokerage from the config (Zerodha
      is Rs 20 or 0.03 percent an order, whichever is lower; INDstocks a
      flat Rs 10), so the charges block gets a per-broker entry.
   e. trader-broker: class OpenAlgoBroker (place, status, positions,
      cancel, funds, analyzer_mode). `place` with paper true requires
      analyze_mode true and records the "mode": "analyze" answer; `--live`
      with paper false places for real. A `reconcile` command polls
      /orderstatus for the two exit legs and cancels the survivor when one
      is complete; `flatten` uses /closeposition. The paper ledger stays the
      default and its tests stay green.
   f. SKILL.md and README: the two paper targets, the two brokerages, the
      MIS-has-no-bracket caveat, and the Analyzer-mode-first rule.
   Never call /placeorder outside Analyzer mode during development.
10. Indicator setups, decided 2026-11-05 after reading
    marketcalls/openalgo-indicator-skills (MIT): it holds textbook
    definitions and example rules, no backtests, no win rates. Take the
    rules, not the code; every number comes from our own fixtures.
    a. trader-scanner daily: ema_cross (EMA 10 over EMA 20 within the last
       3 bars), supertrend_flip (Supertrend 10, 3.0 turns up), volume_spike
       (volume over 2x the 20-day average), bb_squeeze (Bollinger 20, 2.0
       width at a 20-bar low) and bb_release (the squeeze ended this bar).
       Per hit a score card out of 6: close > EMA20, EMA20 > EMA50,
       RSI14 > 50, RSI14 < 70, MACD(12,26,9) > signal, ADX14 > 20.
    b. trader-scanner orb: a trend flag per hit from the daily bars (close
       above EMA50), shown, never a hidden filter.
    c. trader-exit: --stop supertrend (10, 3.0 on the intraday bars) beside
       atr and or-mid; the plan records which.
    d. trader-timemachine history on the existing fixtures for each rule
       that is a 5-minute entry; daily setups are reported as counts over
       the daily fixture. SKILL.md and README quote only those runs, in the
       "fired N: target/stop/neither" form, no win-rate language.
    e. Tests reproduce every number; stdlib only; Wilder smoothing for RSI,
       ATR and ADX, EMA seeded with the SMA.
11. The paper month (built 2026-11-05 evening; paper day 1 is 2026-11-13,
    "paper_start" in trader/config.json, Nov 6 was a dry run; 3-4 weeks, then a Rs 10,000 real account with 1-2 trades
    a day, then Zerodha via step 9 for its Rs 20-or-0.03 % brokerage).
    Three whole-universe days (Nov 3-5, bundles in trader/replays/,
    gitignored) showed: charges beat the gross loss on soft days, a 10:00
    breadth gate (>= 65 % of charts above their open) sat out both soft
    days, 2R beat 1R, the score card did not help, entries by 11:00 kept
    the same R with half the trades. Built:
    a. scan.py breadth: one quote sweep (200 codes a request, 1,000 gives
       HTTP 414) -> trader/state/breadth.json; gate_time 10:00, 65 %.
    b. check.py: breadth_gate_pct / gate_time / stop_after_loss checks (off
       unless configured); `journal` -> trader/journal/DATE.json; `review`.
    c. timemachine.py universe: the evening replay of the rule on every chart
       of a saved day, every-breakout tally and the paper ledger, --compare
       baseline, --write trader/replays/DATE.json.
    d. trader-budget/paper.example.json: the paper-month config. The paper
       ledger's sells now carry pnl and avg_cost.
    Rule for the month: no win-rate language; the evening replay must
    beat its baseline before any setting is called good. (Superseded
    2026-11-16 by step 12's metrics and go/no-go.)
12. Evidence before money (decided 2026-11-16, after 28 whole-universe days
    showed no stop/target/exit shape with a positive net per trade):
    a. timemachine.py multiday over every saved bundle: the rule plus one
       change at a time; the last 20 days held out (settings fixed on the
       days before, only scored on these). History-based inputs (relative
       volume, the liquid 174 by turnover) from earlier days only.
    b. Gate the trading, not the measurement: universe scores every signal
       on gate-closed days too; only the ledger stays empty.
    c. Honest fills: slippage_pct (0.05 a side) on every market fill in the
       replay and the paper ledger; a stop gapped through fills at the open.
    d. trader-risk: max_charges_pct_of_risk (15) blocks a trade whose
       round-trip charges are over that share of the rupee risk.
    e. review: net R a trade, expectancy with a 95 % range, profit factor,
       max drawdown, weekly net (trader_data.trade_metrics).
    f. GO/NO-GO: no real money until net expectancy is above zero on the
       held-out replay days AND over at least 200 paper trades.
    g. Test selection (liquid set, relative volume, a short side on weak
       days) before more entry rules. VWAP's positive gross on Nov 11 is a
       lead only.
    h. Automate the morning: INDstocks POST /generate/token (x-api-key =
       client id, mpin, totp; one live token, 1 a minute) from
       INDSTOCKS_CLIENT_ID, INDSTOCKS_MPIN, INDSTOCKS_TOTP_SECRET in .env;
       the token stays in memory, never on disk.
13. Stop tuning, test two things on new days (decided 2026-11-17, after 72
    whole-universe days, Aug 3 to Nov 16):
    a. One rule: multiday and universe read the paper config
       (rule_from_config). The paper config logs the 15 % cost check
       (warn_charges_pct_of_risk) instead of blocking (max_charges_pct_of_risk
       null): the blocking cap stopped about 86 % of the rule's signals.
    b. Slippage scales with turnover (slippage_model "turnover": 0.05 % at
       Rs 50 crore a day and up, x sqrt(50 crore / turnover) below, cap 0.5 %).
    c. Every-signal 95 % ranges by day (one total per day); the replay half of
       the go/no-go needs 10+ signal days and the low end above zero.
    d. multiday chooses on the train days and seals every other test half
       (--unseal to look, with a warning).
    e. The long ORB is not tuned further. Two tests are registered in
       trader-timemachine/registered.json (short at or under 30 % breadth on
       INDstocks' intraday-allowed stocks; rel. volume top 20, gate off) and
       scored only on sessions from 2026-11-17 (`timemachine.py registered`,
       run every evening). Verdict after 20 sessions with 10 signal days;
       never edit the registration to rescue a test.
    f. If both fail: point the replay at multi-day holds (wider stops, so the
       charges are a smaller share of the risk).

## Rules

- Run `python3 -m unittest discover -s tests -v` before claiming a step done.
- Keep scripts Python 3.8+, standard library only, like upstream.
- Do not call the live order endpoints during development. Paper only.
- Ask before changing anything in the Decisions section.

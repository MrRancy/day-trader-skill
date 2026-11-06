---
name: trader-broker
description: >-
  Order Executor for NSE. Sends approved orders to your broker, paper trading
  by default, never without your yes. Shows each order as a plain ticket (a
  smart order shows its stop-loss leg, its target leg and the max loss at the
  stop, charges included) plus the exact JSON body for INDstocks'
  POST /smart/order, records your yes word for word, then places it: on the
  paper ledger (trader/paper.json, fills at the quoted last price, legs that
  trigger on a tick, the broker's 15:20 MIS square-off) or, only with
  "paper": false and --live, at INDstocks. Builds the sell ticket that closes
  a day trade by your flat-by time, records fills with their charges, and
  logs each trade's max loss so trader-risk can hold the daily limit. Covers
  the INDstocks token, the static-IP rule for live orders, and keeping
  ticket.py place behind a permission prompt. Use when the user says "place
  the order", "buy it", "execute", "send it to my broker", "close my
  position", "flatten", "get me out", "paper trade it", "set up INDstocks",
  or after /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the market only after three
separate gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user approved the `ticket.py place` command itself** on Claude's
   own permission prompt, because that command is never on an allow list.

Two yeses from the user, one PASS from the rules. No yes, no order. And by
default the order goes to the paper ledger, not to INDstocks at all.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" place --index 1          # paper: fills at the live quote
python3 "${CLAUDE_SKILL_DIR}/ticket.py" paper tick               # legs vs the live quote; square-off after 15:20
python3 "${CLAUDE_SKILL_DIR}/ticket.py" paper show
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol TATACONSUM --qty 71
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol TATACONSUM --side sell --qty 71 --price 1061.00
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`--now "2026-11-03 10:01"` pins the clock (IST). `--price 1050.50` on
`place`, and `--price 1061.20` (or `TATACONSUM=1061.20`, which is needed
once more than one symbol is open) on `paper tick`, give the ledger a price
instead of a quote.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, placed, failed and filled, with each entry's max loss and each fill's charges |
| `trader/paper.json` | the paper ledger: cash, positions, the order book with its GTT legs, charges paid |
| `trader/portfolio.json` | cash and shares, updated by every fill (paper or recorded), charges off |
| `trader/plan.json` | the flat-by time for `flatten` |
| `trader/prices.json` | a fresh price (under 15 minutes) fills a paper order without a quote |

## Setup: INDstocks

INDstocks (indstocks.com) gives every account a free trading API with a flat
Rs 10 brokerage an order. There is no sandbox, so this pack paper trades on
its own ledger until the user turns `"paper"` off.

1. The user generates an access token at
   indstocks.com/app/api-trading/access-tokens. It lasts 24 hours. They put
   it in the `INDSTOCKS_TOKEN` environment variable or in a `.env` file next
   to `trader_data.py`. **Tokens never go in the chat.** Never ask for one,
   never read `.env` back into the conversation, and if the user pastes a
   token anyway, tell them to delete that message and generate a new one.
2. Read-only calls (quotes, candles, the order book, positions, funds) work
   from anywhere. **Placing, modifying or cancelling an order needs the
   account's static IP whitelisted** on that same page (NSE circular
   NSE/INVG/67858): two slots, each changeable once a calendar week. Without
   it a live `place` fails with a plain HTTP error, which is recorded.
3. In Claude Code, keep `ticket.py place` on ask: never add it to an allow
   list in `.claude/settings.json`, and never run it from a scheduled task.
   In Claude Desktop the command runs through the same permission prompt.
4. Going live is three deliberate steps the user takes themselves: `"paper":
   false` in `trader/config.json`, the IP whitelisted at INDstocks, and
   `--live` typed on the `place` command for that one order.

Zerodha Kite Connect (`"broker": "kite"`) is not built yet: a Kite ticket can
be recorded with `ticket.py result` after the user places it in Kite.

**Smart orders at INDstocks.** `POST /smart/order` places the entry
(MARKET orders are converted to a LIMIT at the live price), then a GTT child
order with the stop-loss and target legs (`sl_trigger_price` /
`sl_limit_price`, `tgt_trigger_price` / `tgt_limit_price`). The child
activates when the entry fills; when one leg fills the other is cancelled.
Every price must sit on the stock's tick, the stop-loss limit at or below
its trigger, the target limit at or above. Product INTRADAY is MIS: the
broker squares it off from about 15:20 if the user has not.

## The paper ledger

`trader/paper.json` starts from the config budget on the first `place`.

- A MARKET order fills at the quoted last price: `--price`, else a fresh
  entry in `trader/prices.json`, else a live INDstocks quote (read only). A
  LIMIT buy fills when the price is at or under the limit, else waits.
- The stop-loss and target legs wait until `paper tick` sees a price through
  a trigger: the stop-loss fills at its trigger, the target at its trigger,
  and the other leg is cancelled. A tick is one price per symbol, so it is a
  snapshot: run it after each 5-minute bar, or with the bar's low and high
  in two ticks when a bar touched both.
- After 15:20 a tick squares off every open INTRADAY position at the price
  given, the way the broker does. At 15:30 whatever is still pending expires.
- Every fill pays the real charges (`trader_data.charges_leg`): brokerage,
  STT on the sell, exchange, SEBI, IPFT, stamp duty on the buy, GST. The
  ledger and `trader/portfolio.json` both carry cash net of charges.
- A paper fill at the last price is kinder than a real one: no slippage, no
  partial fills, no gap through the stop. Say so when the user reads it.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "yes to all", or a yes that names a different
   stock or amount). Ask again. Never turn a reply into a yes yourself.
4. Only after "Recorded your yes", run `ticket.py place --index N`. On the
   paper ledger it fills at the quoted price and writes the fill to
   `trader/portfolio.json` and the log. With `"paper": false` it refuses
   unless `--live` is typed; then it sends the body the ticket printed, and
   records placed (with the INDstocks order ids) or failed.
5. Live only: record the fill when you see it, with `ticket.py sync` (the
   order book and positions, read only) and `ticket.py fill`, or `ticket.py
   result` if the user placed it in the INDstocks app instead. The log keeps
   the entry's max loss, charges included, for trader-risk's daily limit.

## Steps: while it is open, and out by the close

- trader-alerts watches the stop, the target and the flat-by time
  (`alerts.py add --from-plan`). The GTT child does the stopping at
  INDstocks; on paper, `paper tick` does.
- When the stop-loss or the target leg fills live, record it:
  `ticket.py fill --symbol TATACONSUM --side sell --qty 71 --price 1061.00`.
- By `flat_by` (15:15 IST), anything still open gets closed, before the
  broker's own square-off at about 15:20:
  1. `ticket.py flatten --symbol TATACONSUM --qty 71` writes a market sell for
     those shares to `trader/orders.json` (the qty from the portfolio, the
     ledger, or `--qty` from the broker).
  2. `/trader-risk`. A sell that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  3. `ticket.py show`, then the user's yes for that one order, then `place`.
  4. On paper the ledger cancels the open legs when the sell fills. Live, the
     GTT child still holds those shares: `ticket.py sync` shows its `GTT-`
     id, cancel it with `POST /smart/order/cancel` (the user approves that
     too), then place the ticket.

## The paper month: one day, start to finish

The routine agreed on 2026-11-05 for the paper weeks, with
`trader-budget/paper.example.json` copied to `trader/config.json` (Rs
10,000 a day, 2 trades, Rs 300 max loss, 2R, entries 10:00 to 11:00, the
breadth gate, the day over after a loss). All times IST.

1. **08:45** `scan.py daily`; `news.py` on the hits that matter. Read, do not trade.
2. **09:35** `scan.py orb` for the early breakouts; watch, take nothing.
3. **10:00** `scan.py breadth`. Closed: the day is a watching day, go to step 7.
   Open: `scan.py orb --at 10:00`, pick at most one chart, `entry.py check`.
4. **On a trigger** `exit.py plan --target 2R`, `timemachine.py run`,
   `size.py --write`, `check.py`. PASS and the user's yes, then
   `ticket.py place`. `alerts.py add --from-plan`.
5. **While open** `alerts.py check` and `ticket.py paper tick` every few
   minutes. A second trade only after the first has closed green and before
   11:00.
6. **15:15** flat, whatever is open.
7. **After 15:30** `scan.py orb --now "DATE 15:00" --save trader/replays/orb5m-DATE.json.gz --no-trend`,
   then `timemachine.py universe trader/replays/orb5m-DATE.json.gz --compare --others --write`,
   then `check.py journal --note "..."` with what went wrong and why.
8. **Friday** `check.py review --days 7`. The month's question is one
   number: net rupees after charges, and whether the rule beat its baseline.

## Example: TATACONSUM, Tue Nov 3 2026

```
ORDER 1 of 1, PAPER ledger (trader/paper.json), INDstocks
  BUY 71 TATACONSUM, INTRADAY (MIS, squared off from about 15:20), market, good for today
  stop-loss trigger 1039.40 (limit 1036.80)
  target trigger 1061.00 (limit 1061.10)
  max loss about Rs 816.81, charges included, if the stop fills at 1039.40 (it can fill worse in a fast market)
  why: opening-range breakout on TATACONSUM: 71 shares, bound by your position cap (71 x 10.80 = Rs 766.80 at the stop, plus Rs 50.01 charges = Rs 816.81)
  READY: trader-risk PASS, waiting for your yes
  The paper ledger will simulate POST /smart/order with exactly:
    {"txn_type": "BUY", "exchange": "NSE", "segment": "EQUITY", "product": "INTRADAY", "order_type": "MARKET",
     "validity": "DAY", "security_id": "3432", "qty": 71, "sl_trigger_price": 1039.4, "sl_limit_price": 1036.8,
     "tgt_trigger_price": 1061.0, "tgt_limit_price": 1061.1, "remarks": "day-trader-india/orb/2026-11-03"}
```

The user replies "yes". Claude records it and runs `ticket.py place --index 1
--price 1050.50`: the ledger buys 71 shares at 1,050.50 for Rs 74,585.50
plus Rs 16.92 charges, cash Rs 25,397.58, and the legs wait. A tick at
1,055.00 moves nothing. A tick at 1,061.20 fills the target leg at 1,061.00:
Rs 75,331 back, Rs 33.54 charges, cash Rs 100,695.04. The `why` and the plan
stay in the file; they are never sent.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats the token.
- Never switches `"paper"` to false or adds `--live` on its own. The user does that.
- Never cancels every order or closes every position on its own.
- Never promises a fill price: a market order and a stop fill at whatever the market gives.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, F&O, margin beyond MIS intraday, short selling or leverage unless the config allows it and the user confirms.
- Broker tokens live in environment variables or a .env file, never in the chat.

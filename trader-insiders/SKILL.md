---
name: trader-insiders
description: >-
  The Insider Tracker for NSE. Watches the trades company insiders must
  disclose under SEBI's insider-trading rules (promoters, directors, key
  managerial personnel, designated employees and their relatives: every buy,
  sell, ESOP, pledge, gift and off-market transfer, with quantity, value,
  holding before and after, and how many days it took to reach the exchange)
  and the bulk and block deals NSE publishes after the close (who bought or
  sold a large block, at what price). Alerts once per new row, flags your
  symbols. Use when the user says "are promoters buying", "insider trades in
  X", "who bought the block", "bulk deals today", "any large deals in my
  watchlist", "alert me when an insider files", or "run the insiders skill".
argument-hint: "[symbols] [--kinds market]"
---

# trader-insiders

India has no Pelosi, but it has something the US does not: insiders must
disclose their trades within two trading days, and the exchange publishes
every bulk and block deal the same evening. This skill reads both feeds from
NSE, turns them into clean rows and tells you only what is new.

It is fast. It is not early. An insider has two trading days to tell the
company and the company two more to tell the exchange, so a disclosure is
days old when it appears. A bulk deal is published after the close of the day
it happened. There is no inside information here: only public filings, read
quickly. Say that every time.

Tool in this folder (standard library Python, no key):

```bash
python3 "${CLAUDE_SKILL_DIR}/insiders.py" --help
```

## What it reads and writes

| feed / file | what |
| --- | --- |
| `nseindia.com/api/corporates-pit?index=equities&symbol=X` | SEBI PIT Regulation 7(2) disclosures, one symbol at a time (the index-wide call answered no rows on 2026-11-05). Each row carries the XBRL the company filed |
| `nseindia.com/api/snapshot-capital-market-largedeal` | today's bulk deals, block deals and short-sale disclosures |
| `nseindia.com/api/historicalOR/bulk-block-short-deals?optionType=bulk_deals&from=..&to=..` | past bulk (or block) deals. NSE answers at most 70 rows a call, oldest date first (Nov 4 had 133 bulk deals; the history gave 70), so the tool asks one trading day at a time, up to 31 days, and says when a day was cut short |
| `trader/watchlist.json`, `trader/plan.json`, `trader/portfolio.json` | your symbols, when `--symbols` is not given |
| `trader/state/insiders.json` | the rows already shown, so each alerts once (kept 400 days) |

nseindia.com wants a browser's headers; the tool sends them and pauses half a
second between calls. BSE's API refuses automated clients (403), so BSE-only
disclosures are not read.

## The rules

- **Say what kind of row it is.** Most disclosures are not market trades.
  Every row is tagged: `market` (a purchase or sale on the exchange), `esop`
  (an allotment or exercise), `pledge` (creation, release or invocation),
  `gift`, `off-market` (a transfer, often between promoter entities),
  `corporate` (rights, bonus, preferential, buyback) or `other`. Only
  `market` rows say an insider paid money for shares on the open market.
  `--kinds market` keeps just those; the market-flow line sums them.
- **Quote the filing.** Every row has the disclosure's XBRL link and the
  disclosure time on NSE. Third-party trackers are fine for browsing; the
  filing wins.
- **Say the delay.** Each row shows the days between the trade and the
  exchange's broadcast; over 7 calendar days is flagged LATE (two trading
  days for the insider, two for the company, plus a weekend).
- **A block is two sides.** Block and bulk deals list the buyer and the
  seller as separate rows at the same price. Name both before reading
  anything into it; a fund selling to a fund is a transfer of ownership, not
  a view.
- **Bulk is not block.** A bulk deal is one client trading more than 0.5% of
  a company's shares in a day (reported after the close). A block deal is one
  trade of Rs 10 crore or more in the exchange's block windows (08:45 to 09:00
  and 14:05 to 14:20). Both name the client; neither says why.

## Step 1: what insiders filed

```bash
python3 "${CLAUDE_SKILL_DIR}/insiders.py" pit TATACONSUM RELIANCE
python3 "${CLAUDE_SKILL_DIR}/insiders.py" pit RELIANCE --kinds market --min-value 1000000
python3 "${CLAUDE_SKILL_DIR}/insiders.py" pit --since 2026-10-01          # your watchlist
```

Newest first, with a line on net market buying or selling by insiders over
the last 90 days (`--flow-days`). Exit code 1 means no rows matched.

## Step 2: today's large deals

```bash
python3 "${CLAUDE_SKILL_DIR}/insiders.py" deals                          # your symbols, today
python3 "${CLAUDE_SKILL_DIR}/insiders.py" deals --all-symbols            # everything NSE published
python3 "${CLAUDE_SKILL_DIR}/insiders.py" deals --from 2026-10-06 --to 2026-11-05 --symbols ICICIBANK
```

NSE posts the day's bulk and block deals in the evening; `deals` without
dates shows that snapshot (dated, so you can see it is yesterday's until the
new one lands). It is the complete list. The dated history is read a day at
a time and NSE cuts each day at 70 rows, so a busy day comes back short and
the tool says so; `new` on a nightly schedule keeps a full record. `--short`
adds the short-sale disclosures (quantity per symbol, no names).

## Step 3: watch for new ones

```bash
python3 "${CLAUDE_SKILL_DIR}/insiders.py" new
python3 "${CLAUDE_SKILL_DIR}/insiders.py" new --all-deals --json
```

Shows only disclosures and deals not seen at the last check (the first run
shows the last 30 days, `--days`), for your symbols, and records them in the
state file. Exit code 1 means nothing new. Companies file disclosures all
day and NSE posts deals after the close, so `/loop 1h` on weekdays, or a
scheduled task at 19:00 IST, is plenty.

## Example: TATACONSUM and RELIANCE, read 2026-11-05

`insiders.py pit TATACONSUM`: twenty rows, all ESOP allotments and two
market sales by designated employees, nothing from a promoter. The newest:

```
2026-02-24  Key Managerial Personnel SIVAKUMAR SIVASANKARAN: bought 1,736 equity shares (ESOP),
            worth Rs 20.20 lakh at about Rs 1,163.59, holding 0 -> 1,736, traded 2026-02-20,
            disclosed 4 days later
Market trades by insiders in the last 90 days: none
```

`insiders.py pit RELIANCE --kinds market` finds eight market sales by
immediate relatives of insiders since 2021, the largest 100,000 shares at
Rs 1,385 on 2025-09-12 (Rs 13.85 crore), and one 500-share market purchase.
The promoter-group row of 2023-08-22 is a 24.09 crore-share off-market
transfer at no value: a reorganisation of holdings, not a trade.

`insiders.py deals --all-symbols` on Nov 4: 133 bulk deals and 2 block
deals; the block was Cello Pens buying 25 lakh AJMERA shares from ValueQuest
at Rs 141 (Rs 35.25 crore), and the same buyer appears in the bulk list.
Neither TATACONSUM nor RELIANCE had a large deal that day.

Then: "These are public disclosures, days old. They say what an insider or a
fund did, not what anyone will do next."

If the user wants to act on it, it goes the long way: a disclosure is a
reason to look at a chart, not a setup. `/trader-scanner` and
`/trader-entry` decide if there is a trade today, sized by `/trader-budget`,
checked by `/trader-risk`, placed by `/trader-broker` only after their yes.

## What it never does

- Never says or implies an insider has information the market lacks, or that
  copying insiders or bulk-deal buyers beats the market. No return claims.
- Never treats an ESOP allotment, a pledge or a gift as a purchase.
- Never names a buyer or seller the filing does not name.
- Never places an order.

## Safety

- Education and tooling, not financial advice. Nothing in this pack is a
  recommendation to buy or sell anything.
- Paper trading by default (`"paper": true` in `trader/config.json`).
- Nothing places an order except `/trader-broker`, and only after
  `/trader-risk` passes AND the user types an explicit yes for that order in
  the chat, with the broker tool on its own permission prompt. Never
  allow-list an order tool.
- Insider disclosures are PUBLIC filings under SEBI's PIT Regulations, made
  up to four trading days after the trade. This skill reads them fast; it has
  no inside information, and it says so.

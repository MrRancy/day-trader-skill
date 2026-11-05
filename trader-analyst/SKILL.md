---
name: trader-analyst
description: >-
  The Results Analyst for NSE. Reads a company's quarterly numbers straight
  from the Ind-AS XBRL it filed with the exchange (SEBI's integrated filing,
  consolidated when available) and sums up what changed: revenue from
  operations, EBITDA, profit-before-tax and net margins, net profit and
  diluted EPS, quarter by quarter, each against the same quarter a year
  earlier, with the trailing twelve months. Finds the next results date from
  NSE's board-meeting intimations. Reads an earnings call transcript when
  you paste one. Use when the user says "how were Tata Consumer's results",
  "analyse the quarter", "what changed this quarter", "is revenue growing",
  "check the results", "compare margins", "when are results due", or before
  trading a stock around its results.
argument-hint: "[symbols]"
---

# trader-analyst

Results move stocks more than anything else on the calendar, and the press
release is written to put the best number first. This skill reads the
numbers the company actually filed with NSE, lines them up quarter by
quarter, and says plainly what changed. For a day trader it answers two
questions before a trade: what did the last quarter say, and is the next
one inside this trade?

Tool in this folder (standard library Python, no key, no login):

```bash
python3 "${CLAUDE_SKILL_DIR}/results.py" --help
```

## What it reads and writes

- Reads `nseindia.com/api/integrated-filing-results?index=equities&symbol=X&period_ended=all&type=Integrated Filing- Financials`
  (every quarterly filing since the quarter ended March 2025, with the XBRL
  link), `nseindia.com/api/corporates-financial-results?index=equities&symbol=X&period=Quarterly`
  (the older feed, up to December 2024, read when more history is needed),
  each filing's XBRL at `nsearchives.nseindia.com/corporate/xbrl/`, and
  `nseindia.com/api/corporate-board-meetings?index=equities&symbol=X` for
  the next results date. BSE's API refuses automated clients (403); the XBRL
  a company files is the same document on both exchanges.
- Writes `trader/metrics.json` with `--metrics`: one row per company (TTM
  revenue, net profit and diluted EPS, the latest margins and growth), for
  your notes or a spreadsheet. `metrics.example.json` in this folder is a
  real run for TATACONSUM, RELIANCE and HDFCBANK on 2026-11-05.
- Earnings calls: the tool does not fetch transcripts. If the user pastes one
  (or the company's investor page has it), read it and sum up what
  management said changed: guidance, margins, demand, anything they declined
  to answer.

## The rules

- **Filed numbers first.** The table comes from the XBRL the company tagged
  and filed. Use the press release and the investor presentation for context
  (guidance, segment colour), and say which numbers came from where.
- **Compare like with like.** Growth is against the same quarter a year
  earlier. Quarter-on-quarter changes are shown but many businesses are
  seasonal (a tea company's December quarter, a bank's March quarter).
- **Consolidated unless told otherwise.** Group numbers are used when the
  company files them; `--standalone` reads the parent alone. Say which.
- **EBITDA is derived.** Profit before exceptional items and tax, plus
  finance costs, plus depreciation, from the tagged lines. A company's own
  "EBITDA" in its release may add back other things; say so when they differ.
- **Flag one-offs.** Exceptional items, a tax credit, or other income that is
  a large share of profit before tax are each called out. Banks tag a
  different taxonomy: revenue is interest earned, there is no EBITDA, and the
  tool shows operating profit before provisions and the provisions instead.
- **EPS across a bonus or split.** The tool compares the EPS each filing
  reported. After a bonus issue the year-earlier EPS is the unadjusted
  figure (HDFCBANK's 2025 bonus shows as a 40 to 46 percent EPS drop while
  profit grew). When EPS growth and profit growth disagree by that much, say
  why and quote the profit line.
- **Audited or not.** March-quarter filings are audited; the others are
  limited reviews. The table marks the audited ones.

## Steps

1. Run it for the companies in question (or the symbols on
   `trader/watchlist.json`):

   ```bash
   python3 "${CLAUDE_SKILL_DIR}/results.py" TATACONSUM
   python3 "${CLAUDE_SKILL_DIR}/results.py" TATACONSUM RELIANCE --metrics trader/metrics.json
   ```

   `--quarters 8` shows two years, `--json` everything machine-readable,
   `--standalone` the parent company alone. The tool fetches four quarters
   more than it shows, for the year-earlier comparisons, about one XBRL every
   half second.
2. Open the latest filing (the iXBRL link is the human-readable results
   statement) and the investor presentation when the numbers need context,
   and read them.
3. Write the summary: one line on the quarter (revenue, EPS, growth), one on
   margins, one on anything unusual (an exceptional item, a bank's
   provisions, a bonus-adjusted EPS), the next results date, and the links.
   Plain words, real numbers, no adjectives the numbers do not support.
4. If the next results fall inside the planned trade (an overnight CNC hold,
   or a board meeting before the open), say it before `/trader-exit` sets the
   stop: a stop does not protect against a gap, the stock can open past it.
   An intraday MIS trade squared off by 15:15 is not exposed to a result
   published after the close.

## Example: the chain's stock, TATACONSUM (run 2026-11-05)

```
quarter end fiscal      revenue  rev yoy  ebitda     pbt     net net profit  EPS dil EPS yoy
2026-09-30  Q2 FY27     5,661.1   +14.0%   14.5%   11.2%    8.3%      470.1     4.74  +15.9%
2026-06-30  Q1 FY27     5,348.9   +11.9%   14.9%   11.1%    8.0%      427.2     4.31  +27.9%
2026-03-31  Q4 FY26     5,433.6   +17.9%   15.6%   11.8%    7.8%      424.0     4.24  +21.5%  audited
```

Quarter ending 2026-09-30 (Q2 FY27, unaudited, consolidated): revenue Rs
5,661.13 crore, +14.0% against a year earlier; net profit Rs 470.14 crore
(+15.7%); diluted EPS Rs 4.74 (+15.9%). EBITDA margin 14.5%, 0.4 points
below the June quarter. Exceptional items of Rs -7.37 crore sit inside
profit before tax. Last four quarters: Rs 21,555.63 crore revenue, Rs 17.17
EPS. The results were approved at the board meeting of Oct 26 (intimated Oct
19); no next meeting is intimated yet. So for the Nov 3 trade in
`/trader-entry`: "Results are a week old and the next are three months out.
Nothing on the calendar inside an intraday trade."

The same run for RELIANCE: revenue Rs 332,861 crore (+28.6%), net profit Rs
29,550 crore (+33.8%), EBITDA margin 18.7%. For HDFCBANK the tool says it
is a bank, shows operating profit before provisions (Rs 32,871.54 crore)
and provisions (Rs 4,209 crore), flags an exceptional item of Rs -837.06
crore, and the EPS comparison is distorted by the 2025 bonus issue.

## What it never does

- Never says a stock is cheap, expensive, a buy or a sell. It reports what
  the company filed.
- Never invents a number the filings do not have. When a quarter's XBRL is
  missing or unreadable it says so and leaves the row out.
- Never hammers NSE: browser headers, one request at a time, a pause between
  calls.

## Safety

Education and tooling, not financial advice. This skill only reads filings.
Nothing places an order except `/trader-broker`, after `/trader-risk` passes
and the user types an explicit yes for that order.

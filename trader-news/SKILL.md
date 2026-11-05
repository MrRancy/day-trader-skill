---
name: trader-news
description: >-
  News Monitor for NSE. Tracks breaking news on every stock you watch and
  flags what moves it: headlines per company (Yahoo Finance's .NS feed, then
  Google News when Yahoo is empty), each company's own filings on NSE's
  corporate-announcements feed (results, board meetings, management changes,
  press releases, with the PDF) and how far each stock is from the previous
  close. Shows only what is new since the last check, newest first, in IST,
  tags each item (earnings, analyst, deal, flows, halt, regulatory, macro...)
  and never repeats one. Use when the user says "any news on RELIANCE", "why
  is this moving", "what's the news on my watchlist", "morning briefing",
  "pre-market news", "monitor the news", "did anything break", "check the
  exchange filings", or after /trader-scanner finds setups.
argument-hint: "[symbols]"
---

# trader-news

A chart can look perfect and still be about to gap on a results date, a
circuit halt or a SEBI order. Before a setup becomes a trade, this skill
reads the feeds that carry those first, for every stock on the watchlist,
and tells you only what is new. It also prints how far each stock has moved
today, so a headline and a move sit side by side.

Tool in this folder (standard library Python; INDstocks' token only for the
price line):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                         # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --symbols TATACONSUM RELIANCE --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-nse --json
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--symbols`, else `trader/watchlist.json` | the scanner's hits (trader-scanner) |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold (trader-broker records fills there) |
| `trader/universe.txt` | company names for the headline search (trader-scanner writes it; `--master FILE` fills gaps) |
| Yahoo Finance headline RSS | `feeds.finance.yahoo.com/rss/2.0/headline?s=RELIANCE.NS&region=IN&lang=en-IN`: tried first; it answered an empty feed for every NSE symbol on 2026-11-05 |
| Google News RSS | `news.google.com/rss/search?q="Reliance Industries"&hl=en-IN&gl=IN&ceid=IN:en`: the working headline source, used when Yahoo is empty; each item names its publisher |
| NSE corporate announcements | `nseindia.com/api/corporate-announcements?index=equities&symbol=RELIANCE`: the exchange's own filings feed, with the category and the PDF link. BSE's API refuses automated clients (403), so BSE is not read |
| INDstocks quotes, through `trader_data.py` | the last price against the previous close |
| `trader/state/news.json` | what has been shown (kept 30 days) |

## The rules

- **Headlines are leads, not facts.** Before telling the user something
  happened, open the story or the filing and read it. Quote the source and
  link it. Never summarise a paywalled story you could not read: say so.
  Google News links are 400-character redirects: the text output cuts them
  at 80 characters, and `--json` (or `trader/state/news.json`) has the whole
  link to open.
- **Filings beat headlines.** If an NSE announcement and a headline disagree,
  the announcement is what the company told the exchange.
- **Say what it touches.** Every item lists the symbols it touches and its
  tags: earnings, analyst, legal, deal, capital, people, risk, halt,
  regulatory, macro, flows. Filings marked `!` carry an important category:
  outcome of a board meeting, financial results, a change in management, a
  press release, an acquisition, a credit rating, a regulator's order, a
  buyback, rights, bonus, split, dividend, fund raising, delisting or
  suspension. Newspaper copies, investor meets, ESOP notices and trading
  window closures are listed without the mark.
- **MOVING** marks a stock 3 % or more from the previous close
  (`--move-pct`). A move with no headline is worth saying out loud too, and
  so is a move that stops exactly at a circuit band.
- **No predictions.** Report what was published and how far the price is from
  the previous close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone.
2. Read the items that matter: every `!` filing, anything tagged earnings,
   halt, legal, regulatory, flows or risk, and every MOVING symbol first.
3. Write a short brief, per symbol: what happened, the source and link, the
   move, and one line on why it matters to the setup (for example "results
   are after the close today: the trade is in and out before 15:15 anyway").
4. If a story changes the picture, say so before `/trader-entry` and
   `/trader-exit`: a results date inside the trade, an ASM or GSM listing, a
   circuit halt, a block deal. Never act on a headline alone.

During the session: `/loop 15m /trader-news`, or a scheduled task each
morning before 09:15.

## Example: TATACONSUM, Thu Nov 5 2026

A check at 12:00 IST with the saved feeds
(`tests/fixtures/trader-news`): Yahoo's `.NS` feed is empty, so the
headlines come from Google News. Four items are new in the last 48 hours:
Moneycontrol on Nomura flagging margin pressure across consumer names
(tagged earnings, analyst, macro), a Storyboard18 interview, Global Coffee
Report on Tata Starbucks' quarter (earnings), and the company's own NSE
filing of Nov 4, a newspaper copy with the PDF. The price line reads
`TATACONSUM 1,068.00 -0.47% vs the previous close 1,073.00`. Two weeks back
the filings carry the marks that matter: two "Outcome of Board Meeting"
notices, a "Change in Management" and a press release, all on Oct 26, the
results day. The brief: "Results are out, the management change is filed;
no results risk inside today's trade. No ASM or GSM flag." When a symbol has
no news, say so rather than padding the brief.

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a
  headline.
- Never hammers a server: one request per feed per symbol, a pause between
  calls, browser headers for nseindia.com and nothing more.

## Safety

Education and tooling, not financial advice. This skill only reads and
reports. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.

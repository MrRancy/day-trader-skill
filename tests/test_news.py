"""trader-news tests. Fixtures in tests/fixtures/trader-news/, all read on 2026-11-05:

yahoo-TATACONSUM.xml   Yahoo Finance's headline RSS for TATACONSUM.NS: the real answer, an empty feed
gnews-TATACONSUM.xml   Google News RSS for "Tata Consumer", the first 12 of 40 items
gnews-RELIANCE.xml     Google News RSS for "Reliance Industries", the first 12 of 98 items
nse-TATACONSUM.json    NSE corporate announcements for TATACONSUM, the newest 25 of 1,660
quote-RELIANCE.json    a real INDstocks full quote (Nov 4 17:59 IST)
quote-TATACONSUM.json  a full quote built from the Nov 4 daily bar, in the API's shape
"""

import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT, load

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
NOW = "2026-11-05T12:00:00+05:30"
UTC = datetime.timezone.utc


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_long_links_are_cut_on_the_terminal_only(self):
        items = news.parse_rss(raw("gnews-TATACONSUM.xml"), "TATACONSUM", "Google News")
        link = items[0]["link"]
        self.assertGreater(len(link), 300)
        short = news.short_link(link)
        self.assertEqual(short, link[:80] + "... (the full link is in --json)")
        self.assertEqual(news.short_link("https://nsearchives.nseindia.com/x.pdf"), "https://nsearchives.nseindia.com/x.pdf")
        self.assertEqual(news.short_link(None), "")
        news.tag(items[:1], {"TATACONSUM": "Tata Consumer"})
        text = news.render(items[:1], [], datetime.datetime(2026, 11, 5, 6, 30, tzinfo=UTC))
        self.assertIn(short, text)
        self.assertNotIn(link, text)
        self.assertEqual(news._json_ready(items[:1])[0]["link"], link)   # whole in the JSON

    def test_yahoo_is_empty_for_nse_symbols(self):
        self.assertEqual(news.parse_rss(raw("yahoo-TATACONSUM.xml"), "TATACONSUM"), [])

    def test_google_news_rss(self):
        items = news.parse_rss(raw("gnews-TATACONSUM.xml"), "TATACONSUM", "Google News")
        self.assertEqual(len(items), 12)
        first = items[0]
        self.assertTrue(first["title"].startswith("'Best creator partnerships borrow credibility, not reach'"))
        self.assertFalse(first["title"].endswith("Storyboard18"))       # the publisher is split off the title
        self.assertEqual((first["source"], first["feed"], first["kind"]), ("Storyboard18", "Google News", "headline"))
        self.assertEqual(first["time"], datetime.datetime(2026, 11, 5, 3, 36, 40, tzinfo=UTC))
        self.assertTrue(first["link"].startswith("https://news.google.com/"))
        self.assertTrue(first["id"].startswith("rss:"))
        self.assertNotIn("<", first["summary"])

    def test_nse_announcements(self):
        items = news.parse_nse(raw("nse-TATACONSUM.json"), "TATACONSUM")
        self.assertEqual(len(items), 25)
        first = items[0]
        self.assertEqual(first["title"], "Tata Consumer Products Limited: Copy of Newspaper Publication")
        self.assertEqual((first["source"], first["kind"], first["category"]), ("NSE", "filing", "Copy of Newspaper Publication"))
        self.assertFalse(first["important"])
        self.assertEqual(first["time"], datetime.datetime(2026, 11, 4, 7, 21, 39, tzinfo=UTC))   # 12:51:39 IST
        self.assertTrue(first["link"].endswith(".pdf"))
        self.assertEqual(first["id"], "nse:106843826")
        marked = [i for i in items if i["important"]]
        self.assertEqual([i["category"] for i in marked],
                         ["Change in Management", "Press Release", "Outcome of Board Meeting", "Outcome of Board Meeting",
                          "Credit Rating"])
        self.assertTrue(all(i["time"].astimezone(news.IST).date().isoformat() == "2026-10-26" for i in marked[:4]))
        with self.assertRaises(news.NewsError):
            news.parse_nse(b"<html>", "TATACONSUM")

    def test_times(self):
        self.assertEqual(news.parse_time("Thu, 05 Nov 2026 03:36:40 GMT").hour, 3)
        self.assertEqual(news.parse_time("2026-11-03T10:00:00+05:30").hour, 4)
        self.assertEqual(news.parse_time("04-Nov-2026 12:51:39").isoformat(), "2026-11-04T07:21:39+00:00")
        self.assertIsNone(news.parse_time("soon"))
        self.assertEqual(news.to_ist(datetime.datetime(2026, 11, 5, 6, 30, tzinfo=UTC)).hour, 12)

    def test_search_names(self):
        self.assertEqual(news.search_name("Tata Consumer Products Ltd", "TATACONSUM"), "Tata Consumer Products")
        self.assertEqual(news.search_name("Reliance Industries Limited", "RELIANCE"), "Reliance Industries")
        self.assertEqual(news.search_name("Mahindra & Mahindra Ltd.", "M&M"), "Mahindra & Mahindra")
        self.assertEqual(news.search_name("", "RELIANCE"), "RELIANCE")
        master = os.path.join(FIX, "trader_data", "instruments-equity-2026-11-04.csv.gz")
        self.assertEqual(news.names_from_master(["TATACONSUM", "NOPE"], master, "/nonexistent"),
                         {"TATACONSUM": "Tata Consumer Products Ltd"})


class Tagging(unittest.TestCase):
    def test_touches_and_tags(self):
        items = news.parse_rss(raw("gnews-TATACONSUM.xml"), "TATACONSUM", "Google News")
        watch = {"TATACONSUM": "Tata Consumer Products Ltd", "MARICO": "Marico Ltd", "HINDUNILVR": "Hindustan Unilever Ltd"}
        news.tag(items, watch)
        nomura = next(i for i in items if i["title"].startswith("Nomura flags"))
        self.assertEqual(nomura["touches"], ["MARICO", "TATACONSUM"])
        for t in ("earnings", "analyst", "macro"):
            self.assertIn(t, nomura["tags"])

    def test_filings_are_tagged(self):
        items = news.tag(news.parse_nse(raw("nse-TATACONSUM.json"), "TATACONSUM"), {"TATACONSUM": None})
        board = next(i for i in items if i["category"] == "Outcome of Board Meeting")
        self.assertEqual(board["tags"][0], "earnings")
        mgmt = next(i for i in items if i["category"] == "Change in Management")
        self.assertEqual(mgmt["tags"][0], "people")

    def test_dedupe_merges_the_same_story(self):
        a = news.parse_rss(raw("gnews-TATACONSUM.xml"), "TATACONSUM", "Google News")
        b = news.parse_rss(raw("gnews-TATACONSUM.xml"), "MARICO", "Google News")
        items = news.dedupe(news.tag(a + b, {"TATACONSUM": None, "MARICO": None}))
        self.assertEqual(len(items), 12)
        self.assertEqual(items[0]["touches"], ["MARICO", "TATACONSUM"])

    def test_day_trading_tags(self):
        items = [{"id": "x", "source": "x", "feed": "x", "kind": "headline", "symbol": "RELIANCE",
                  "title": "Trading halted at the upper circuit after a block deal; SEBI order on the promoter", "summary": "",
                  "link": "", "time": None}]
        tags = news.tag(items, {"RELIANCE": None})[0]["tags"]
        for t in ("halt", "flows", "regulatory", "people"):
            self.assertIn(t, tags)


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        none = os.path.join(self.tmp.name, "none.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW, "--portfolio", none,
                     "--watchlist", none, "--plan", none, "--universe", none]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--symbols", "TATACONSUM", "--hours", "48", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(data["items"]), 4)                # 3 Google News headlines, 1 NSE filing
        self.assertEqual([i["source"] for i in data["items"]], ["Moneycontrol.com", "Storyboard18", "Global Coffee Report", "NSE"])
        self.assertEqual(data["moves"], [{"symbol": "TATACONSUM", "price": 1068.0, "prev_close": 1073.0, "pct": -0.47,
                                          "time": "2026-11-05T06:30:00+00:00", "moving": False}])
        self.assertEqual(data["notes"], [])
        code, out, _ = run(["--symbols", "TATACONSUM", "--hours", "48"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)
        self.assertIn("TATACONSUM   1,068.00 -0.47% vs the previous close 1,073.00 at 12:00 IST", out)

    def test_filings_show_in_a_wider_window(self):
        code, out, err = run(["--symbols", "TATACONSUM", "--hours", "336", "--no-moves"] + self.base)
        self.assertEqual(code, 0, err)
        marked = [l for l in out.splitlines() if l.startswith("! ")]
        self.assertEqual(len(marked), 3)        # the two same-titled board-meeting notices of Oct 26 merge into one
        self.assertIn("! 2026-10-26 17:40 IST  TATACONSUM   NSE              Tata Consumer Products Limited: Change in Management", out)
        self.assertIn("tags: people", out)
        self.assertIn("nsearchives.nseindia.com", out)
        self.assertIn("Not financial advice", out)
        self.assertIn("Checked 2026-11-05 12:00 IST", out)
        self.assertNotIn(chr(0x2014), out)

    def test_symbols_from_watchlist_and_plan(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [{"symbol": "TATACONSUM", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "trader-exit", "symbol": "RELIANCE"}, fh)
        self.assertEqual(sorted(news.watch_from_files("/nonexistent.json", wl, plan)), ["RELIANCE", "TATACONSUM"])
        args = list(self.base)
        args[args.index("--watchlist") + 1] = wl
        code, out, err = run(["--json", "--dry-run", "--hours", "48"] + args)
        self.assertEqual(code, 0, err)
        touched = set(t for i in json.loads(out)["items"] for t in i["touches"])
        self.assertEqual(touched, {"TATACONSUM"})
        self.assertFalse(os.path.exists(self.state))

    def test_no_symbols_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no symbols", err)

    def test_reliance_three_days(self):
        code, out, err = run(["--symbols", "RELIANCE", "--hours", "72", "--json", "--dry-run"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data["items"]), 9)
        self.assertEqual(data["items"][0]["title"], "Reliance Industries Retains 'A-' Credit Rating From S&P Global")
        self.assertEqual(data["moves"][0]["price"], 1311.0)
        self.assertEqual(data["notes"], [])          # no NSE file for RELIANCE offline: no note, no filings
        self.assertTrue(all(i["kind"] == "headline" for i in data["items"]))

    def test_a_bare_now_is_ist(self):
        code, out, _ = run(["--symbols", "TATACONSUM", "--hours", "48", "--no-moves", "--dry-run", "--offline", DIR,
                            "--state", self.state, "--now", "2026-11-05T12:00:00", "--universe", "/nonexistent"])
        self.assertIn("Checked 2026-11-05 12:00 IST", out)
        code, _, err = run(["--symbols", "TATACONSUM", "--now", "noon", "--offline", DIR])
        self.assertEqual(code, 2)
        self.assertIn("--now wants", err)

    def test_moves(self):
        now = news.parse_time(NOW)
        m = news.parse_quote(raw("quote-RELIANCE.json"), "RELIANCE", now)
        self.assertEqual((m["price"], m["prev_close"], m["pct"]), (1311.0, 1307.9, 0.24))
        quiet = dict(m, pct=1.2)
        self.assertNotIn("MOVING", news.render_moves([quiet])[0])
        self.assertIn("MOVING", news.render_moves([quiet], move_pct=1.0)[0])
        with self.assertRaises(news.NewsError):
            news.parse_quote(b'{"status": "error"}', "ZZZZ", now)

        class Fake(object):
            def resolve(self, s):
                return "NSE_1"

            def quote(self, syms):
                return {"RELIANCE": {"ltp": 1350.0, "prev_close": 1307.9}}
        got = news.collect_moves(news.Fetcher(), {"RELIANCE": None}, now, backend=Fake())
        self.assertEqual((got[0]["pct"], got[0]["price"]), (3.22, 1350.0))
        self.assertIn("MOVING", news.render_moves(got)[0])

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-08-01T00:00:00+00:00"}}
        now = news.parse_time(NOW)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-news", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-news\n"))
        low = " ".join(text.lower().split())
        for must in ("google news", "nse corporate announcements", "yahoo", "bse", "403", "leads, not facts",
                     "moving", "not financial advice", "ist"):
            self.assertIn(must, low)
        for never in ("—", "sec edgar", "8-k", "new york"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()

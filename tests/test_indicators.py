"""Step 10: the indicator setups. The maths lives in trader_data.py; the scanner, the exit planner and the
Time Machine read it. Fixtures are the ones the other tests use:

tests/fixtures/trader-scanner/daily-2026-11-04.json.gz        174 real daily charts, 100 days to Wed Nov 4 2026
tests/fixtures/trader-scanner/orb5m-2026-11-03.json.gz        174 real 5-minute charts, Tue Nov 3 2026
tests/fixtures/trader-entry/TATACONSUM-2026-11-03-{5m,1m}.json  the chain's session
tests/fixtures/trader-timemachine/TATACONSUM-100d5m.json.gz    68 full sessions of 5-minute bars to Nov 2
"""

import datetime
import io
import json
import os
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

scan = load("trader-scanner", "scan")
exitp = load("trader-exit", "exit")
tm = load("trader-timemachine", "timemachine")
td = scan.trader_data
SDIR = os.path.join(FIX, "trader-scanner")
DAILY = os.path.join(SDIR, "daily-2026-11-04.json.gz")
ORB = os.path.join(SDIR, "orb5m-2026-11-03.json.gz")
BARS5 = os.path.join(FIX, "trader-entry", "TATACONSUM-2026-11-03-5m.json")
BARS1 = os.path.join(FIX, "trader-entry", "TATACONSUM-2026-11-03-1m.json")
POOL = os.path.join(FIX, "trader-timemachine", "TATACONSUM-100d5m.json.gz")


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class Maths(unittest.TestCase):
    """Textbook checks on series whose answers are known by hand."""

    def test_averages(self):
        self.assertEqual(td.sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])
        e = td.ema([float(x) for x in range(1, 61)], 5)
        self.assertEqual(e[:4], [None] * 4)
        self.assertEqual(e[4], 3.0)                                   # seeded with the SMA of 1..5
        self.assertAlmostEqual(e[-1], 58.0, places=6)                 # lags a straight line by (n-1)/2
        self.assertEqual(td.wilder([2.0] * 20, 14)[-1], 2.0)
        self.assertEqual(td.ema([1.0, 2.0], 5), [None, None])

    def test_rsi_matches_the_scanner(self):
        closes = [float(x) for x in range(1, 40)]
        self.assertEqual(td.rsi_series(closes)[-1], 100.0)
        doc = scan.load_offline(DAILY)[0]
        c = [b["c"] for b in doc["RELIANCE"]]
        self.assertAlmostEqual(td.rsi_series(c)[-1], scan.rsi(c), places=9)
        self.assertEqual(td.rsi_series(c)[:14], [None] * 14)

    def test_true_range_atr_and_supertrend(self):
        c = [float(x) for x in range(1, 61)]
        h, l = [x + 1 for x in c], [x - 1 for x in c]
        self.assertEqual(td.true_ranges(h, l, c), [2.0] * 60)          # high - previous close is also 2
        gap = td.true_ranges([10, 20], [9, 19], [9.5, 19.5])
        self.assertEqual(gap, [1.0, 10.5])                              # a gap: high minus the previous close
        self.assertAlmostEqual(td.atr_series(h, l, c, 14)[-1], 2.0, places=9)
        self.assertEqual(td.atr_series(h, l, c, 14)[:13], [None] * 13)
        short = td.atr_series(h[:5], l[:5], c[:5], 14)                 # fewer bars: the plain average, last bar only
        self.assertEqual(short, [None, None, None, None, 2.0])
        line, d = td.supertrend(h, l, c)
        self.assertEqual(d[-1], 1)
        self.assertAlmostEqual(line[-1], 60.0 - 3.0 * 2.0, places=6)   # hl2 - 3 x ATR in an uptrend
        down = list(reversed(c))
        line, d = td.supertrend([x + 1 for x in down], [x - 1 for x in down], down)
        self.assertEqual(d[-1], -1)
        self.assertGreater(line[-1], down[-1])

    def test_macd_adx_bollinger(self):
        c = [float(x) for x in range(1, 61)]
        h, l = [x + 1 for x in c], [x - 1 for x in c]
        line, sig, hist = td.macd(c)
        self.assertAlmostEqual(line[-1], 7.0, places=6)                # EMA12 - EMA26 of a straight line: 5.5 + 12.5
        self.assertAlmostEqual(hist[-1], 0.0, places=6)
        self.assertEqual(line[:25], [None] * 25)
        a = td.adx_series(h, l, c)
        self.assertEqual(round(a["plus_di"][-1], 6), 50.0)            # +DM 1 on a TR of 3... wait, 1/2 of the 2 smoothed
        self.assertEqual(a["minus_di"][-1], 0.0)
        self.assertEqual(round(a["adx"][-1], 6), 100.0)
        bb = td.bbands(c)
        sd = (sum((x - 50.5) ** 2 for x in range(41, 61)) / 20) ** 0.5
        self.assertAlmostEqual(bb["upper"][-1], 50.5 + 2 * sd, places=9)
        self.assertAlmostEqual(bb["width"][-1], 4 * sd / 50.5, places=9)
        self.assertEqual(bb["middle"][:19], [None] * 19)

    def test_crossed_above(self):
        fast = [1, 1, 3, 3, 3]
        slow = [2, 2, 2, 2, 2]
        self.assertTrue(td.crossed_above(fast, slow, 3))
        self.assertFalse(td.crossed_above(fast, slow, 1))             # the cross was two bars ago
        self.assertFalse(td.crossed_above([None, 3], [None, 2], 3))

    def test_score_card(self):
        c = [float(x) for x in range(1, 61)]
        h, l = [x + 1 for x in c], [x - 1 for x in c]
        s = td.score_card(h, l, c)
        self.assertEqual((s["score"], s["of"]), (4, 6))
        self.assertEqual([k for k, v in s["parts"].items() if not v], ["rsi_under_70", "macd_above_signal"])
        self.assertIsNone(td.score_card(h[:50], l[:50], c[:50]))


class DailyScan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = scan.load_offline(DAILY)[0]
        cls.syms = sorted(cls.data)
        cls.now = scan.parse_now("2026-11-05 08:30")
        cls.hits, cls.counts, _ = scan.scan_daily(cls.data, cls.syms, cls.now, 50, 500000)
        cls.by = dict((h["symbol"], h) for h in cls.hits)

    def test_counts(self):
        self.assertEqual((self.counts["scanned"], len(self.hits)), (149, 108))      # 82 before the indicator setups
        per = Counter(t for h in self.hits for t in h["setups"])
        self.assertEqual((per["ema_cross"], per["supertrend_flip"], per["volume_spike"], per["bb_squeeze"],
                          per["bb_release"]), (34, 22, 18, 15, 7))
        self.assertEqual((per["breakout_20d"], per["bull_flag"], per["reclaim_ma20"], per["oversold"],
                          per["oversold_bounce"]), (57, 2, 22, 1, 2))                 # unchanged
        self.assertEqual(sorted(Counter(h["score"] for h in self.hits).items()), [(2, 2), (3, 9), (4, 27), (5, 65), (6, 5)])
        self.assertEqual([h["symbol"] for h in self.hits[:3]], ["HDFCBANK", "RELIANCE", "BHARTIARTL"])

    def test_four_tags_on_one_chart(self):
        bel = self.by["BEL"]
        self.assertEqual(bel["setups"], ["breakout_20d", "supertrend_flip", "volume_spike", "bb_release"])
        self.assertEqual((bel["score"], bel["ema20"], bel["ema50"]), (5, 377.12, 385.62))
        self.assertFalse(bel["score_parts"]["ema20_above_ema50"])
        self.assertEqual(self.by["ADANIPORTS"]["score"], 6)
        self.assertEqual(self.by["SUNPHARMA"]["setups"], ["volume_spike", "bb_squeeze"])
        self.assertEqual(self.by["RELIANCE"]["setups"], ["breakout_20d", "ema_cross"])
        self.assertNotIn("TATACONSUM", self.by)                       # no setup on Nov 4

    def test_rules_on_made_up_bars(self):
        n = 60
        base = [100.0 + 0.5 * (i % 2) for i in range(n)]                # a quiet wiggle: a constant band width
        c = base[:-3] + [101.0, 103.0, 105.0]                           # a late pop: EMA10 crosses EMA20
        h, l = [x + 0.5 for x in c], [x - 0.5 for x in c]
        v = [1000] * (n - 1) + [2500]
        nums = {}
        tags = scan.indicator_setups(h, l, c, v, nums)
        self.assertIn("ema_cross", tags)
        self.assertIn("supertrend_flip", tags)
        self.assertIn("volume_spike", tags)
        self.assertAlmostEqual(nums["volume_x"], 2.5)
        self.assertNotIn("bb_squeeze", tags)                            # the width has been rising for 3 bars
        self.assertNotIn("bb_release", tags)
        squeeze = scan.indicator_setups([x + 0.5 for x in base], [x - 0.5 for x in base], base, [1000] * n)
        self.assertIn("bb_squeeze", squeeze)                            # a constant width is at its 20-bar low
        self.assertNotIn("bb_release", squeeze)
        self.assertNotIn("volume_spike", squeeze)
        pop = base[:-1] + [105.0]                                       # yesterday at the low, today not: released
        release = scan.indicator_setups([x + 0.5 for x in pop], [x - 0.5 for x in pop], pop, [1000] * n)
        self.assertIn("bb_release", release)
        self.assertNotIn("bb_squeeze", release)
        tags, nums = scan.find_setups(c)                                # closes only: the price setups alone
        self.assertEqual(tags, ["breakout_20d"])
        self.assertIsNone(nums["score"])

    def test_cli_shows_the_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run(scan, ["daily", "--offline", DAILY, "--now", "2026-11-05 08:30",
                                        "--out", os.path.join(tmp, "wl.json"), "--show", "3"])
        self.assertEqual(code, 0, err)
        self.assertIn("108 charts matched a setup: 57 20-day closing-high breakout, 2 bull flag, 22 reclaimed the "
                      "20-day average, 1 oversold (RSI under 30), 2 oversold bounce, 34 EMA 10 crossed over EMA 20, "
                      "22 Supertrend turned up, 18 volume over 2x its 20-day average, 15 Bollinger squeeze (width at "
                      "a 20-bar low), 7 Bollinger squeeze released.", out)
        self.assertIn("RELIANCE       1,311.00  RSI  67.1  score 5/6  20-day closing-high breakout, EMA 10 crossed over EMA 20", out)
        self.assertIn("a state, not a signal", out)


class OrbTrend(unittest.TestCase):
    def test_flag_from_the_day_before(self):
        daily = scan.load_offline(DAILY)[0]
        odata = scan.load_offline(ORB)[0]
        day, cutoff = datetime.date(2026, 11, 3), datetime.datetime(2026, 11, 3, 10, 0)
        hits, _ = scan.scan_orb(odata, sorted(odata), day, cutoff, 15, 50, 0)
        scan.trend_flags(daily, hits, day)
        self.assertEqual(Counter(h["trend"] for h in hits), {"below_ema50": 32, "above_ema50": 16})
        tc = next(h for h in hits if h["symbol"] == "TATACONSUM")
        self.assertEqual((tc["trend"], tc["ema50"]), ("above_ema50", 1008.31))
        # only bars dated before the scan day count: the Nov 3 and Nov 4 closes never leak in
        closes = [b["c"] for b in daily["TATACONSUM"] if td.ist_of(b["ts"]).date() < day]
        self.assertAlmostEqual(td.ema(closes, 50)[-1], 1008.31, places=2)
        scan.trend_flags({}, hits, day)
        self.assertTrue(all(h["trend"] is None and h["ema50"] is None for h in hits))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run(scan, ["orb", "--offline", ORB, "--daily", DAILY, "--now", "2026-11-03 10:00",
                                        "--out", os.path.join(tmp, "orb.json"), "--show", "2"])
            self.assertEqual(code, 0, err)
            self.assertIn("09:35  ADANIENSOL     1,395.20  range 1,366.80 to 1,389.90  +0.38% over  daily below EMA50", out)
            self.assertIn("Daily trend: 16 of 48 hits closed above their 50-day EMA the day before (a flag, not a filter).", out)
            with open(os.path.join(tmp, "orb.json")) as fh:
                hit = next(h for h in json.load(fh)["hits"] if h["symbol"] == "TATACONSUM")
            self.assertEqual(hit["trend"], "above_ema50")
            code, out, _ = run(scan, ["orb", "--offline", ORB, "--now", "2026-11-03 10:00",
                                      "--out", os.path.join(tmp, "orb2.json"), "--show", "1"])
            self.assertNotIn("daily", out.split("48 of 171")[1].split("\n")[1])   # no --daily: no flag


class SupertrendStop(unittest.TestCase):
    def test_on_the_5_minute_bars(self):
        self.assertEqual(exitp.parse_stop("supertrend"), ("supertrend", None))
        ch = exitp.load_chart("TATACONSUM", "5m", None, BARS5)
        bars = exitp.session_bars(ch, datetime.date(2026, 11, 3), "5m", upto_slot=8)
        st = exitp.supertrend_of(bars)
        self.assertEqual((st["direction"], st["bars"], st["period"], st["mult"]), (1, 9, 10, 3.0))
        self.assertEqual(st["line"], 1030.3833)
        sr, sp = exitp.parse_stop("supertrend")
        tr, tp = exitp.parse_target("1R")
        c = exitp.compute(1050.20, sr, sp, tr, tp, 1048.8, 1030.0, None, None, "0.1", st)
        self.assertEqual((float(c["stop"]), float(c["risk"]), float(c["target"])), (1030.3, 19.9, 1070.1))
        self.assertEqual(c["stop_label"], "Supertrend(10, 3) line")
        self.assertEqual(c["stop_detail"], "line Rs 1030.38 on the 9 bars to the entry (fewer than 10 so far), "
                                           "rounded down to the 0.1 tick")

    def test_refusals(self):
        sr, sp = exitp.parse_stop("supertrend")
        tr, tp = exitp.parse_target("1R")
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.compute(1050.20, sr, sp, tr, tp, 1048.8, 1030.0, None, None, "0.1", None)
        self.assertIn("needs the session's bars", str(cm.exception))
        down = {"line": 1060.0, "direction": -1, "bars": 12, "period": 10, "mult": 3.0}
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.compute(1050.20, sr, sp, tr, tp, 1048.8, 1030.0, None, None, "0.1", down)
        self.assertIn("downtrend at the entry: its line Rs 1,060.00 sits above the price", str(cm.exception))
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.parse_stop("vwap")
        self.assertIn("atr:K or supertrend", str(cm.exception))

    def test_cli_5m_and_1m(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plan.json")
            code, out, err = run(exitp, ["plan", "--symbol", "TATACONSUM", "--entry", "1050.20", "--or-high", "1048.80",
                                         "--or-low", "1030.00", "--date", "2026-11-03", "--time", "10:00",
                                         "--entry-bar", "8", "--interval", "5m", "--stop", "supertrend", "--target", "1R",
                                         "--tick", "0.10", "--offline", BARS5, "--write", path])
            self.assertEqual(code, 0, err)
            self.assertIn("Stop          Rs 1,030.30  Supertrend(10, 3) line: line Rs 1030.38 on the 9 bars to the entry "
                          "(fewer than 10 so far), rounded down to the 0.1 tick", out)
            self.assertIn("Risk a share  Rs 19.90 (1.89% of the entry", out)
            with open(path) as fh:
                plan = json.load(fh)
            self.assertEqual((plan["stop_rule"], plan["stop"], plan["supertrend"]["bars"]), ("supertrend", 1030.3, 9))
            code, out, err = run(exitp, ["plan", "--symbol", "TATACONSUM", "--entry", "1051.30", "--or-high", "1048.80",
                                         "--or-low", "1030.00", "--date", "2026-11-03", "--time", "09:31",
                                         "--entry-bar", "15", "--interval", "1m", "--stop", "supertrend", "--target", "1R",
                                         "--tick", "0.10", "--offline", BARS1])
            self.assertEqual(code, 0, err)
            self.assertIn("Stop          Rs 1,039.50  Supertrend(10, 3) line: line Rs 1039.59 on the 16 bars to the entry, "
                          "rounded down to the 0.1 tick", out)


class SupertrendHistory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        code, out, err = run(tm, ["run", "--symbol", "TATACONSUM", "--date", "2026-11-03", "--entry", "1050.20",
                                  "--stop", "1030.30", "--target", "1070.10", "--entry-time", "10:00",
                                  "--stop-rule", "supertrend", "--tick", "0.10", "--offline", POOL,
                                  "--now", "2026-11-03 10:00", "--json"])
        assert code == 0, err
        cls.doc = json.loads(out)

    def test_history_with_the_supertrend_rule(self):
        h = self.doc["history"]
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "supertrend", "target_r": 1.0,
                                     "tick": 0.1})
        self.assertEqual((h["sessions"], h["triggered"], h["target"], h["stop"], h["close"]), (68, 23, 8, 3, 12))
        self.assertEqual((h["green"], h["avg_R"], h["no_stop"]), (13, 0.19, 3))
        m = self.doc["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"], m["p_green"]), (0.078, 0.0771, 0.8449, 0.5099))
        self.assertEqual(m["avg_R"], 0.012)
        self.assertEqual(self.doc["verdict"], "Close to a coin flip: target first 7.8 % vs stop first 7.7 %.")

    def test_rule_stop_on_a_session(self):
        days = tm.split_sessions(tm.parse_bars(tm.bundle_bars(tm.read_text(POOL), "TATACONSUM"),
                                               tm.parse_now("2026-11-03 10:00"))[0])
        sessions, _ = tm.full_sessions(days, "2026-11-03")
        d, D = sessions[-1]
        self.assertEqual(d, "2026-11-02")
        rule = {"stop_rule": "supertrend", "tick": 0.1}
        s = tm.rule_stop(rule, 0, 0, D[8]["c"], True, D, 8)
        line, direction = td.supertrend([b["h"] for b in D[:9]], [b["l"] for b in D[:9]], [b["c"] for b in D[:9]], 10, 3.0)
        self.assertEqual(s, None if direction[-1] != 1 else tm.tick_away(line[-1], True, 0.1))
        self.assertIsNone(tm.rule_stop(rule, 0, 0, 100.0, True))        # no bars: no stop

    def test_text(self):
        code, out, err = run(tm, ["run", "--symbol", "TATACONSUM", "--date", "2026-11-03", "--entry", "1050.20",
                                  "--stop", "1030.30", "--target", "1070.10", "--entry-time", "10:00",
                                  "--stop-rule", "supertrend", "--tick", "0.10", "--offline", POOL,
                                  "--now", "2026-11-03 10:00"])
        self.assertEqual(code, 0, err)
        self.assertIn("History: the same rule (15-min opening range, stop at the Supertrend(10, 3) line at the entry, "
                      "1R target) on each of those 68 real sessions fired 23 times: 8 target, 3 stop, 12 neither. "
                      "Average R +0.190. 3 more breakouts had the Supertrend line above the entry: no stop, not taken.", out)
        self.assertNotIn(chr(0x2014), out)


if __name__ == "__main__":
    unittest.main()

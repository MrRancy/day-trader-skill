import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("trader-timemachine", "timemachine")
td = tm.trader_data

DIR = os.path.join(FIX, "trader-timemachine")
DATA = os.path.join(DIR, "TATACONSUM-100d5m.json.gz")   # INDstocks 5-minute bars, 100 days to Nov 4 2026, 70 sessions
with gzip.open(DATA, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
NOW = "2026-11-03 10:01"
NOW_IST = tm.parse_now(NOW)

# the plan as trader-exit writes it (TATACONSUM, Tue Nov 3 2026)
PLAN = {"created_by": "trader-exit", "symbol": "TATACONSUM", "side": "buy", "date": "2026-11-03", "setup": "orb",
        "entry": 1050.2, "entry_time": "10:00", "entry_bar": 8, "interval": "5m",
        "opening_range": {"high": 1048.8, "low": 1030.0, "minutes": 15},
        "stop": 1039.4, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
        "target": 1061.0, "target_r": 1.0, "target_label": "1R (one times the risk)",
        "risk_per_share": 10.8, "reward_risk": 1.0, "flat_by": "15:15", "currency": "INR", "tick": 0.1}

# fingerprints of the first run (seed 19): the numbers in SKILL.md, bit for bit
SIM_BANDS = "2be028288c0af6ba"
SIM_PATHS = "47e4d32e9ba46d07"
SIM_TRADES = "0c97bb37158032af"
SIM_HIST = {"lo": 1025.93, "hi": 1077.71,
            "bins": [112, 133, 160, 237, 295, 405, 507, 592, 706, 729, 768, 761, 795, 722, 649, 551, 471, 358,
                     289, 205, 157, 111, 79, 54]}


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_IST, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn):
    """The fixture bundle, changed by fn(bars), back as text."""
    doc = json.loads(TEXT)
    fn(doc["bars"]["TATACONSUM"])
    return json.dumps(doc)


def keep_bars(bars, keep):
    bars[:] = [b for b in bars if keep(b)]


def day_of(b):
    return td.ist_of(b["ts"]).date().isoformat()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class TataConsum(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "15:30"))
        self.assertEqual(m["pool_sessions"], 68)
        self.assertEqual(m["bars_left"], 66)
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-07-27", "2026-11-02"))
        self.assertEqual(m["p_target"], 0.2982)
        self.assertEqual(m["p_stop"], 0.3588)
        self.assertEqual(m["p_neither"], 0.343)
        self.assertEqual(m["p_green"], 0.479)
        self.assertEqual(m["median_close"], 1050.53)
        self.assertEqual(m["p5_close"], 1032.85)
        self.assertEqual(m["p95_close"], 1069.09)
        self.assertEqual(m["avg_R"], -0.045)
        self.assertEqual(m["r_at_target"], 1.0)

    def test_bit_for_bit(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), SIM_BANDS)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), SIM_PATHS)
        self.assertEqual(m["hist"], SIM_HIST)
        self.assertEqual(m["bands"][-1], [1033.96, 1043.5, 1050.57, 1058.39, 1069.85])

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (68, 26))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (15, 7, 4, 17))
        self.assertEqual(h["avg_R"], 0.306)
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0, "tick": 0.1})
        slim = [{"date": t["date"], "entry": t["entry"], "outcome": t["outcome"], "R": t["R"]} for t in h["trades"]]
        self.assertEqual(sha(slim), SIM_TRADES)
        first = h["trades"][0]
        # Jul 28: range 1100.60 to 1110.80 ... the 09:40 bar closed 1110.80 above it; midpoint stop on the 0.10 tick
        self.assertEqual((first["date"], first["time"], first["entry"], first["stop"], first["target"], first["outcome"]),
                         ("2026-07-28", "09:45", 1110.8, 1105.7, 1115.9, "target"))
        self.assertEqual(h["trades"][-1]["date"], "2026-11-02")
        for t in h["trades"]:
            self.assertEqual(round(t["stop"] * 10) / 10, t["stop"], t)   # every stop on the tick

    def test_the_verdict_says_coin_flip(self):
        self.assertEqual(self.doc["verdict"], "Close to a coin flip: target first 29.8 % vs stop first 35.9 %.")
        # 1R breaks even at 50 % of the replays that hit one side; the target got 45.4 %
        gap, share, even = tm.lean(self.doc["mc"])
        self.assertEqual((round(share, 3), even), (0.454, 0.5))
        self.assertEqual(self.doc["verdict_notes"], [])

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1050.2] * 5)
        self.assertEqual(len(m["bands"]), 67)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("10:00", "15:30", 67))
        self.assertTrue(all(p[0] == 1050.2 and len(p) == 67 for p in m["sample_paths"]))

    def test_the_plan_is_carried_through(self):
        p = self.doc["plan"]
        self.assertEqual((p["symbol"], p["entry"], p["stop"], p["target"], p["entry_bar"], p["entry_time"]),
                         ("TATACONSUM", 1050.2, 1039.4, 1061.0, 8, "10:00"))
        self.assertEqual((p["risk_per_share"], p["target_r"], p["flat_by"], p["tick"]), (10.8, 1.0, "15:15", 0.1))
        self.assertEqual(self.doc["warnings"], [])
        self.assertEqual(self.doc["data"]["last_bar"], "2026-11-03 09:55 IST")
        self.assertEqual(self.doc["data"]["dropped"], {"no_price": 0, "off_grid": 0, "not_complete": 141,
                                                       "outside_hours": 0})   # the rest of Nov 3, all of Nov 4

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("TATACONSUM buy, Tuesday 2026-11-03: entry 1050.20 at 10:00 (bar 8), stop 1039.40, target 1061.00",
                     "10,000 replays", "66 five-minute bars", "seed 19", "68 full sessions",
                     "target first   29.8 %", "stop first     35.9 %", "neither        34.3 %   still open at 15:30",
                     "green          47.9 %", "median 1050.53, 90 % of replays between 1032.85 and 1069.09",
                     "Average R: -0.045", "fired 26 times: 15 target, 7 stop, 4 neither. Average R +0.306",
                     "Close to a coin flip: target first 29.8 % vs stop first 35.9 %.", "not a prediction", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick", "Yahoo", " ET"):
            self.assertNotIn(word, text)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = machine(replays=2000, seed=5)["mc"]
        b = machine(replays=2000, seed=5)["mc"]
        self.assertEqual(a, b)

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertNotEqual((m["p_target"], m["p_stop"], m["median_close"]), (0.2982, 0.3588, 1050.53))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), SIM_BANDS)

    def test_the_draws_are_stable_across_pythons(self):
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(68)), 37)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def setUp(self):
        self.raw = tm.bundle_bars(TEXT, "TATACONSUM")

    def test_the_in_progress_bars_are_dropped(self):
        bars, dropped = tm.parse_bars(self.raw, NOW_IST)
        self.assertEqual(dropped, {"no_price": 0, "off_grid": 0, "not_complete": 141, "outside_hours": 0})
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-11-03", "09:55", 8))
        entry = [b for b in bars if b["date"] == "2026-11-03" and b["slot"] == 8][0]
        self.assertEqual((entry["start"], entry["c"]), ("09:55", 1050.2))
        # with the real clock everything is complete: 70 sessions of 75 bars
        bars, dropped = tm.parse_bars(self.raw, tm.parse_now(None))
        self.assertEqual((len(bars), dropped["not_complete"]), (5250, 0))

    def test_now_pins_the_clock(self):
        bars = tm.parse_bars(self.raw, tm.parse_now("2026-11-03 09:57"))[0]
        today = [b for b in bars if b["date"] == "2026-11-03"]
        self.assertEqual(len(today), 8)
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-11-03 09:57"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("09:50 bar", str(cm.exception))
        self.assertEqual(machine(now=tm.parse_now("2026-11-03 10:00"), replays=200)["mc"]["pool_sessions"], 68)

    def test_the_grid_and_the_hours(self):
        t0 = td.ts_of(tm.datetime.datetime(2026, 10, 1, 9, 15))
        raw = [{"ts": t0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0},                 # 09:15
               {"ts": t0 + 60, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0},            # 09:16, off the 5-minute grid
               {"ts": t0 - 300, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0},           # 09:10, pre-open
               {"ts": t0 + 75 * 300, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0},      # 15:30, the closing print
               {"ts": t0 + 300, "o": None, "h": 1, "l": 1, "c": 1, "v": 0},        # 09:20 with no open
               {"ts": t0 + 600, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}]           # 09:25
        bars, dropped = tm.parse_bars(raw, tm.parse_now("2026-10-02T00:00"))
        self.assertEqual([(b["start"], b["slot"]) for b in bars], [("09:15", 0), ("09:25", 2)])
        self.assertEqual(dropped, {"no_price": 1, "off_grid": 1, "not_complete": 0, "outside_hours": 2})

    def test_a_short_session_is_left_out(self):
        def cut(bars):  # Jul 27 ends at 13:00, like a session cut short: 45 bars
            keep_bars(bars, lambda b: not (day_of(b) == "2026-07-27" and td.ist_of(b["ts"]).hour >= 13))
        doc = machine(text=edited(cut), replays=300)
        self.assertEqual(doc["mc"]["pool_sessions"], 67)
        self.assertEqual(doc["mc"]["pool_from"], "2026-07-28")
        self.assertEqual(doc["data"]["short_sessions_left_out"], [{"date": "2026-07-27", "bars": 45}])
        self.assertEqual(doc["history"]["sessions"], 67)
        self.assertNotIn("2026-07-27", [t["date"] for t in doc["history"]["trades"]])
        self.assertIn("Left out 1 short session", "\n".join(tm.report(doc)))

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-11-02")
        self.assertNotIn("2026-11-03", [t["date"] for t in doc["history"]["trades"]])

    def test_a_missing_price_is_dropped(self):
        def hole(bars):
            bars[5]["c"] = None
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["data"]["dropped"]["no_price"], 1)
        self.assertEqual(doc["mc"]["pool_sessions"], 67)   # 2026-07-27 now has a gap

    def test_empty_and_broken_data(self):
        empty = json.dumps({"interval": "5minute", "bars": {"TATACONSUM": []}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:
            machine(text=json.dumps({"bars": {"RELIANCE": []}}))
        self.assertIn("data is for RELIANCE but the plan is for TATACONSUM", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:
            machine(text=json.dumps({"interval": "1minute", "bars": {"TATACONSUM": []}}))
        self.assertIn("needs 5-minute bars", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")
        with self.assertRaises(tm.TMError):
            machine(text=json.dumps({"spark": {}}))

    def test_fewer_than_20_sessions_is_refused(self):
        dates = sorted(set(day_of(b) for b in self.raw if day_of(b) < "2026-11-03"))
        self.assertEqual(len(dates), 68)
        nineteen, twenty = dates[-19], dates[-20]

        def trim(bars):
            keep_bars(bars, lambda b: day_of(b) >= nineteen)
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(trim))
        self.assertIn("only 19 full session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))

        def keep20(bars):
            keep_bars(bars, lambda b: day_of(b) >= twenty)
        self.assertEqual(machine(text=edited(keep20), replays=200)["mc"]["pool_sessions"], 20)


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-11-04"))        # tomorrow's bars are in the file but not complete at --now
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("no bars for that day", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:      # 10:05 has not happened at 10:01
            machine(plan(entry_time="10:05", entry_bar=None))
        self.assertIn("the data stops at the 09:55 bar", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="10:02", entry_bar=None), "5-minute grid"),
            (dict(entry_time="15:30", entry_bar=None), "between 09:20 and 15:25"),
            (dict(entry_bar=9), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "single trades"),
            (dict(stop=1055.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "NSE symbol"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="Nov 3"), "date like"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
            (dict(tick=0), "tick must be above 0"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)
        self.assertEqual(plan(symbol="m&m")["symbol"], "M&M")
        self.assertEqual(plan(symbol="bajaj-auto")["symbol"], "BAJAJ-AUTO")

    def test_entry_bar_or_entry_time_alone_and_the_tick(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "10:00")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 8)
        self.assertEqual(plan(entry_time="10:00 AM", entry_bar=None)["entry_bar"], 8)
        self.assertEqual(plan(tick=None)["tick"], 0.1)          # from the price band when the plan has none
        self.assertEqual(plan(tick=None, entry=500.0, stop=495.0, target=505.0)["tick"], 0.05)

    def test_until_cuts_the_replay(self):
        doc = machine(until="15:15", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("15:15", 63, "15:15"))
        self.assertIn("still open at 15:15", "\n".join(tm.report(doc)))
        self.assertEqual(machine(until="3:15 PM", replays=500)["mc"], m)
        self.assertEqual(machine(until="3:15", replays=500)["mc"], m)
        for bad, words in (("10:00", "nothing to replay"), ("15:35", "no later than 15:30"),
                           ("15:17", "5-minute grid"), ("noon", "IST time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_the_flat_by_run(self):
        # --until 15:15, the plan's flat-by: the lean to the stop grows, 43 % still open
        m = machine(until="15:15")["mc"]
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"], m["p_green"]), (0.2363, 0.3325, 0.4312, 0.4406))
        self.assertEqual((m["median_close"], m["p5_close"], m["p95_close"], m["avg_R"]), (1048.86, 1032.74, 1065.51, -0.106))
        self.assertEqual(tm.verdict(m), "Leans toward the stop: target first 23.6 % vs stop first 33.2 %.")
        self.assertEqual(tm.verdict_notes(m), ["43.1 % of the replays hit neither by 15:15; the price at 15:15 decides those."])

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=1060.0, stop=1050.0, target=1070.0), replays=200)
        self.assertIn("outside that bar's range", doc["warnings"][0])

    def test_stop_rounds_away_from_the_entry_on_the_tick(self):
        self.assertEqual(tm.tick_away(1039.45, True, 0.1), 1039.4)
        self.assertEqual(tm.tick_away(1039.45, False, 0.1), 1039.5)
        self.assertEqual(tm.tick_away(1039.4000000001, True, 0.1), 1039.4)
        self.assertEqual(tm.tick_away(1039.39999999999, True, 0.1), 1039.4)
        self.assertEqual(tm.tick_away(358.325, True, 0.05), 358.3)
        self.assertEqual(tm.tick_away(358.325, False, 0.05), 358.35)
        self.assertEqual(tm.tick_away(358.325, True, 0.01), 358.32)
        self.assertEqual(tm.tick_away(30001.0, True, 5.0), 30000.0)

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        rule = machine(plan(stop_rule=None), replays=100)["history"]["rule"]
        self.assertEqual(rule["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=1030.0, target=1070.4), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=1041.3, target=1068.0), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.8475, 2.0))

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="flag"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("orb", doc["history"]["note"])
        self.assertEqual(doc["mc"]["pool_sessions"], 68)
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=1061.0, target=1039.4, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(m["r_at_target"], 1.0)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)
        self.assertIn("below the entry", "\n".join(tm.report(doc)))

    def test_the_verdict_thresholds_at_1r(self):
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.10, "p_stop": 0.60, "r_at_target": 1.0}),
                         "Strongly toward the stop: target first 10.0 % vs stop first 60.0 %.")
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:15"}),
                         "No replay reached the target or the stop by 15:15: the price at 15:15 decides every one.")

    def test_a_2r_target_is_judged_against_its_break_even(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        self.assertEqual(tm.verdict_notes(mc), [])
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertTrue(tm.verdict(lean).startswith("Leans toward the stop: at 2R the target has to come first in "
                                                    "33 % of the replays that hit one side; it did in 21 %"))
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])
        self.assertIn("at 1.5R the target has to come first in 40 %", tm.verdict(dict(mc, r_at_target=1.5)))

    def test_2r_on_the_fixture_a_clear_lean(self):
        # the same entry and stop with a 2R target 1,071.80: the target came first in only 5.6 % of replays
        doc = machine(plan(target=1071.8))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]),
                         (2.0, 0.056, 0.3618, 0.5822, -0.019))
        self.assertEqual(doc["verdict"], "Strongly toward the stop: at 2R the target has to come first in 33 % of "
                                         "the replays that hit one side; it did in 13 % (target first 5.6 % vs stop "
                                         "first 36.2 %).")
        self.assertEqual(doc["verdict_notes"],
                         ["58.2 % of the replays hit neither by 15:30; the price at 15:30 decides those."])
        h = doc["history"]
        self.assertEqual((h["triggered"], h["target"], h["stop"], h["close"], h["avg_R"]), (26, 9, 11, 6, 0.311))


class Network(unittest.TestCase):
    """INDstocks is never called: a fake backend answers from the fixture."""

    class Fake(object):
        def __init__(self, bars, fail=None):
            self.bars, self.fail, self.calls = bars, fail, []

        def candles(self, sym, interval, start, end):
            self.calls.append((sym, interval, str(start), str(end)))
            if self.fail:
                raise td.DataError(self.fail)
            return self.bars

    def setUp(self):
        self.bars = tm.bundle_bars(TEXT, "TATACONSUM")

    def test_the_request_is_100_days_of_5_minute_bars(self):
        fake = self.Fake(self.bars)
        doc = tm.fetch_bars("TATACONSUM", NOW_IST, backend=fake)
        self.assertEqual(fake.calls, [("TATACONSUM", "5m", "2026-07-26", "2026-11-04")])
        self.assertEqual(doc["interval"], "5minute")
        self.assertEqual(len(doc["bars"]["TATACONSUM"]), 5250)
        self.assertEqual(machine(text=doc, replays=200)["mc"]["pool_sessions"], 68)

    def test_a_data_error_is_one_plain_line(self):
        fake = self.Fake(None, fail="INDstocks answered HTTP 429 for /market/historical/5minute: Too Many Requests")
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_bars("TATACONSUM", NOW_IST, backend=fake)
        self.assertIn("429", str(cm.exception))

    def test_run_over_the_network_path(self):
        fake = self.Fake(self.bars)
        td._default["indstocks"] = fake
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(PLAN, fh)
            code, text, err = run(["run", "--plan", p, "--now", NOW, "--replays", "300", "--json"])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(text)["source"], "indstocks")
            self.assertEqual(len(fake.calls), 1)
        finally:
            td._default.pop("indstocks", None)
            shutil.rmtree(d)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "trader", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "trader", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", DATA, "--now", NOW, "--write", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("target first   29.8 %", text)
        self.assertIn("Close to a coin flip: target first 29.8 % vs stop first 35.9 %.", text)
        self.assertIn("Wrote %s." % self.out, text)
        self.assertIn("/trader-budget", text)
        with open(self.out) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(k for k in ("plan", "mc", "history") if k in doc), ["history", "mc", "plan"])
        self.assertEqual(doc["created_by"], "trader-timemachine")
        self.assertEqual(doc["source"], "offline")
        self.assertEqual(doc["created_at"], "2026-11-03T10:01:00+05:30")
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["avg_R"]), (0.2982, -0.045))
        self.assertEqual(sha(doc["mc"]["sample_paths"]), SIM_PATHS)
        self.assertEqual((doc["history"]["triggered"], doc["history"]["avg_R"]), (26, 0.306))
        self.assertEqual(doc["plan"]["from"], self.plan)

    def test_json_output(self):
        code, text, _ = run(["run", "--plan", self.plan, "--offline", DATA, "--now", NOW, "--json"])
        self.assertEqual(code, 0)
        doc = json.loads(text)
        self.assertEqual(doc["mc"]["p_stop"], 0.3588)
        self.assertNotIn("sample_paths", doc["mc"])
        self.assertEqual(doc["verdict"][:20], "Close to a coin flip")
        self.assertFalse(os.path.exists(self.out))

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "tataconsum", "--date", "2026-11-03", "--entry", "1050.20",
                               "--stop", "1039.40", "--target", "1061.00", "--entry-time", "10:00",
                               "--offline", DATA, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["p_green"]), (0.2982, 0.479))
        self.assertEqual((doc["plan"]["stop_rule"], doc["plan"]["tick"]), ("or_mid", 0.1))   # the tick from the price band
        self.assertEqual((doc["history"]["target"], doc["history"]["stop"], doc["history"]["avg_R"]), (15, 7, 0.306))
        code, _, err = run(["run", "--symbol", "TATACONSUM", "--entry", "1050.20", "--offline", DATA])
        self.assertEqual(code, 1)
        self.assertIn("--stop", err)

    def test_offline_folder_and_default_paths(self):
        shutil.copy(DATA, self.d)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            self.assertTrue(os.path.exists(os.path.join("trader", "timemachine.json")))
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            self.assertIn("* median", text)
            with open(os.path.join("trader", "timemachine.json")) as fh:
                self.assertEqual(json.load(fh)["mc"]["replays"], 500)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run", "--plan", self.plan, "--offline", DATA, "--until", "9:00"],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)
            self.assertNotIn("Traceback", err)
        self.assertEqual(run([])[0], 1)

    def test_chart(self):
        run(["run", "--plan", self.plan, "--offline", DATA, "--now", NOW, "--write", self.out])
        code, text, _ = run(["chart", "--from", self.out])
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertIn("2,000 replay paths from the 10:00 entry (seed 19)", lines[0])
        self.assertTrue(any(l.startswith("  1061.00 |") and l.endswith("  target") for l in lines))
        self.assertTrue(any(l.startswith("  1039.40 |") and l.endswith("  stop") for l in lines))
        self.assertTrue(any(l.startswith("  1050.20 |*") and l.endswith("  entry") for l in lines))
        self.assertIn("10:00", text)
        self.assertIn("15:30", text)
        self.assertEqual(lines[-1], "Close to a coin flip: target first 29.8 % vs stop first 35.9 %.")
        code, again, _ = run(["chart", "--plan", self.plan, "--offline", DATA, "--now", NOW])
        self.assertEqual(again, text)


if __name__ == "__main__":
    unittest.main()

"""trader-exit tests. Bars for the ATR stop reuse tests/fixtures/trader-entry/
(TATACONSUM's real Nov 3 2026 five-minute bars, see test_entry.py)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT

exitp = load("trader-exit", "exit")
entry = load("trader-entry", "entry")

BARS = os.path.join(FIX, "trader-entry", "TATACONSUM-2026-11-03-5m.json")
MASTER = os.path.join(FIX, "trader_data", "instruments-equity-2026-11-04.csv.gz")

# trader/plan.json for TATACONSUM, Nov 3 2026, exactly as the contract writes it
PLAN = {"created_by": "trader-exit", "symbol": "TATACONSUM", "side": "buy", "date": "2026-11-03", "setup": "orb",
        "entry": 1050.2, "entry_time": "10:00", "entry_bar": 8, "interval": "5m",
        "opening_range": {"high": 1048.8, "low": 1030.0, "minutes": 15},
        "stop": 1039.4, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
        "target": 1061.0, "target_r": 1.0, "target_label": "1R (one times the risk)",
        "risk_per_share": 10.8, "reward_risk": 1.0, "flat_by": "15:15", "currency": "INR", "tick": 0.1}
ENTRY = {"created_by": "trader-entry", "symbol": "TATACONSUM", "setup": "orb", "side": "buy", "date": "2026-11-03",
         "status": "triggered", "time": "10:00", "bar_start": "09:55", "bar_index": 8, "price": 1050.2,
         "level": 1048.8, "interval": "5m", "currency": "INR", "tick": 0.1,
         "opening_range": {"high": 1048.8, "low": 1030.0, "minutes": 15},
         "source": "indstocks", "checked_at": "2026-11-03T10:00:00+05:30"}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=1048.8, ol=1030.0, atr=None, n=None, tick="0.1"):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n, tick)


class Math(unittest.TestCase):
    def test_tataconsum_or_mid_1r(self):
        p = c(1050.20, "or-mid", "1R")
        self.assertEqual(p["stop"], Decimal("1039.40"))          # (1048.80 + 1030.00) / 2, on the tick
        self.assertEqual(p["risk"], Decimal("10.80"))
        self.assertEqual(p["target"], Decimal("1061.00"))
        self.assertEqual(p["reward_risk"], Decimal("1.00"))
        self.assertEqual(p["target_label"], "1R (one times the risk)")
        self.assertNotIn("rounded", p["stop_detail"])

    def test_the_default_2r(self):
        p = c(1050.20, "or-mid")
        self.assertEqual(p["target"], Decimal("1071.80"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")

    def test_the_tick_rounds_the_stop_down_and_the_target_half_up(self):
        # a midpoint of 1039.35 is not a 0.10 tick: the stop goes down to 1039.30
        p = c(1050.20, "or-mid", "1R", 1048.7, 1030.0)
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1039.30"), Decimal("10.90"), Decimal("1061.10")))
        self.assertIn("rounded down to the 0.1 tick", p["stop_detail"])
        # the same numbers on a 0.05 tick stock
        p = c(1050.20, "or-mid", "1R", 1048.7, 1030.0, tick="0.05")
        self.assertEqual((p["stop"], p["target"]), (Decimal("1039.35"), Decimal("1061.05")))
        # a 1.5R target that lands between ticks rounds half up: 1050.20 + 16.20 = 1066.40 exact; try 1.33R
        p = c(1050.20, "or-mid", "1.33R")
        self.assertEqual(p["target"], Decimal("1064.60"))          # 1050.20 + 14.364 -> 1064.564 -> 1064.60
        p = c(100, "price:98.97", "1R", None, None, tick="0.05")
        self.assertEqual((p["stop"], p["target"]), (Decimal("98.95"), Decimal("101.05")))   # stop down, target up

    def test_raw_floats_give_the_same_paise(self):
        p = c(1050.2000000001, "or-mid", "1R", 1048.79999999, 1030.0000001)
        self.assertEqual((p["entry"], p["stop"], p["target"]), (Decimal("1050.20"), Decimal("1039.40"), Decimal("1061.00")))

    def test_other_stops(self):
        p = c(1050.20, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1030.00"), Decimal("20.20"), Decimal("1090.60")))
        p = c(1050.20, "price:1039.47")
        self.assertEqual(p["stop"], Decimal("1039.40"))             # rounds away from the entry, onto the tick
        p = c(1050.20, "pct:1")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1039.60"), Decimal("10.60"), Decimal("1071.40")))
        p = c(1050.20, "atr:1.5", atr=5.8889, n=9)
        self.assertEqual(p["stop"], Decimal("1041.30"))             # 1050.20 - 8.83335 = 1041.36665 -> 1041.30
        self.assertEqual(p["stop_label"], "1.5 x ATR(9) under the entry")
        self.assertEqual(p["target"], Decimal("1068.00"))

    def test_price_targets(self):
        p = c(1050.20, "or-mid", "price:1064")
        self.assertEqual(p["reward_risk"], Decimal("1.28"))
        self.assertEqual(p["target_label"], "your price (1.28R)")
        self.assertEqual(p["target_r"], Decimal("1.28"))
        self.assertEqual(exitp.parse_target("price:Rs 1,064")[1], Decimal("1064"))

    def test_refusals_say_why(self):
        cases = [("price:1050.20", "2R", "at or above the entry"),
                 ("price:1051", "2R", "at or above the entry"),
                 ("or-mid", "price:1050", "at or below the entry"),
                 ("price:1050.10", "2R", "too tight to survive the spread"),
                 ("pct:0.02", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(1050.20, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # 0.05% of 1050.20 is 0.525: a Rs 0.60 risk is allowed, Rs 0.50 is not
        self.assertEqual(c(1050.20, "price:1049.60")["risk"], Decimal("0.60"))
        with self.assertRaises(exitp.PlanError):
            c(1050.20, "price:1049.70")
        # on a Rs 5 tick at Rs 6,002 the floor is one tick (Rs 5), not 0.05 % (Rs 3): a stop of 6,001
        # rounds down to 6,000, two rupees under the entry, and is refused
        with self.assertRaises(exitp.PlanError) as cm:
            c(6002, "price:6001", "2R", None, None, tick="5")
        self.assertIn("less than Rs 5.00", str(cm.exception))
        self.assertEqual(c(6002, "price:5995", "2R", None, None, tick="5")["risk"], Decimal("7"))
        # at Rs 30,000 it is 0.05 % again: Rs 15
        with self.assertRaises(exitp.PlanError) as cm:
            c(30000, "price:29990", "2R", None, None, tick="5")
        self.assertIn("less than Rs 15.00", str(cm.exception))
        with self.assertRaises(exitp.PlanError):
            c(1035.00, "or-mid")                                    # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(1050.20, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1050.20, "or-mid", oh=1030.0, ol=1048.8)
        with self.assertRaises(exitp.PlanError):
            c(1050.20, "or-mid", tick="0")

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("TATACONSUM", "5m", None, BARS)
        day = exitp.datetime.date(2026, 11, 3)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=8)
        self.assertEqual(len(bars), 9)                              # 09:15 ... the 09:55 entry bar
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 1048.0 - 1030.0, places=4)
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 9)
        self.assertAlmostEqual(atr, sum(trs) / 9, places=9)
        self.assertAlmostEqual(atr, 5.8889, places=4)
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertEqual(len(all_bars), 75)
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.load_chart("RELIANCE", "5m", None, BARS)
        self.assertIn("holds TATACONSUM, not RELIANCE", str(cm.exception))

    def test_ticks(self):
        td = exitp.trader_data
        rows = td.parse_instruments(td.read_text(MASTER))
        self.assertEqual(td.tick_size("TATACONSUM", 1050.2, rows), (0.1, "master"))
        self.assertEqual(td.tick_size("HDFCBANK", None, rows), (0.05, "master"))
        self.assertEqual(td.tick_size("BOSCHLTD", None, rows), (5.0, "master"))
        self.assertEqual(td.tick_size("IDEA", None, rows), (0.01, "master"))
        self.assertEqual(td.tick_size("NOSUCH", 1050.2, rows), (0.1, "price band"))
        for price, tick in ((12, 0.01), (249.99, 0.01), (250, 0.05), (999, 0.05), (1000, 0.1), (4999, 0.1),
                            (5000, 0.5), (9999, 0.5), (10000, 1.0), (19999, 1.0), (20000, 5.0), (117690, 5.0)):
            self.assertEqual(td.tick_size(price=price), (tick, "price band"), price)
        self.assertEqual(td.tick_size(), (0.05, "default"))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-11-03 10:01"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json")) as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, err = run(entry, ["check", "TATACONSUM", "--offline", BARS, "--now", "2026-11-03 10:00",
                                   "--master", MASTER, "--write"])
        self.assertEqual(code, 0, err)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["tick_source"], "trader/entry.json")
        self.assertIn("Entry         Rs 1,050.20 at 10:00 (bar 8)", text)
        self.assertIn("Stop          Rs 1,039.40  opening-range midpoint", text)
        self.assertIn("Target        Rs 1,061.00  1R (one times the risk)", text)
        self.assertIn("Risk a share  Rs 10.80 (1.03% of the entry", text)
        self.assertIn("Reward:risk   1.0", text)
        self.assertIn("Flat by       15:15 IST", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/trader-timemachine", text)
        self.assertNotIn("pro" + "fit", text.lower())   # never on screen
        self.assertNotIn("Note:", text)
        code, text, _ = self.plan("--write")                       # the defaults: or-mid, 2R
        self.assertEqual(self.read_plan()["target"], 1071.80)
        self.assertEqual(self.read_plan()["target_r"], 2.0)
        code, text, _ = run(exitp, ["show", "--now", "2026-11-03 10:20"])
        self.assertEqual(code, 0)
        self.assertIn("Rs 1,071.80", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-11-04 10:20"])
        self.assertIn("not today", text)

    def test_manual_flags_and_json(self):
        code, text, _ = self.plan("--symbol", "tataconsum", "--entry", "1050.20", "--or-high", "1048.80",
                                  "--or-low", "1030.00", "--time", "10:00", "--entry-bar", "8", "--date", "2026-11-03",
                                  "--target", "1R", "--tick", "0.1", "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["warnings"], [])
        self.assertEqual(p["tick_source"], "--tick")
        # the tick from the saved master, and from the price band with a note
        code, text, _ = self.plan("--symbol", "TATACONSUM", "--entry", "1050.20", "--or-high", "1048.80",
                                  "--or-low", "1030.00", "--master", MASTER, "--json")
        self.assertEqual(json.loads(text)["tick_source"], "master")
        code, text, _ = self.plan("--symbol", "TATACONSUM", "--entry", "1050.20", "--or-high", "1048.80",
                                  "--or-low", "1030.00", "--offline", BARS, "--json")
        p = json.loads(text)
        self.assertEqual((p["tick"], p["tick_source"]), (0.1, "price band"))
        self.assertIn("price band", p["warnings"][0])

    def test_atr_stop_reads_bars(self):
        self.write(os.path.join("trader", "entry.json"), ENTRY)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", BARS, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (1041.3, 8.9, 1068.0))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 9))
        self.assertAlmostEqual(p["atr"], 5.8889, places=4)
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", BARS, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("TATACONSUM is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "1048.90", "--json")    # plan ahead at the level
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (1048.9, 1039.4, None))

        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, setup="level", level=1048.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("v2 plans longs only", err)
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "TATACONSUM")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "TATACONSUM", "--from", "x.json", "--entry", "1")
        self.assertIn("not both", err)
        code, _, err = self.plan("--symbol", "TATACONSUM", "--entry", "1050.20", "--or-high", "1048.80")
        self.assertIn("both --or-high and --or-low", err)
        code, _, err = self.plan("--symbol", "TATACONSUM", "--entry", "1050.20", "--or-high", "1048.80",
                                 "--or-low", "1030", "--tick", "0")
        self.assertIn("--tick must be above 0", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_config_square_off_and_late_entries(self):
        self.write(os.path.join("trader", "entry.json"), ENTRY)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:00", "min_reward_risk": 1.5})
        code, text, _ = self.plan("--target", "1R", "--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "15:00")
        self.assertIn("under your min_reward_risk 1.5", p["warnings"][0])
        os.remove(os.path.join("trader", "config.json"))

        code, text, _ = self.plan("--flat-by", "15:25")
        self.assertEqual(code, 0)
        self.assertIn("Flat by       15:25 IST", text)
        self.assertIn("squaring off MIS", text)
        code, _, err = self.plan("--flat-by", "15:30")
        self.assertEqual(code, 1)
        self.assertIn("at or after the 15:30 close", err)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:45"})
        code, _, err = self.plan()
        self.assertIn("flat_by in the config 15:45 is at or after the 15:30 close", err)
        os.remove(os.path.join("trader", "config.json"))
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, time="15:20"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, time="3:10 PM"))
        code, text, _ = self.plan("--json")
        self.assertEqual(json.loads(text)["entry_time"], "15:10")

    def test_show_without_a_plan(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "day trading loses most people money",
                     "/trader-timemachine", "/trader-budget", "longs only", "1,039.40", "1,061.00", "10.80", "1,071.80",
                     "too tight to survive the spread", "tick", "15:15", "15:20"):
            self.assertIn(must, low)
        for never in ("—", "tick " + "by tick", "real " + "time", "pro" + "fit", "yahoo", " et "):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()

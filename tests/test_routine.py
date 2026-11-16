"""The paper-month routine (step 11): the 10:00 breadth gate, the evening whole-universe replay, the two new risk
limits, the journal and the review. Fixtures: the 174-chart Nov 3 2026 5-minute bundle and the Nov 3 orders file."""

import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

scan = load("trader-scanner", "scan")
tm = load("trader-timemachine", "timemachine")
check = load("trader-risk", "check")
ORB = os.path.join(FIX, "trader-scanner", "orb5m-2026-11-03.json.gz")
MASTER = os.path.join(FIX, "trader_data", "instruments-equity-2026-11-04.csv.gz")
ORDERS = os.path.join(FIX, "trader-risk", "orders-tataconsum.json")
DAY = datetime.date(2026, 11, 3)


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def at(hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime.datetime(2026, 11, 3, h, m)


class BreadthGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = scan.load_offline(ORB)[0]

    def test_reading_from_bars(self):
        r = scan.breadth_from_bundle(self.data, DAY, at("10:00"))
        self.assertEqual(r, {"counted": 171, "above": 107, "pct": 62.6})
        self.assertEqual(scan.breadth_from_bundle(self.data, DAY, at("09:40"))["pct"], 64.3)
        self.assertEqual(scan.breadth_from_bundle(self.data, DAY, at("15:30"))["pct"], 57.9)

    def test_reading_from_quotes(self):
        q = {"A": {"open": 100, "ltp": 101, "volume": 5}, "B": {"open": 100, "ltp": 99, "volume": 5},
             "C": {"open": 40, "ltp": 41, "volume": 5},           # under the price floor
             "D": {"open": 100, "ltp": 102, "volume": 0}}         # has not traded
        self.assertEqual(scan.breadth_from_quotes(q), {"counted": 2, "above": 1, "pct": 50.0})
        self.assertEqual(scan.breadth_from_quotes({})["pct"], None)

    def test_verdicts(self):
        r = {"counted": 171, "above": 107, "pct": 62.6}
        v = scan.gate_verdict(r, at("09:40"), 65.0, "10:00")
        self.assertFalse(v["open"])
        self.assertIn("before the gate time 10:00", v["reason"])
        v = scan.gate_verdict(r, at("10:00"), 65.0, "10:00")
        self.assertEqual((v["open"], v["reason"]), (False, "62.6% of 171 charts above their open at 10:00, under the 65% gate"))
        v = scan.gate_verdict(r, at("10:05"), 60.0, "10:00")
        self.assertTrue(v["open"])
        self.assertIn("at or over the 60% gate", v["reason"])
        self.assertFalse(scan.gate_verdict({"counted": 0, "above": 0, "pct": None}, at("10:00"), 65, "10:00")["open"])

    def test_cli_writes_the_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "breadth.json")
            code, out, err = run(scan, ["breadth", "--offline", ORB, "--now", "2026-11-03 10:00", "--out", out_path,
                                        "--config", "/nonexistent"])
            self.assertEqual(code, 1, err)
            self.assertIn("Breadth Tue 2026-11-03 10:00 IST: 107 of 171 charts above their 09:15 open (62.6%).", out)
            self.assertIn("GATE CLOSED: 62.6% of 171 charts above their open at 10:00, under the 65% gate.", out)
            self.assertIn("trader-risk blocks them", out)
            with open(out_path) as fh:
                doc = json.load(fh)
            self.assertEqual((doc["date"], doc["time"], doc["pct"], doc["open"], doc["gate_pct"]),
                             ("2026-11-03", "10:00", 62.6, False, 65.0))
            code, out, _ = run(scan, ["breadth", "--offline", ORB, "--now", "2026-11-03 10:00", "--out", out_path,
                                      "--config", "/nonexistent", "--gate", "60"])
            self.assertEqual(code, 0)
            self.assertIn("GATE OPEN", out)
            code, out, _ = run(scan, ["breadth", "--offline", ORB, "--now", "2026-11-03 09:40", "--out", out_path,
                                      "--config", "/nonexistent"])
            self.assertIn("too early to decide", out)
            self.assertIn("Re-run at 10:00.", out)


class UniverseReplay(unittest.TestCase):
    def test_the_rule_and_the_baseline_on_174_charts(self):
        day, sessions = tm.load_universe(ORB)
        self.assertEqual((day, len(sessions)), ("2026-11-03", 174))
        rows = tm.trader_data.parse_instruments(tm.trader_data.read_text(MASTER))
        rule = dict(tm.U_DEFAULTS, stop_after_loss=True)
        rec = tm.universe_replay(day, sessions, rule, rows)
        s = tm.universe_summary(rec)
        # gate the trading, not the measurement: the closed day still scores its 32 breakouts, the ledger is empty
        self.assertEqual((s["breadth_pct"], s["gate_open"], s["breakouts"]), (62.6, False, 32))
        self.assertEqual((s["target"], s["stop"], s["flat"], s["avg_R"], s["net"], rec["ledger"]), (5, 14, 13, 0.105, -648.44, []))
        self.assertIn("measured, not taken: every breakout at Rs 10,000 each: 32 seen", tm.render_universe(rec, "x"))
        old = tm.universe_summary(tm.universe_replay(day, sessions, dict(rule, measure_closed=False), rows))
        self.assertEqual(old["breakouts"], 0)
        base = dict(rule, target_r=1.0, gate_pct=0.0, cutoff="", stop_after_loss=False, max_trades=3)
        rec = tm.universe_replay(day, sessions, base, rows)
        s = tm.universe_summary(rec)
        self.assertEqual((s["breakouts"], s["target"], s["stop"], s["flat"], s["flat_green"]), (90, 34, 34, 22, 15))
        self.assertEqual((s["avg_R"], s["gross"], s["charges"], s["net"]), (0.029, -522.4, 2345.9, -2868.3))
        self.assertEqual([(t["symbol"], t["at"], t["qty"], t["why"], t["net"]) for t in rec["ledger"]],
                         [("HDFCBANK", "09:35", 10, "target", 29.33), ("GMRAIRPORT", "12:05", 82, "stop", -151.7),
                          ("TITAN", "14:55", 1, "flat", -22.12)])
        self.assertEqual(s["ledger_net"], -144.49)
        two_r = dict(rule, gate_pct=0.0)
        s = tm.universe_summary(tm.universe_replay(day, sessions, two_r, rows))
        self.assertEqual((s["breakouts"], s["target"], s["stop"], s["flat"], s["avg_R"], s["net"]), (75, 13, 33, 29, 0.109, -1542.54))
        self.assertEqual(s["ledger_net"], 60.32)               # HDFCBANK alone: flat at 724.70 against a 2R target

    def test_slippage_short_side_selection_and_the_cost_filter(self):
        day, sessions = tm.load_universe(ORB)
        rows = tm.trader_data.parse_instruments(tm.trader_data.read_text(MASTER))
        rule = dict(tm.U_DEFAULTS, stop_after_loss=True)
        g = dict(rule, gate_pct=0.0)

        def run(r, extras=None):
            rec = tm.universe_replay(day, sessions, r, rows, None, None, extras)
            s = tm.universe_summary(rec)
            return rec, s
        # 0.05 % a side on every market fill: same 75 trades, Rs 631 worse than -1,542.54 without it
        rec, s = run(dict(g, slippage_pct=0.05))
        self.assertEqual((s["breakouts"], s["gross"], s["charges"], s["net"], s["ledger_net"]), (75, -204.29, 1969.38, -2173.67, 53.12))
        # the short side: the first close under the range low, stop at the midpoint, 2R down
        rec, s = run(dict(g, side="short"))
        self.assertEqual((s["breakouts"], s["target"], s["stop"], s["flat"], s["net"]), (31, 4, 15, 12, -755.48))
        self.assertEqual([(t["symbol"], t["side"], t["qty"], t["why"], t["net"]) for t in rec["ledger"]],
                         [("TITAN", "short", 1, "stop", -55.7)])
        # a short gate opens on weak days: at or under the level (62.6 % is not under 40, is under 70)
        self.assertFalse(run(dict(rule, side="short", gate_pct=40.0))[1]["gate_open"])
        rec, s = run(dict(rule, side="short", gate_pct=70.0))
        self.assertEqual((s["gate_open"], s["breakouts"], s["net"], s["ledger_net"]), (True, 15, -35.9, 11.75))
        # charges over 15 % of the rupee risk: at Rs 10,000 a trade only 5 of 75 breakouts have stops wide enough
        self.assertEqual(run(dict(g, max_charges_pct_of_risk=15))[1]["breakouts"], 5)
        # a fixed symbol set
        self.assertEqual(run(dict(g, symbols=["HDFCBANK", "TITAN", "RELIANCE"]))[1]["breakouts"], 1)
        # relative volume, chosen at 09:30 from the opening range against each stock's own average
        ex = {sym: {"or_vol_avg": sum(b["v"] for b in bars[:3])} for sym, bars in sessions.items()}
        ex["HDFCBANK"]["or_vol_avg"] /= 10
        rec, s = run(dict(g, rank="rvol", top_n=5), ex)
        self.assertEqual([e["symbol"] for e in rec["every"]], ["HDFCBANK"])

    def test_a_stop_gapped_through_fills_at_the_open(self):
        bars = [{"o": 100, "h": 100.5, "l": 99.8, "c": 100, "v": 1000} for _ in range(tm.SESSION_BARS)]
        for i in range(3):
            bars[i] = dict(bars[i], h=101, l=98)                 # range 98-101, midpoint stop 99.50
        bars[3] = dict(bars[3], h=102, c=101.5)               # the breakout close; fills at the next open, 100
        bars[6] = dict(bars[6], o=97.0, h=97.5, l=96.0, c=96.5)    # opens under the stop
        rec = tm.universe_replay("2026-11-03", {"X": bars}, dict(tm.U_DEFAULTS, gate_pct=0.0, first_entry="09:30"))
        e = rec["every"][0]
        self.assertEqual((e["why"], e["stop"], e["exit"]), ("stop", 99.5, 97.0))

    def test_multiday_one_change_at_a_time_train_then_test(self):
        import gzip as gz
        import json as js
        import tempfile
        src = js.load(gz.open(ORB, "rt"))
        folder = tempfile.mkdtemp()
        for k in range(6):                               # the fixture day, shifted to six dates
            doc = dict(src, bars={s: [dict(b, ts=b["ts"] + 86400 * k) for b in bars] for s, bars in src["bars"].items()})
            with gz.open(os.path.join(folder, "orb5m-2026-11-%02d.json.gz" % (3 + k)), "wt") as fh:
                js.dump(doc, fh)
        days = tm.md_bundles(folder)
        self.assertEqual((len(days), days[0][0], days[-1][0]), (6, "2026-11-03", "2026-11-08"))
        rule = dict(tm.U_DEFAULTS, stop_after_loss=True, slippage_pct=0.05, strategy="orb")
        res = tm.multiday_replay(days, rule, "gate off,liquid 174,rel. volume top 20,short below 40%")
        self.assertEqual(sorted(res), ["gate off", "liquid 174", "rel. volume top 20", "rule", "short below 40%"])
        # the rule: 62.6 % breadth, gate closed every day, yet every day's 32 breakouts are measured
        self.assertEqual([(d["gate_open"], len(d["ledger"]), len(d["every"])) for d in res["rule"]["days"]], [(False, 0, 32)] * 6)
        # the history-based variants wait for 5 earlier days, so they start on the sixth
        self.assertEqual([d["date"] for d in res["liquid 174"]["days"]], ["2026-11-08"])
        self.assertEqual(len(res["rel. volume top 20"]["days"][0]["every"]), 4)
        s = tm.multiday_summary(res, 2)
        self.assertEqual((s["days"], s["train_to"], s["test_from"]), (6, "2026-11-06", "2026-11-07"))
        g = s["variants"]["gate off"]
        self.assertEqual((g["train"]["ledger"]["n"], g["train"]["ledger"]["net"], g["test"]["ledger"]["net"]), (4, -517.32, -258.66))
        self.assertEqual((g["train"]["every"]["n"], g["train"]["every"]["expectancy"]), (128, -28.74))
        self.assertEqual(s["variants"]["rule"]["test"]["ledger"]["n"], 0)
        self.assertIn("scored on 2026-11-07..2026-11-08 (2 days)", tm.render_multiday(s))

    def test_multiday_base_stacks_one_more_change(self):
        rule = dict(tm.U_DEFAULTS)
        v = dict(tm.md_variants(rule, base="short below 40%"))
        self.assertEqual(list(v)[:5], ["rule", "short below 40%", "short below 40% + below 30%",
                                       "short below 40% + below 50%", "short below 40% + gate off"])
        self.assertEqual(v["rule"].get("side"), None)
        self.assertEqual((v["short below 40% + below 30%"]["side"], v["short below 40% + below 30%"]["gate_pct"]), ("short", 30.0))
        self.assertEqual((v["short below 40% + target 3R"]["gate_pct"], v["short below 40% + target 3R"]["target_r"]), (40.0, 3.0))
        self.assertNotIn("short below 40% + vwap", v)                      # vwap is long only
        self.assertNotIn("short below 40% + short below 40%", v)
        self.assertEqual(list(dict(tm.md_variants(rule, "short below 40% + liquid 174", base="short below 40%"))),
                         ["rule", "short below 40%", "short below 40% + liquid 174"])
        self.assertEqual(dict(tm.md_variants(rule, base="target 3R"))["target 3R + cutoff 12:00"]["cutoff"], "12:00")
        with self.assertRaises(tm.TMError):
            tm.md_variants(rule, base="nonsense")

    def test_the_other_strategies_on_174_charts(self):
        day, sessions = tm.load_universe(ORB)
        rows = tm.trader_data.parse_instruments(tm.trader_data.read_text(MASTER))
        base = dict(tm.U_DEFAULTS, gate_pct=0.0, cutoff="", stop_after_loss=True)
        got = {}
        for strat in ("vwap", "ema", "supertrend_rsi"):
            s = tm.universe_summary(tm.universe_replay(day, sessions, dict(base, strategy=strat), rows))
            got[strat] = (s["breakouts"], s["target"], s["stop"], s["flat"], s["rule_exit"], s["avg_R"], s["net"])
        self.assertEqual(got["vwap"], (131, 59, 63, 9, 0, -0.024, -3539.05))
        self.assertEqual(got["ema"], (106, 31, 58, 17, 11, 0.027, -2707.61))
        self.assertEqual(got["supertrend_rsi"], (61, 2, 18, 41, 0, -0.049, -1720.44))
        # the two published rules need the daily bundle: ATR, volume and the previous close before the day
        import gzip
        with gzip.open(os.path.join(FIX, "trader-scanner", "daily-2026-11-04.json.gz"), "rt", encoding="utf-8") as fh:
            dbars = json.load(fh)["bars"]
        extras = tm.daily_extras(dbars, day)
        self.assertEqual(len(extras), 174)
        self.assertEqual({k: round(v, 2) for k, v in extras["TATACONSUM"].items()},
                         {"atr14": 25.06, "avg_vol14": 2137122.43, "prev_close": 1028.9})
        for strat in ("orb_play", "half_hour"):
            s = tm.universe_summary(tm.universe_replay(day, sessions, dict(base, strategy=strat), rows, None, None, extras))
            got[strat] = (s["breakouts"], s["target"], s["stop"], s["flat"], s["avg_R"], s["gross"], s["charges"], s["net"],
                          s["ledger_net"])
        self.assertEqual(got["orb_play"], (7, 0, 6, 1, 0.481, 55.67, 188.97, -133.3, -43.04))   # 10 % ATR stops: 6 of 7
        self.assertEqual(got["half_hour"], (132, 0, 0, 132, 0.044, 1128.02, 3367.07, -2239.05, -24.69))  # gross up, charges 3x
        rec = tm.universe_replay(day, sessions, dict(base, strategy="orb_play"), rows, None, None, extras)
        self.assertEqual([(e["symbol"], e["at"], e["fill"], e["stop"], e["why"]) for e in rec["every"]][:2],
                         [("ICICIGI", "09:35", 1570.0, 1565.7, "stop"), ("DEVYANI", "09:35", 119.27, 118.76, "stop")])
        self.assertTrue(all(e["target"] is None for e in rec["every"]))
        self.assertIsNone(tm.first_signal("orb_play", sessions["TATACONSUM"], base, 0.1, 9 * 60 + 35, 15 * 60, 3))   # no extra
        # the definitions, on one made-up session
        flat = [{"o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0, "v": 1000}] * 75
        self.assertIsNone(tm.first_signal("vwap", flat, base, 0.05, 9 * 60 + 35, 15 * 60, 3))
        dip = [dict(b) for b in flat]
        dip[20] = {"o": 100.0, "h": 100.0, "l": 99.0, "c": 99.2, "v": 1000}        # 0.8 % under the VWAP
        sig = tm.first_signal("vwap", dip, base, 0.05, 9 * 60 + 35, 15 * 60, 3)
        self.assertEqual((sig["k"], sig["close_min"]), (20, 9 * 60 + 15 + 105))
        self.assertEqual(sig["entry"], 99.2)
        self.assertAlmostEqual(sig["target"], 100.0, places=1)                    # the VWAP, on the tick
        self.assertAlmostEqual(sig["stop"], 99.2 - (sig["target"] - 99.2), places=1)
        with self.assertRaises(tm.TMError):
            tm.first_signal("martingale", flat, base, 0.05, 0, 900, 3)
        # half_hour: up first half hour against the previous close, in at the 14:45 open, no target
        ex = dict(base, _extra={"prev_close": 99.0, "atr14": 2.0})
        sig = tm.first_signal("half_hour", flat, ex, 0.05, 9 * 60 + 35, 15 * 60 + 30, 3)
        self.assertEqual((sig["k"], tm.clock(sig["close_min"]), sig["entry"], sig["stop"], sig["target"]), (65, "14:45", 100.0, 98.0, None))
        self.assertIsNone(tm.first_signal("half_hour", flat, dict(base, _extra={"prev_close": 101.0}), 0.05, 575, 930, 3))
        # orb_play: the first bar must close up; the stop order at its high fills on the first bar through it
        up = [dict(b) for b in flat]
        up[0] = {"o": 100.0, "h": 101.0, "l": 99.8, "c": 100.8, "v": 5000}
        up[7] = {"o": 100.9, "h": 101.4, "l": 100.7, "c": 101.2, "v": 1000}
        ex = dict(base, _extra={"atr14": 2.0, "avg_vol14": 100000.0})
        sig = tm.first_signal("orb_play", up, ex, 0.05, 9 * 60 + 35, 15 * 60, 3)
        self.assertEqual((sig["k"], sig["walk_from"], sig["fill"], sig["stop"], sig["target"]), (7, 7, 101.0, 100.8, None))
        self.assertAlmostEqual(sig["rank"], 0.05)
        self.assertIsNone(tm.first_signal("orb_play", flat, ex, 0.05, 575, 900, 3))          # first bar flat: no direction

    def test_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "replay.json")
            code, out, err = run(tm, ["universe", ORB, "--strategy", "vwap", "--gate", "0", "--cutoff", "none",
                                      "--config", "/nonexistent", "--master", MASTER])
            self.assertEqual(code, 0, err)
            self.assertIn("2026-11-03, vwap, 2R, gate off, cutoff none: 174 charts", out)
            self.assertIn("every signal at Rs 10,000 each: 131 taken, 59 target, 63 stop, 9 flat at 15:15", out)
            code, out, err = run(tm, ["universe", ORB, "--config", "/nonexistent", "--compare", "--master", MASTER,
                                      "--write", path])
            self.assertEqual(code, 0, err)
            self.assertIn("2026-11-03, 2R, gate 65%, cutoff 11:00: 174 charts with a full session; breadth at 10:00 62.6% "
                          "(gate 65%): CLOSED, no trades.", out)
            self.assertIn("every breakout at Rs 10,000 each: 90 taken, 34 target, 34 stop, 22 flat at 15:15 (15 of those "
                          "above the entry); average R +0.03; gross -Rs 522.40, charges Rs 2,345.90, net -Rs 2,868.30.", out)
            self.assertIn("09:35 HDFCBANK     10 at 716.05, stop 710.90, target 721.60 -> target 721.60 at 12:00: +Rs 29.33", out)
            self.assertIn("Not a forecast", out)
            with open(path) as fh:
                doc = json.load(fh)
            self.assertEqual(doc["summary"]["gate_open"], False)
            self.assertEqual(doc["baseline"]["breakouts"], 90)
            self.assertIsNone(doc["every"])
            code, out, err = run(tm, ["universe", ORB, "--config", "/nonexistent", "--gate", "0", "--target", "1R",
                                      "--cutoff", "none", "--max-trades", "3", "--no-stop-after-loss", "--master", MASTER])
            self.assertIn("90 taken", out)
            code, _, err = run(tm, ["universe", ORB, "--min-score", "5"])
            self.assertEqual(code, 1)
            self.assertIn("needs --daily", err)


class RiskLimits(unittest.TestCase):
    def setUp(self):
        with open(ORDERS) as fh:
            self.orders = json.load(fh)["orders"]
        self.cfg = {"budget": 100000, "breadth_gate_pct": 65, "gate_time": "10:00", "stop_after_loss": True}

    def reasons(self, res):
        return [r for x in res["results"] for r in x["reasons"]]

    def test_gate_reasons(self):
        c = dict(check.DEFAULTS, breadth_gate_pct=65)
        self.assertEqual(check.gate_reasons(c, None, "2026-11-03"),
                         ["gate: no breadth reading for 2026-11-03; run scan.py breadth at 10:00 first (breadth_gate_pct 65)"])
        self.assertEqual(check.gate_reasons(c, {"date": "2026-11-02", "open": True}, "2026-11-03")[0][:33],
                         "gate: no breadth reading for 2026")
        self.assertEqual(check.gate_reasons(c, {"date": "2026-11-03", "open": False, "reason": "62.6% ... under the 65% gate"},
                                            "2026-11-03"), ["gate: 62.6% ... under the 65% gate"])
        self.assertEqual(check.gate_reasons(c, {"date": "2026-11-03", "open": True}, "2026-11-03"), [])
        self.assertEqual(check.gate_reasons(check.DEFAULTS, None, "2026-11-03"), [])      # no gate configured

    def test_the_gate_blocks_an_entry(self):
        res = check.check(self.orders, self.cfg, now=at("10:01"))
        self.assertEqual(res["block"], 1)
        self.assertTrue(any(r.startswith("gate: no breadth reading") for r in self.reasons(res)))
        closed = {"date": "2026-11-03", "time": "10:00", "pct": 62.6, "open": False,
                  "reason": "62.6% of 171 charts above their open at 10:00, under the 65% gate"}
        res = check.check(self.orders, self.cfg, now=at("10:01"), breadth=closed)
        self.assertIn("gate: 62.6% of 171 charts above their open at 10:00, under the 65% gate", self.reasons(res))
        self.assertIn("gate closed: 62.6% of charts above their open at 10:00 (gate 65%)", res["results"][0]["info"])
        opened = dict(closed, pct=71.9, open=True, reason="71.9% ... at or over the 65% gate")
        res = check.check(self.orders, self.cfg, now=at("10:01"), breadth=opened)
        self.assertEqual(res["pass"], 1)
        self.assertIn("gate open: 71.9% of charts above their open at 10:00 (gate 65%)", res["results"][0]["info"])

    def test_the_cost_check(self):
        opened = {"date": "2026-11-03", "time": "10:00", "pct": 71.9, "open": True, "reason": "open"}
        res = check.check(self.orders, self.cfg, now=at("10:01"), breadth=opened)
        self.assertEqual(res["pass"], 1)                     # off unless configured
        tight = dict(self.cfg, max_charges_pct_of_risk=1)
        res = check.check(self.orders, tight, now=at("10:01"), breadth=opened)
        self.assertEqual(res["block"], 1)
        cost = [r for r in self.reasons(res) if r.startswith("cost: ")]
        self.assertEqual(len(cost), 1)
        self.assertIn("of the", cost[0])
        self.assertIn("over your 1% (max_charges_pct_of_risk)", cost[0])
        res = check.check(self.orders, dict(self.cfg, max_charges_pct_of_risk=15), now=at("10:01"), breadth=opened)
        self.assertEqual(res["pass"], 1)

    def test_one_loss_ends_the_day(self):
        ledger = {"orders": [
            {"id": "PAPER-GTT-2", "txn_type": "SELL", "status": "SUCCESS", "symbol": "BEL", "pnl": -151.7,
             "updated_at": "2026-11-03T09:58:00+05:30"},
            {"id": "PAPER-GTT-4", "txn_type": "SELL", "status": "SUCCESS", "symbol": "OLD", "pnl": -9.0,
             "updated_at": "2026-11-02T14:00:00+05:30"},                       # yesterday: not counted
            {"id": "PAPER-GTT-6", "txn_type": "SELL", "status": "SUCCESS", "symbol": "WIN", "pnl": 40.0,
             "updated_at": "2026-11-03T09:50:00+05:30"},
            {"id": "PAPER-EQ-1", "txn_type": "BUY", "status": "SUCCESS", "symbol": "BEL",
             "updated_at": "2026-11-03T09:40:00+05:30"}]}
        self.assertEqual(check.losses_today(ledger, "2026-11-03"), [{"symbol": "BEL", "pnl": -151.7, "time": "09:58"}])
        opened = {"date": "2026-11-03", "open": True, "pct": 71.9, "time": "10:00"}
        res = check.check(self.orders, self.cfg, now=at("10:01"), breadth=opened, ledger=ledger)
        self.assertEqual(res["block"], 1)
        self.assertIn("stopped for the day: BEL closed at Rs -151.70 before charges at 09:58, and stop_after_loss is on",
                      self.reasons(res))
        res = check.check(self.orders, dict(self.cfg, stop_after_loss=False), now=at("10:01"), breadth=opened, ledger=ledger)
        self.assertEqual(res["pass"], 1)
        res = check.check(self.orders, dict(self.cfg, paper=False), now=at("10:01"), breadth=opened)
        self.assertTrue(any("live fills are not read yet" in w for w in res["warnings"]))

    def test_cli_reads_the_two_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "config.json")
            with open(cfg, "w") as fh:
                json.dump(self.cfg, fh)
            b = os.path.join(tmp, "breadth.json")
            with open(b, "w") as fh:
                json.dump({"date": "2026-11-03", "time": "10:00", "pct": 71.9, "open": True, "reason": "ok"}, fh)
            code, out, err = run(check, ["--orders", ORDERS, "--config", cfg, "--now", "2026-11-03 10:01", "--breadth", b,
                                         "--paper-ledger", os.path.join(tmp, "none.json"), "--no-write",
                                         "--portfolio", os.path.join(tmp, "none.json"), "--prices", os.path.join(tmp, "n.json"),
                                         "--state", os.path.join(tmp, "n.json")])
            self.assertEqual(code, 0, err + out)
            self.assertIn("gate open: 71.9%", out)
            code, out, _ = run(check, ["--orders", ORDERS, "--config", cfg, "--now", "2026-11-03 10:01",
                                       "--breadth", os.path.join(tmp, "missing.json"), "--no-write",
                                       "--portfolio", os.path.join(tmp, "none.json"), "--prices", os.path.join(tmp, "n.json"),
                                       "--state", os.path.join(tmp, "n.json")])
            self.assertEqual(code, 1)
            self.assertIn("gate: no breadth reading for 2026-11-03", out)


class JournalAndReview(unittest.TestCase):
    def test_paper_day_counts_trading_days_from_the_start(self):
        # The paper month started Fri 2026-11-13; the 24th is Guru Nanak Jayanti.
        self.assertEqual(check.paper_day("2026-11-13", "2026-11-13"), 1)
        self.assertEqual(check.paper_day("2026-11-13", "2026-11-16"), 2)
        self.assertEqual(check.paper_day("2026-11-13", "2026-11-25"), 8)
        self.assertIsNone(check.paper_day("2026-11-13", "2026-11-06"))
        self.assertIsNone(check.paper_day("2026-11-13", "2026-11-14"))
        self.assertIsNone(check.paper_day(None, "2026-11-13"))
        j = check.journal_entry("2026-11-13", {}, paper_start="2026-11-13")
        self.assertEqual(j["paper_day"], 1)
        self.assertTrue(check.render_journal(j).startswith("Journal 2026-11-13, paper day 1"))
        # The morning's daily scan is "as of" the previous close but belongs to the day it was made.
        w = {"mode": "daily", "created_at": "2026-11-13T09:06:59+05:30", "as_of": "2026-11-12 close", "hits": [], "scanned": 506}
        self.assertIsNone(check.journal_entry("2026-11-12", {"watchlist": w})["scan"])
        self.assertEqual(check.journal_entry("2026-11-13", {"watchlist": w})["scan"]["scanned"], 506)

    def files(self):
        return {
            "breadth": {"date": "2026-11-03", "time": "10:00", "pct": 62.6, "open": False, "reason": "under the gate"},
            "watchlist": {"mode": "orb", "as_of": "2026-11-03 10:00 IST", "hits": [{"symbol": "X"}] * 48, "scanned": 171},
            "entry": {"date": "2026-11-03", "symbol": "TATACONSUM", "status": "triggered", "time": "10:00", "price": 1050.2},
            "plan": {"date": "2026-11-03", "symbol": "TATACONSUM", "entry": 1050.2, "stop": 1039.4, "target": 1061.0,
                     "stop_rule": "or_mid", "target_r": 1.0, "risk_per_share": 10.8},
            "timemachine": {"plan": {"date": "2026-11-03"}, "mc": {"p_target": 0.2982, "p_stop": 0.3588, "p_neither": 0.343},
                            "verdict": "Close to a coin flip: target first 29.8 % vs stop first 35.9 %.",
                            "history": {"rule": {}, "triggered": 26, "target": 15, "stop": 7, "close": 4}},
            "risk": {"checked_at": "2026-11-03T10:01:00+05:30", "pass": 0, "block": 1,
                     "results": [{"reasons": ["gate: under the gate", "size: too big"]}]},
            "ledger": {"orders": [
                {"id": "PAPER-EQ-1", "txn_type": "BUY", "status": "SUCCESS", "symbol": "TATACONSUM", "traded_qty": 71,
                 "traded_price": 1050.5, "charges": 16.92, "updated_at": "2026-11-03T10:02:00+05:30", "what": "fill"},
                {"id": "PAPER-GTT-2", "txn_type": "SELL", "status": "SUCCESS", "symbol": "TATACONSUM", "traded_qty": 71,
                 "traded_price": 1061.0, "charges": 33.54, "pnl": 745.5, "updated_at": "2026-11-03T11:10:00+05:30",
                 "what": "target leg"}]},
            "replay": {"summary": {"breakouts": 0, "target": 0, "stop": 0, "flat": 0, "avg_R": None, "net": 0, "gate_open": False},
                       "baseline": {"breakouts": 90, "avg_R": 0.029, "net": -2868.3}},
        }

    def test_one_day(self):
        j = check.journal_entry("2026-11-03", self.files(), ["took it anyway, gate was closed: a mistake"])
        self.assertEqual(j["breadth"]["open"], False)
        self.assertEqual(j["scan"], {"mode": "orb", "as_of": "2026-11-03 10:00 IST", "hits": 48, "scanned": 171})
        self.assertEqual(j["plan"]["stop_rule"], "or_mid")
        self.assertEqual(j["timemachine"]["history"], "26 fired: 15 target, 7 stop, 4 neither")
        self.assertEqual(j["risk"]["blocked_on"], ["gate", "size"])
        self.assertEqual(len(j["fills"]), 2)
        self.assertEqual(j["trades"], {"closed": 1, "won": 1, "lost": 0, "gross": 745.5, "charges": 50.46, "net": 695.04})
        self.assertEqual(j["notes"][0]["note"], "took it anyway, gate was closed: a mistake")
        # the trade for the metrics: 745.50 less 33.54 and 16.92 of charges; risk 10.80 a share x 71
        self.assertEqual(j["paper_trades"], [{"date": "2026-11-03", "symbol": "TATACONSUM", "net": 695.04, "risk_inr": 766.8}])
        text = check.render_journal(j)
        self.assertIn("gate      closed at 10:00: 62.6%, under the gate", text)
        self.assertIn("entry     TATACONSUM triggered 10:00 at 1050.2", text)
        self.assertIn("fill      11:10 SELL 71 TATACONSUM at 1061.0, Rs 745.50 before charges", text)
        self.assertIn("trades    1 closed, 1 won, 0 lost: gross Rs 745.50, charges Rs 50.46, net Rs 695.04", text)
        self.assertIn("baseline 1R no gate: 90 breakouts, avg R 0.029, net Rs -2,868.30", text)
        # another day's files do not leak in
        j2 = check.journal_entry("2026-11-04", self.files())
        self.assertTrue(all(j2[k] is None for k in ("breadth", "scan", "entry", "plan", "timemachine", "risk")))
        self.assertEqual(j2["fills"], [])
        # re-running keeps earlier notes and appends
        j3 = check.journal_entry("2026-11-03", self.files(), ["second note"], existing=j)
        self.assertEqual([n["note"] for n in j3["notes"]], ["took it anyway, gate was closed: a mistake", "second note"])

    def test_review_rolls_up(self):
        a = check.journal_entry("2026-11-03", self.files())
        f2 = self.files()
        f2["ledger"]["orders"][1].update({"pnl": -300.0, "symbol": "BEL", "updated_at": "2026-11-04T12:00:00+05:30"})
        f2["ledger"]["orders"][0].update({"symbol": "BEL", "updated_at": "2026-11-04T10:30:00+05:30"})
        f2["breadth"].update({"date": "2026-11-04", "open": True})
        f2["replay"]["summary"].update({"net": 5399.95, "gate_open": True})
        b = check.journal_entry("2026-11-04", f2)
        r = check.review([b, a])
        self.assertEqual((r["from"], r["to"]), ("2026-11-03", "2026-11-04"))
        t = r["totals"]
        self.assertEqual((t["days"], t["days_traded"], t["closed"], t["won"], t["lost"]), (2, 2, 2, 1, 1))
        self.assertEqual((t["gross"], t["charges"], t["net"]), (445.5, 100.92, 344.58))
        self.assertEqual((t["gate_open_days"], t["gate_read_days"]), (1, 2))
        self.assertEqual((t["replay_days"], t["replay_rule_net"], t["replay_base_net"]), (2, 5399.95, -5736.6))
        self.assertEqual(list(r["by_symbol"]), ["TATACONSUM", "BEL"])
        text = check.render_review(r)
        self.assertIn("2 journal days, 2 with a closed trade", text)
        self.assertIn("BEL          1 trade, 0 won, gross Rs -300", text)
        # efficiency, not just counts: net per trade with its 95 % range, net R, profit factor, drawdown, weeks
        self.assertEqual((r["paper"]["n"], r["paper"]["net"], r["paper"]["expectancy"], r["paper"]["profit_factor"],
                          r["paper"]["max_drawdown"], r["paper"]["weekly"]), (2, 344.58, 172.29, 1.98, 350.46, {"2026-W45": 344.58}))
        self.assertIn("paper     2 trades (1 won, 1 lost), net Rs +344.58; expectancy Rs +172.29 a trade (95% Rs -852.30 to Rs +1,196.88)", text)
        self.assertIn("replay    the rule's ledger: no closed trades", text)

    def test_cli_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            jd = os.path.join(tmp, "journal")
            none = os.path.join(tmp, "none.json")
            b = os.path.join(tmp, "breadth.json")
            with open(b, "w") as fh:
                json.dump({"date": "2026-11-03", "time": "10:00", "pct": 62.6, "open": False, "reason": "under"}, fh)
            args = ["--journal-dir", jd, "--breadth", b, "--watchlist", none, "--entry", none, "--plan", none,
                    "--timemachine", none, "--out", none, "--paper-ledger", none, "--replays-dir", tmp,
                    "--config", none, "--multiday", none]
            code, out, err = run(check, ["journal", "--today", "2026-11-03", "--note", "quiet day"] + args)
            self.assertEqual(code, 0, err)
            self.assertIn("Journal 2026-11-03", out)
            self.assertIn("note", out)
            self.assertTrue(os.path.exists(os.path.join(jd, "2026-11-03.json")))
            code, out, err = run(check, ["review", "--today", "2026-11-03", "--days", "7"] + args)
            self.assertEqual(code, 0, err)
            self.assertIn("1 journal day, 0 with a closed trade", out)
            self.assertIn("gate      open on 0 of 1 days read", out)
            self.assertIn("go/no-go  NOT MET: no held-out replay trades yet (timemachine.py multiday); 0 paper trades, 200 needed", out)
            code, out, _ = run(check, ["review", "--today", "2026-10-01", "--days", "3"] + args)
            self.assertEqual(code, 1)
            self.assertIn("No journal days", out)


if __name__ == "__main__":
    unittest.main()

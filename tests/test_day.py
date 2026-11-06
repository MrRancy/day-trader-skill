"""trader-day, the conductor: the state machine over the trader/ files, and the dry-run commands."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import load

day = load("trader-day", "day")
CFG = {"gate_time": "10:00", "no_entries_after": "11:00", "flat_by": "15:15", "max_trades_per_day": 2,
       "stop_after_loss": True}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = day.main(argv)
    return code, out.getvalue(), err.getvalue()


class Scratch(unittest.TestCase):
    """Each test runs inside an empty folder with its own trader/ tree."""

    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        os.makedirs("trader/state")
        os.makedirs("trader/replays")
        os.makedirs("trader/journal")
        with open("trader/config.json", "w") as fh:
            json.dump(CFG, fh)

    def tearDown(self):
        os.chdir(self.old)

    def put(self, name, doc):
        path = day.FILES[name]
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def nxt(self, when):
        s = day.state(day.now_ist(when), CFG)
        return day.next_step(s)


class TheStateMachine(Scratch):
    def test_phases(self):
        for t, ph in (("08:00", "before the day"), ("08:45", "pre-market"), ("09:15", "early session"),
                      ("10:00", "entry window"), ("11:00", "holding window"), ("15:15", "flat by"), ("15:30", "evening"),
                      ("23:59", "evening")):
            self.assertEqual(day.phase_of(day.now_ist("2026-11-06 " + t), CFG), ph, t)
        self.assertEqual(day.phase_of(day.now_ist("2026-11-06 10:30"), {"gate_time": "10:30"}), "entry window")
        self.assertEqual(day.phase_of(day.now_ist("2026-11-06 10:29"), {"gate_time": "10:30"}), "early session")

    def test_a_closed_day_and_the_morning(self):
        what, cmd = self.nxt("2026-11-08 10:00")                     # Sunday
        self.assertTrue(what.startswith("No session today"))
        what, cmd = self.nxt("2026-11-06 08:00")
        self.assertEqual((what, cmd), ("Nothing until 08:45.", None))
        what, cmd = self.nxt("2026-11-06 08:50")                     # no universe yet
        self.assertEqual(cmd, "day.py morning (builds trader/universe.txt first)")
        with open("trader/universe.txt", "w") as fh:
            fh.write("RELIANCE|Reliance Industries Ltd\n")
        what, cmd = self.nxt("2026-11-06 08:50")
        self.assertEqual((what, cmd), ("The pre-market scan has not run.", "day.py morning"))
        self.put("watchlist", {"mode": "daily", "as_of": "2026-11-05 close", "hits": [{}] * 88, "created_at": "2026-11-06T08:46:00+05:30"})
        what, cmd = self.nxt("2026-11-06 08:50")
        self.assertIn("take nothing before the gate", what)
        what, cmd = self.nxt("2026-11-06 09:40")
        self.assertIn("Watch the early breakouts", what)

    def test_the_entry_window_step_by_step(self):
        with open("trader/universe.txt", "w") as fh:
            fh.write("X|X\n")
        self.assertEqual(self.nxt("2026-11-06 10:01"), ("The gate has not been read.", "day.py gate"))
        self.put("breadth", {"date": "2026-11-06", "time": "10:00", "pct": 48.8, "open": False})
        what, cmd = self.nxt("2026-11-06 10:01")
        self.assertEqual(what, "The gate is closed (48.8% at 10:00): a watching day.")
        self.put("breadth", {"date": "2026-11-06", "time": "10:00", "pct": 71.9, "open": True})
        what, cmd = self.nxt("2026-11-06 10:01")
        self.assertEqual((what, cmd), ("Gate open. Pick ONE chart from the opening-range hits.", "entry.py check SYMBOL --setup orb"))
        self.put("entry", {"date": "2026-11-06", "symbol": "BEL", "status": "waiting", "time": None})
        self.assertEqual(self.nxt("2026-11-06 10:05")[1], "entry.py watch BEL")
        self.put("entry", {"date": "2026-11-06", "symbol": "BEL", "status": "triggered", "time": "10:05"})
        what, cmd = self.nxt("2026-11-06 10:06")
        self.assertEqual((what, cmd), ("BEL triggered at 10:05: set the exit before anything else.", "exit.py plan --target 2R --write"))
        self.put("plan", {"date": "2026-11-06", "symbol": "BEL", "stop": 384.4, "target": 405.9})
        self.assertEqual(self.nxt("2026-11-06 10:07")[1], "timemachine.py run --write")
        self.put("timemachine", {"plan": {"date": "2026-11-06"}})
        self.assertEqual(self.nxt("2026-11-06 10:08")[1], "size.py --write")
        self.put("orders", {"created_at": "2026-11-06T10:09:00+05:30", "orders": [{}]})
        self.assertEqual(self.nxt("2026-11-06 10:09")[1], "check.py")
        self.put("risk", {"checked_at": "2026-11-06T10:10:00+05:30", "pass": 0, "block": 1})
        what, cmd = self.nxt("2026-11-06 10:10")
        self.assertTrue(what.startswith("Risk blocked it"))
        self.assertIsNone(cmd)
        self.put("risk", {"checked_at": "2026-11-06T10:10:00+05:30", "pass": 1, "block": 0})
        self.assertEqual(self.nxt("2026-11-06 10:10")[1], "ticket.py show, then approve and place")
        # yesterday's files never count
        self.put("entry", {"date": "2026-11-05", "symbol": "OLD", "status": "triggered", "time": "10:05"})
        self.assertEqual(self.nxt("2026-11-06 10:11")[1], "entry.py check SYMBOL --setup orb")

    def test_open_positions_losses_and_limits(self):
        with open("trader/universe.txt", "w") as fh:
            fh.write("X|X\n")
        self.put("breadth", {"date": "2026-11-06", "time": "10:00", "pct": 71.9, "open": True})
        self.put("paper", {"positions": {"BEL": {"qty": 19, "avg_price": 391.75}}, "orders": []})
        what, cmd = self.nxt("2026-11-06 10:30")
        self.assertEqual((what, cmd), ("BEL is open on the paper ledger.", "day.py watch (every few minutes, or /loop 5m)"))
        self.assertEqual(self.nxt("2026-11-06 12:00")[0], "BEL is open on the paper ledger.")
        what, cmd = self.nxt("2026-11-06 15:16")
        self.assertEqual((what, cmd), ("BEL is still open at flat-by.", "ticket.py flatten --symbol BEL, then check.py and your yes"))
        self.put("paper", {"positions": {}, "orders": [
            {"txn_type": "SELL", "status": "SUCCESS", "symbol": "BEL", "pnl": -151.7, "updated_at": "2026-11-06T10:40:00+05:30"}]})
        what, cmd = self.nxt("2026-11-06 10:45")
        self.assertEqual((what, cmd), ("A loss closed the day (BEL).", "day.py evening after 15:30"))
        self.put("paper", {"positions": {}, "orders": [
            {"txn_type": "SELL", "status": "SUCCESS", "symbol": "BEL", "pnl": 100.0, "updated_at": "2026-11-06T10:40:00+05:30"},
            {"txn_type": "SELL", "status": "SUCCESS", "symbol": "SBIN", "pnl": 50.0, "updated_at": "2026-11-06T10:55:00+05:30"}]})
        self.assertEqual(self.nxt("2026-11-06 10:56")[0], "2 trades done, the day's limit.")
        self.put("paper", {"positions": {}, "orders": [
            {"txn_type": "SELL", "status": "SUCCESS", "symbol": "BEL", "pnl": 100.0, "updated_at": "2026-11-06T10:40:00+05:30"}]})
        self.assertEqual(self.nxt("2026-11-06 10:56")[1], "entry.py check SYMBOL --setup orb")   # one green trade: room for one more
        self.assertEqual(self.nxt("2026-11-06 11:30")[0], "Past the entry cutoff with nothing open.")
        self.assertEqual(self.nxt("2026-11-06 15:20")[0], "Flat by: nothing open.")

    def test_the_evening(self):
        with open("trader/universe.txt", "w") as fh:
            fh.write("X|X\n")
        self.assertEqual(self.nxt("2026-11-06 16:00")[1], "day.py evening --note \"...\"")
        open("trader/replays/orb5m-2026-11-06.json.gz", "wb").close()
        self.assertEqual(self.nxt("2026-11-06 16:00")[1],
                         "timemachine.py universe trader/replays/orb5m-2026-11-06.json.gz --compare --others --write")
        open("trader/replays/2026-11-06.json", "w").close()
        self.assertEqual(self.nxt("2026-11-06 16:00")[1], "check.py journal --note \"...\"")
        open("trader/journal/2026-11-06.json", "w").close()
        self.assertEqual(self.nxt("2026-11-06 16:00"), ("The day is done. Friday: check.py review --days 7.", None))


class TheCli(Scratch):
    def test_status_text_and_json(self):
        with open("trader/universe.txt", "w") as fh:
            fh.write("X|X\n")
        self.put("breadth", {"date": "2026-11-06", "time": "10:00", "pct": 48.8, "open": False})
        code, out, _ = run(["status", "--now", "2026-11-06 10:01"])
        self.assertEqual(code, 0)
        self.assertIn("2026-11-06 10:01 IST: entry window.", out)
        self.assertIn("gate      closed, 48.8% at 10:00", out)
        self.assertIn("Next: The gate is closed (48.8% at 10:00): a watching day.  ->  day.py gate again later", out)
        code, out, _ = run(["status", "--now", "2026-11-06 10:01", "--json"])
        doc = json.loads(out)
        self.assertEqual(doc["next"]["command"], "day.py gate again later in the window, else day.py evening")
        code, out, _ = run(["status", "--now", "2026-11-08 10:00"])
        self.assertIn("(NSE closed: weekend)", out)
        self.assertIn("Next: No session today (weekend).", out)

    def test_dry_runs_name_the_other_scripts(self):
        code, out, err = run(["morning", "--dry-run", "--now", "2026-11-06 08:45"])
        self.assertEqual(code, 0, err)
        self.assertIn("$ scan.py universe", out)                    # no universe in the scratch folder
        self.assertIn("$ scan.py daily --show 15 --now 2026-11-06 08:45", out)
        self.assertIn("$ news.py --hours 18 --now 2026-11-06T08:45", out)
        code, out, _ = run(["gate", "--dry-run", "--now", "2026-11-06 10:00"])
        self.assertIn("$ scan.py breadth --now 2026-11-06 10:00", out)
        self.assertIn("$ scan.py orb --show 20 --now 2026-11-06 10:00", out)
        code, out, _ = run(["watch", "--dry-run", "--now", "2026-11-06 10:30", "--price", "395"])
        self.assertIn("$ alerts.py check --now 2026-11-06 10:30", out)
        self.assertIn("$ ticket.py paper tick --now 2026-11-06 10:30 --price 395", out)
        code, out, _ = run(["evening", "--dry-run", "--now", "2026-11-06 16:00", "--note", "quiet day"])
        self.assertIn("$ scan.py orb --now 2026-11-06 15:00 --save trader/replays/orb5m-2026-11-06.json.gz --no-trend", out)
        self.assertIn("$ timemachine.py universe trader/replays/orb5m-2026-11-06.json.gz --compare --others --write", out)
        self.assertIn("$ check.py journal --today 2026-11-06 --note quiet day", out)
        code, _, err = run(["status", "--now", "6 Nov 10:00"])
        self.assertEqual(code, 2)
        self.assertIn("--now wants", err)
        self.assertEqual(run([])[0], 2)

    def test_scripts_exist(self):
        for name in day.SCRIPTS:
            self.assertTrue(os.path.exists(day.script(name)), name)


if __name__ == "__main__":
    unittest.main()

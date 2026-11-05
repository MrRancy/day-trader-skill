"""trader-entry tests. Fixtures in tests/fixtures/trader-entry/:

TATACONSUM-2026-11-03-5m.json  TATACONSUM's Nov 3 2026 five-minute bars (INDstocks, 75 bars,
                               09:15 to 15:25), as a trader bundle.
TATACONSUM-2026-11-03-1m.json  the same session on 1-minute bars (376 bars; the last one is the
                               15:30 closing print, which never counts).
"""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

entry = load("trader-entry", "entry")
td = entry.trader_data

EDIR = os.path.join(FIX, "trader-entry")
BARS5 = os.path.join(EDIR, "TATACONSUM-2026-11-03-5m.json")
BARS1 = os.path.join(EDIR, "TATACONSUM-2026-11-03-1m.json")
MASTER = os.path.join(FIX, "trader_data", "instruments-equity-2026-11-04.csv.gz")
OPEN_TS = 1793677500       # 2026-11-03 09:15 IST
DT = datetime.datetime


def ist(text):
    return entry.parse_now(text)


def doc(path=BARS5):
    with open(path) as fh:
        return json.load(fh)


def chart(d=None, sym="TATACONSUM"):
    return entry.parse_bundle(json.dumps(d if d is not None else doc()), sym)


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), ist(text), **kw)


def shift_to(d, day):
    """The same session moved to another date."""
    d = copy.deepcopy(d)
    delta = td.ts_of(DT.combine(day, datetime.time(9, 15))) - OPEN_TS
    for b in d["bars"]["TATACONSUM"]:
        b["ts"] += delta
    return d


def no_breakout(d=None):
    """Every bar after the range stays under the 1,048.80 high."""
    d = copy.deepcopy(d if d is not None else doc())
    for b in d["bars"]["TATACONSUM"][3:]:
        b["c"] = min(b["c"], 1048.5)
        b["h"] = min(b["h"], 1048.6)
        b["o"] = min(b["o"], 1048.5)
    return d


def synthetic_1m(day="2026-11-03"):
    """SYNTHETIC one-minute bars (not market data): a range of 99.00 to 100.50, a wick above
    the high at 09:32, a close AT the high at 09:33, a missing 09:34 bar, a close above at 09:35
    on thin volume, a close above on heavy volume at 09:38, and the in-progress 09:40 bar."""
    t0 = td.ts_of(DT.strptime(day + " 09:15", "%Y-%m-%d %H:%M"))
    bars = []

    def bar(i, o, h, l, c, v=10000):
        bars.append({"ts": t0 + 60 * i, "o": o, "h": h, "l": l, "c": c, "v": v})
    for i in range(15):
        bar(i, 100.0, 100.5 if i == 7 else 100.2, 99.0 if i == 3 else 99.5, 100.0)
    bar(15, 100.0, 100.3, 99.9, 100.1)
    bar(16, 100.1, 100.4, 100.0, 100.2)
    bar(17, 100.2, 100.9, 100.1, 100.3)           # 09:32: a wick above 100.50
    bar(18, 100.3, 100.5, 100.2, 100.5)           # 09:33: closed AT the high
    bar(20, 100.5, 100.7, 100.4, 100.62, 5000)    # 09:35: closed above, thin volume (09:34 is missing)
    bar(21, 100.62, 100.8, 100.5, 100.7, 8000)
    bar(22, 100.7, 100.9, 100.6, 100.8, 9000)
    bar(23, 100.8, 101.0, 100.7, 100.9, 30000)    # 09:38: closed above on 3x the range's volume
    bar(24, 100.9, 101.1, 100.8, 101.0, 12000)
    bar(25, 101.0, 101.2, 100.9, 101.1, 12000)    # 09:40: in progress at 09:40:30
    return {"interval": "1minute", "bars": {"DEMO": bars}}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheMorning(unittest.TestCase):
    def test_triggers_at_1000_on_the_0955_bar(self):
        r = at("2026-11-03 10:00")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual(r["opening_range"], {"high": 1048.8, "low": 1030.0, "minutes": 15, "bars": 3, "of": 3,
                                              "complete": True, "avg_volume": 44449})
        t = r["trigger"]
        self.assertEqual(t["time"], "10:00")
        self.assertEqual(t["bar_start"], "09:55")
        self.assertEqual(t["bar_index"], 8)
        self.assertEqual(t["price"], 1050.2)
        self.assertEqual(t["volume"], 91405)
        self.assertEqual(t["volume_x"], 2.06)
        self.assertEqual(t["pct_over"], 0.13)
        self.assertEqual(r["level"], 1048.8)
        self.assertEqual(r["minutes_since"], 0)

    def test_at_0957_the_0955_bar_is_still_forming(self):
        r = at("2026-11-03 09:57")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual(r["last"]["bar_start"], "09:50")
        self.assertEqual(r["last"]["close"], 1046.7)
        self.assertEqual(r["forming"], {"bar_start": "09:55", "closes": "10:00", "price": 1050.2})
        self.assertIn("Rs 2.10 under the range high Rs 1048.80", r["reason"])
        self.assertEqual(at("2026-11-03 09:59:59")["status"], "waiting")

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-11-03 09:05")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("opens at 09:15", r["reason"])
        r = at("2026-11-03 09:29")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-11-03 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_read_as_of_the_file_it_is_after_the_close(self):
        c = chart()
        r = entry.evaluate(c, entry.as_of(c, "5m"))
        self.assertEqual(r["now"], "2026-11-03 15:30:00")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["bars_closed"], 75)
        self.assertEqual(r["trigger"]["time"], "10:00")
        self.assertEqual(r["minutes_since"], 330)
        text = "\n".join(entry.describe(r))
        self.assertIn("330 minutes ago; the last close is Rs 1073.00 (15:25 bar)", text)
        self.assertNotIn("This is the moment", text)


class Rules(unittest.TestCase):
    def test_volume_confirmation(self):
        r = at("2026-11-03 10:00", min_volume_x=1)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["volume_x"]), ("10:00", 2.06))   # 2.06x passes 1x
        r = at("2026-11-03 10:00", min_volume_x=3)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("on 2.06x the range's volume, under your 3.00x", r["reason"])

    def test_level_setup(self):
        r = at("2026-11-03 15:30", setup="level", level=1052.0)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]), ("10:25", 13, 1052.6))
        # a level under the open: the first eligible close is the 09:25 bar's, at 09:30 (no_entries_before)
        r = at("2026-11-03 15:30", setup="level", level=1040.0)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]), ("09:30", 2, 1047.8))
        with self.assertRaises(entry.EntryError):
            at("2026-11-03 10:30", setup="level")

    def test_no_breakout_waits_then_expires_at_the_cutoff(self):
        d = no_breakout()
        r = at("2026-11-03 10:49", d)
        self.assertEqual(r["status"], "waiting")
        r = at("2026-11-03 14:59", d)
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["last"]["bar_start"], "14:50")
        r = at("2026-11-03 15:00", d)
        self.assertEqual((r["status"], r["phase"]), ("expired", "after-cutoff"))
        self.assertIn("by 15:00", r["reason"])
        self.assertIn("squared off from about 15:20", r["reason"])
        self.assertEqual(at("2026-11-03 15:31", d)["phase"], "after-close")

    def test_config_cutoff(self):
        cfg = {"no_entries_before": "09:30", "no_entries_after": "09:55"}
        r = at("2026-11-03 10:00", cfg=cfg)
        self.assertEqual(r["status"], "expired")
        cfg = {"no_entries_before": "10:30", "no_entries_after": "15:00"}
        r = at("2026-11-03 15:30", cfg=cfg)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]), ("10:30", 14, 1051.9))
        cfg = {"no_entries_before": "09:30", "no_entries_after": "16:00"}   # past the close: the close wins
        self.assertEqual(at("2026-11-03 15:30", cfg=cfg)["no_entries_after"], "15:30")

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-11-03 10:00", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-11-03 10:00", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        self.assertEqual(entry.parse_now("2026-11-03 10:00"), DT(2026, 11, 3, 10, 0))
        self.assertEqual(entry.parse_now("2026-11-03T04:30:00Z"), DT(2026, 11, 3, 10, 0))
        self.assertEqual(entry.parse_now("2026-11-03 10:00 IST"), DT(2026, 11, 3, 10, 0))
        self.assertEqual(entry.parse_now("10:00").time(), datetime.time(10, 0))

    def test_or_minutes_30(self):
        r = at("2026-11-03 15:30", or_minutes=30)
        self.assertEqual(r["opening_range"]["bars"], 6)
        self.assertEqual(r["opening_range"]["high"], 1052.1)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["price"]), ("10:25", 1052.6))


class OneMinute(unittest.TestCase):
    def test_the_real_morning_on_one_minute_bars(self):
        with open(BARS1, encoding="utf-8") as fh:
            c = entry.parse_bundle(fh.read(), "TATACONSUM")
        self.assertEqual(len(c["bars"]), 376)
        self.assertEqual(entry.ist_of(c["bars"][-1]["t"]), DT(2026, 11, 3, 15, 30))   # the closing print
        r = entry.evaluate(c, ist("2026-11-03 10:30"), interval="1m")
        self.assertEqual(r["opening_range"], {"high": 1048.8, "low": 1030.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 8890})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"], t["volume_x"]),
                         ("09:31", "09:30", 15, 1051.3, 4.66))
        self.assertEqual(r["bars_closed"], 75)
        r = entry.evaluate(c, ist("2026-11-03 09:30:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"], {"bar_start": "09:30", "closes": "09:31", "price": 1051.3})
        self.assertEqual(r["last"]["close"], 1047.8)
        r = entry.evaluate(c, entry.as_of(c, "1m"), interval="1m")
        self.assertEqual((r["now"], r["bars_closed"], r["phase"]), ("2026-11-03 15:31:00", 375, "after-close"))

    def test_wick_close_at_the_high_and_a_missing_bar(self):
        c = entry.parse_bundle(json.dumps(synthetic_1m()), "DEMO")
        r = entry.evaluate(c, ist("2026-11-03 09:40:30"), interval="1m")
        self.assertEqual(r["opening_range"], {"high": 100.5, "low": 99.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 10000})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("09:36", "09:35", 20, 100.62))
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(19, slots)                               # 09:34 is missing
        self.assertEqual(slots[-1], 24)                           # 09:40 still forming
        self.assertEqual(r["forming"]["bar_start"], "09:40")
        r = entry.evaluate(c, ist("2026-11-03 09:34:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["last"]["close"], 100.5)              # the 09:33 bar closed AT the high
        r = entry.evaluate(c, ist("2026-11-03 09:40:30"), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["volume_x"]), ("09:39", 23, 3.0))

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "TATACONSUM", "--offline", BARS1])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_no_dst_any_month(self):
        for day in (datetime.date(2026, 12, 7), datetime.date(2026, 6, 1), datetime.date(2026, 3, 9)):
            d = shift_to(doc(), day)
            r = entry.evaluate(chart(d), ist("%s 10:00" % day))
            self.assertEqual(r["status"], "triggered", day)
            self.assertEqual(r["trigger"]["time"], "10:00", day)
            self.assertEqual(r["trigger"]["bar_index"], 8, day)
            self.assertEqual(entry.evaluate(chart(d), ist("%s 09:57" % day))["status"], "waiting", day)

    def test_weekend_holiday_and_empty_data(self):
        r = at("2026-11-07 10:00")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertIn("weekend", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-09 09:15")
        r = at("2026-11-10 10:00")
        self.assertIn("Diwali-Balipratipada", r["reason"])
        self.assertEqual(r["next_open"], "2026-11-11 09:15")
        empty = {"interval": "5minute", "bars": {"TATACONSUM": []}}
        r = at("2026-11-03 10:00", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-11-04 10:00")                                # yesterday's bars only
        self.assertEqual(r["status"], "no-data")
        self.assertFalse(at("2028-03-01 10:30")["calendar_known"])
        self.assertIn("nseindia.com", "\n".join(entry.describe(at("2028-03-01 10:30"))))

    def test_bundle_errors(self):
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_bundle(json.dumps({"bars": {"RELIANCE": []}}), "TATACONSUM")
        self.assertIn("holds RELIANCE, not TATACONSUM", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_bundle("<html>", "TATACONSUM")
        with self.assertRaises(entry.EntryError):
            entry.parse_bundle(json.dumps({"spark": {}}), "TATACONSUM")


class Network(unittest.TestCase):
    """INDstocks is never called: a fake backend answers from the fixture."""

    class Fake(object):
        def __init__(self, bars=None, fail=None):
            self.bars, self.fail, self.calls = bars, fail, []

        def candles(self, sym, interval, start, end):
            self.calls.append((sym, interval, str(start), str(end)))
            if self.fail:
                raise td.DataError(self.fail)
            return self.bars

        def instruments(self, source="equity"):
            return []

    def test_the_request_is_one_day_of_bars(self):
        fake = self.Fake(doc()["bars"]["TATACONSUM"])
        c = entry.get_chart("tataconsum", "5m", datetime.date(2026, 11, 3), backend=fake)
        self.assertEqual(len(c["bars"]), 75)
        self.assertEqual(fake.calls, [("TATACONSUM", "5m", "2026-11-03", "2026-11-04")])

    def test_a_data_error_is_one_plain_line(self):
        fake = self.Fake(fail="INDstocks answered HTTP 401 for /market/historical/5minute: the token is invalid or has expired")
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("TATACONSUM", "5m", datetime.date(2026, 11, 3), backend=fake)
        self.assertIn("expired", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.get_chart("TATACONSUM", "15m", datetime.date(2026, 11, 3), backend=fake)


def live_provider(d, calls=None):
    """Bars the way INDstocks serves them at `now`: closed bars final, the in-progress bar
    showing a spike to 1,049.50 (above the high, then it closes under)."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = td.ts_of(now)
        bars = []
        for b in base["bars"]:
            if b["t"] > nts:
                continue
            b = dict(b)
            if b["t"] + 300 > nts:
                b["c"], b["h"] = 1049.50, max(b["h"], 1049.50)
            bars.append(b)
        return {"symbol": "TATACONSUM", "bars": bars, "as_of": None, "interval": "5m"}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), calls=calls), "2026-11-03 09:52", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:00")
        self.assertEqual(fired[0][0]["trigger"]["price"], 1050.2)
        # it looked at 09:52 (09:50 bar spiking to 1,049.50 in progress) and at 09:55:05 (that bar closed 1,046.70)
        self.assertEqual([c.strftime("%H:%M:%S") for c in calls], ["09:52:00", "09:55:05", "10:00:05"])
        text = "\n".join(lines)
        self.assertIn("09:50 bar  closed Rs 1046.70  Rs 2.10 under Rs 1048.80", text)
        self.assertIn("09:55 bar  closed Rs 1050.20  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_polls_while_the_data_is_behind(self):
        calls = []
        base = live_provider(doc(), calls=calls)

        def lagging(now):                      # the 09:55 bar shows up 40 seconds late
            ch = base(now)
            if DT(2026, 11, 3, 10, 0) <= now < DT(2026, 11, 3, 10, 0, 40):
                ch["bars"] = [b for b in ch["bars"] if entry.ist_of(b["t"]) < DT(2026, 11, 3, 9, 55)]
            return ch
        code, res, lines, fired, clock = self.loop(lagging, "2026-11-03 09:57", every=30)
        self.assertEqual(code, 0)
        self.assertEqual([c.strftime("%H:%M:%S") for c in calls], ["09:57:00", "10:00:05", "10:00:35", "10:01:05"])
        self.assertEqual(fired[0][0]["trigger"]["time"], "10:00")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(doc()), "2026-11-03 09:00", every=30)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("not open yet", lines[0])
        self.assertIn("09:25 bar  closed Rs 1047.80  opening range set: high Rs 1048.80, low Rs 1030.00", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 9)    # 09:15 ... 09:55, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ist("2026-11-03 09:31"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(doc(), calls=calls), clock.now, clock.sleep, lines.append,
                                     dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        times = [c.strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times[:2], ["09:31:00", "09:49:30"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in ("09:30", "09:35", "09:40", "09:45"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "10:00")

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-11-03 14:30", every=300)
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "expired")
        self.assertEqual(fired, [])
        self.assertEqual(clock.now().strftime("%H:%M"), "15:00")
        until = DT(2026, 11, 3, 11, 0)
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-11-03 10:30", every=300,
                                                   until=until)
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-11-07 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])

        def broken(now):
            raise entry.EntryError("INDstocks did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-11-03 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "TATACONSUM", "--offline", BARS5, "--now", "2026-11-03 10:00", "--master", MASTER,
                             "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 10:00 at Rs 1050.20", text)
        self.assertIn("/trader-exit", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            e = json.load(fh)
        want = {"created_by": "trader-entry", "symbol": "TATACONSUM", "setup": "orb", "side": "buy", "date": "2026-11-03",
                "status": "triggered", "time": "10:00", "bar_start": "09:55", "bar_index": 8, "price": 1050.2,
                "level": 1048.8, "interval": "5m", "currency": "INR", "tick": 0.1,
                "opening_range": {"high": 1048.8, "low": 1030.0, "minutes": 15},
                "source": "indstocks", "checked_at": "2026-11-03T10:00:00+05:30"}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)
        # without the master, offline, the tick comes from the price band
        code, text, _ = run(["check", "TATACONSUM", "--offline", BARS5, "--now", "2026-11-03 10:00", "--write", "e.json"])
        with open("e.json") as fh:
            self.assertEqual(json.load(fh)["tick"], 0.1)

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "TATACONSUM", "--offline", BARS5, "--now", "2026-11-03 09:57", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)
        code, text, _ = run(["check", "TATACONSUM", "--offline", BARS5, "--now", "2026-11-03 09:57", "--write", "e.json"])
        with open("e.json") as fh:
            self.assertEqual(json.load(fh)["status"], "waiting")
        self.assertIn("still forming", text)

    def test_offline_gz_folder_and_mismatch(self):
        os.mkdir("charts")
        with open(BARS5, "rb") as src, gzip.open(os.path.join("charts", "TATACONSUM.json.gz"), "wb") as dst:
            dst.write(src.read())
        code, text, _ = run(["check", "tataconsum", "--offline", "charts", "--now", "2026-11-03 10:00"])
        self.assertEqual(code, 0)
        code, _, err = run(["check", "RELIANCE", "--offline", BARS5])
        self.assertEqual(code, 1)
        self.assertIn("holds TATACONSUM, not RELIANCE", err)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "TATACONSUM", "--offline", BARS5, "--side", "short"])
        self.assertEqual(code, 1)
        self.assertIn("v2 plans longs only", err)
        code, _, err = run(["check", "TATACONSUM", "--offline", BARS5, "--level", "300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "TATACONSUM", "--offline", BARS5, "--setup", "level"])
        self.assertIn("needs --level", err)

    def test_config_is_read(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "09:55"}, fh)
        code, text, _ = run(["check", "TATACONSUM", "--offline", BARS5, "--now", "2026-11-03 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "TATACONSUM", "--offline", BARS5])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "TATACONSUM", "--offline", BARS5, "--now", "2026-11-03 09:35", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn("09:55 bar  closed Rs 1050.20  TRIGGERED", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            self.assertEqual(json.load(fh)["bar_index"], 8)
        code, text, _ = run(["watch", "TATACONSUM", "--offline", BARS5, "--now", "2026-11-03 09:35", "--until", "09:50",
                             "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist(self):
        os.mkdir("charts")
        shutil.copy(BARS5, os.path.join("charts", "TATACONSUM.json"))
        with open(os.path.join("charts", "DEMO.json"), "w") as fh:
            json.dump(synthetic_1m(), fh)
        os.mkdir("trader")
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "trader-scanner", "hits": [
                {"symbol": "TATACONSUM", "setups": ["orb"], "or_high": 1048.8, "or_low": 1030.0},
                {"symbol": "ZZZZ", "setups": ["orb"]}, {"symbol": "DEMO", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-11-03 10:00"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("TATACONSUM    triggered  10:00   1050.20     1048.80"), rows[1])
        self.assertIn("ZZZZ          error: no offline file", rows[2])
        self.assertIn("DEMO          error: DEMO has 1m bars", rows[3])
        self.assertIn("1 of 3 triggered", text)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-11-03 09:57", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)[0]["result"]["status"], "waiting")
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /trader-scanner first", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-exit", "SKILL.md"), encoding="utf-8") as fh:
            exit_md = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], exit_md[exit_md.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("bar by bar", "can lag", "day trading loses most people money", "/trader-exit", "longs only",
                     "in the background", "/loop", "nothing was placed", "10:00", "rs 1050.20", "bar 8", "09:15", "15:20"):
            self.assertIn(must, low)
        for never in ("—", "tick " + "by tick", "real " + "time", "pro" + "fit", "yahoo", "new york"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()

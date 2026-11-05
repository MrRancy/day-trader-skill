import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

scan = load("trader-scanner", "scan")
td = scan.trader_data

SDIR = os.path.join(FIX, "trader-scanner")
MASTER = os.path.join(FIX, "trader_data", "instruments-equity-2026-11-04.csv.gz")  # the real equity master, Nov 4 2026
NSE = os.path.join(SDIR, "nse-2026-11-04")                   # asm.json and gsm.json as nseindia.com served them Nov 4
HOLIDAYS = os.path.join(SDIR, "nse-holidays-2026-11-04.json")  # nseindia.com/api/holiday-master?type=trading, Nov 4
DAILY = os.path.join(SDIR, "daily-2026-11-04.json.gz")       # 174 real daily charts, 100 days to Wed Nov 4 2026
ORB = os.path.join(SDIR, "orb5m-2026-11-03.json.gz")         # 174 real 5-minute charts, Tue Nov 3 2026
DT = datetime.datetime
D = datetime.date
T = datetime.time


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(ist_text):
    return td.ts_of(DT.strptime(ist_text, "%Y-%m-%d %H:%M"))


def bars_of(day, closes, highs=None, lows=None, start="09:15", vol=1000):
    """5-minute bars from `start` on `day` (YYYY-MM-DD); a None close leaves a hole."""
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    out = []
    for i, c in enumerate(closes):
        if c is None:
            continue
        h = highs[i] if highs else c + 0.1
        lo = lows[i] if lows else c - 0.1
        out.append({"ts": td.ts_of(t0 + datetime.timedelta(minutes=5 * i)), "o": c, "h": h, "l": lo, "c": c, "v": vol})
    return out


def daily_of(closes, end="2026-11-04", vol=1000000):
    """Daily bars ending on `end`, weekdays only, oldest first."""
    d = D.fromisoformat(end)
    days = []
    while len(days) < len(closes):
        if d.weekday() < 5 and scan.trading_day(d)[0]:
            days.append(d)
        d -= datetime.timedelta(days=1)
    days.reverse()
    return [{"ts": td.ts_of(DT.combine(dd, T(5, 30))), "o": c, "h": c + 1, "l": c - 1, "c": c, "v": vol}
            for dd, c in zip(days, closes)]


class Calendar(unittest.TestCase):
    def test_holidays_match_nse(self):
        with open(HOLIDAYS, encoding="utf-8") as fh:
            published = scan.holidays_from_report(json.load(fh))
        self.assertEqual(published, scan.NSE_HOLIDAYS)
        self.assertEqual(len(scan.NSE_HOLIDAYS), 20)
        self.assertEqual(scan.NSE_HOLIDAYS["2026-11-10"], "Diwali-Balipratipada")
        self.assertIn("2026-11-08", scan.MUHURAT)

    def test_trading_days(self):
        self.assertEqual(scan.trading_day(D(2026, 11, 3)), (True, ""))
        self.assertEqual(scan.trading_day(D(2026, 11, 7)), (False, "weekend"))
        self.assertEqual(scan.trading_day(D(2026, 11, 10)), (False, "NSE holiday: Diwali-Balipratipada"))
        self.assertEqual(scan.trading_day(D(2027, 1, 4)), (True, "no NSE holiday calendar for 2027 yet"))
        self.assertEqual(scan.session(D(2026, 11, 3)), (T(9, 15), T(15, 30)))
        self.assertIsNone(scan.session(D(2026, 12, 25)))

    def test_clock(self):
        self.assertEqual(scan.parse_now("2026-11-03 10:00"), DT(2026, 11, 3, 10, 0))
        self.assertEqual(scan.parse_now("2026-11-03T04:30:00Z"), DT(2026, 11, 3, 10, 0))
        self.assertEqual(scan.parse_now("2026-11-03T10:00:00+05:30"), DT(2026, 11, 3, 10, 0))
        self.assertEqual(scan.ist_of(1793677500), DT(2026, 11, 3, 9, 15))   # RELIANCE's first bar
        self.assertEqual(scan.clock_label(DT(2026, 11, 3, 9, 35)), "09:35")
        self.assertEqual(scan.clock_label(T(15, 30)), "15:30")
        self.assertEqual(scan.day_label(D(2026, 11, 3)), "Tue 2026-11-03")
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")


class Universe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = td.parse_instruments(td.read_text(MASTER))
        with open(os.path.join(NSE, "asm.json"), encoding="utf-8") as fh:
            asm = json.load(fh)
        with open(os.path.join(NSE, "gsm.json"), encoding="utf-8") as fh:
            gsm = json.load(fh)
        cls.surv = scan.parse_surveillance(asm, gsm)
        cls.kept, cls.counts = scan.build_universe(cls.rows, cls.surv)
        cls.by = {}
        for r in cls.rows:
            cls.by[(r["EXCH"], r["TRADING_SYMBOL"], r["SERIES"])] = r

    def why(self, exch, sym, series="EQ"):
        return scan.classify(self.by[(exch, sym, series)], self.surv)

    def test_the_lists_as_read(self):
        self.assertEqual((len(self.surv["asm"]), len(self.surv["gsm"])), (190, 76))
        self.assertEqual(self.surv["asm"]["A2ZINFRA"], "LT-ASM Stage I")
        self.assertEqual(self.surv["gsm"]["ASIANTNE"], "GSM stage 0")
        with self.assertRaises(scan.ScanError):
            scan.parse_surveillance("<html>", None)
        self.assertEqual(scan.parse_surveillance(None, None), {"asm": {}, "gsm": {}})

    def test_the_kept_list(self):
        self.assertEqual(len(self.kept), UNIVERSE_COUNT)
        self.assertEqual(self.counts["rows"], 23044)
        self.assertEqual(self.counts["keep"], UNIVERSE_COUNT)
        self.assertEqual(self.counts["not NSE"], 12995)
        self.assertEqual(self.counts["SME (SM/ST)"], 581)
        self.assertEqual(self.counts["trade-for-trade (BE/BZ)"], 235)
        self.assertEqual(self.counts["debt, ETF or other series"], 6460)
        self.assertEqual(self.counts["not equity shares"], 381)      # 359 INF fund units + 22 DUMMY test symbols
        self.assertEqual((self.counts["ASM"], self.counts["GSM"]), (131, 28))
        self.assertEqual(self.kept[0], ("20MICRONS", "20 Microns Ltd"))
        self.assertEqual(self.kept[-1], ("ZYDUSWELL", "Zydus Wellness Ltd"))   # typed "Other", ISIN INE
        self.assertEqual(dict(self.kept)["RELIANCE"], "Reliance Industries Ltd")
        syms = [s for s, _ in self.kept]
        self.assertEqual(syms, sorted(set(syms)))

    def test_one_case_per_rule(self):
        self.assertEqual(self.why("NSE", "RELIANCE"), "keep")
        self.assertEqual(self.why("BSE", "RELIANCE", "A"), "not NSE")                 # 1
        self.assertEqual(self.why("NSE", "BILVYAPAR", "BE"), "trade-for-trade (BE/BZ)")   # 2
        self.assertEqual(self.why("NSE", "BEACON", "SM"), "SME (SM/ST)")              # 2
        self.assertEqual(self.why("NSE", "718TN55", "SG"), "debt, ETF or other series")  # 2 a state bond
        self.assertEqual(self.why("NSE", "AUTOIETF"), "not equity shares")            # 3 an ETF in the EQ series
        self.assertEqual(self.why("NSE", "ELIQUID"), "not equity shares")             # 3 a liquid ETF typed Other
        self.assertEqual(self.why("NSE", "ROLEXRINGS"), "keep")                      # 3 typed Other, but an INE share
        self.assertEqual(self.why("NSE", "JISLDVREQS"), "keep")                      # 3 a DVR share, ISIN IN9...
        self.assertEqual(self.why("NSE", "071NSETEST"), "not equity shares")          # 3 an NSE test symbol
        self.assertEqual(self.why("NSE", "A2ZINFRA"), "ASM")                          # 4
        self.assertEqual(self.why("NSE", "ASIANTNE"), "GSM")                          # 4
        self.assertEqual(scan.classify({"EXCH": "NSE", "INSTRUMENT_NAME": "FUTSTK", "SERIES": "EQ"}), "not equity")

    def test_bands(self):
        bands = scan.band_table(self.rows)
        self.assertEqual(bands["RELIANCE"], (1177.2, 1438.6))
        self.assertNotIn("718TN55", bands)
        self.assertTrue(scan.at_band({"h": 10, "l": 10, "c": 10}, None))
        self.assertTrue(scan.at_band({"h": 11, "l": 10, "c": 11}, (9, 11)))
        self.assertTrue(scan.at_band({"h": 11, "l": 9, "c": 9}, (9, 11)))
        self.assertFalse(scan.at_band({"h": 11, "l": 9, "c": 10}, (9, 11)))
        self.assertFalse(scan.at_band({"h": 11, "l": 9, "c": 11}, None))


class Daily(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.failed, cls.interval = scan.load_offline(DAILY)
        cls.syms = sorted(cls.data)
        cls.now = scan.parse_now("2026-11-04 18:00")

    def test_the_fixture(self):
        self.assertEqual((len(self.syms), self.failed, self.interval), (174, {}, "1day"))
        r = self.data["RELIANCE"]
        self.assertEqual(len(r), 70)
        self.assertEqual(scan.ist_of(r[-1]["ts"]), DT(2026, 11, 4, 5, 30))   # daily bars are stamped midnight UTC
        self.assertEqual(r[-1]["c"], 1311.0)

    def test_setups_after_the_close(self):
        hits, counts, last = scan.scan_daily(self.data, self.syms, self.now, 50, 500000)
        self.assertEqual(last, D(2026, 11, 4))
        self.assertEqual(counts, {"requested": 174, "fetched": 174, "illiquid": 25, "short": 0, "circuit": 0,
                                  "scanned": 149, "hits": 108})       # 82 price setups + the indicator ones
        per = dict((k, sum(1 for h in hits if k in h["setups"])) for k, _ in scan.SETUPS)
        self.assertEqual(per, {"breakout_20d": 57, "bull_flag": 2, "reclaim_ma20": 22, "oversold": 1,
                               "oversold_bounce": 2, "ema_cross": 34, "supertrend_flip": 22, "volume_spike": 18,
                               "bb_squeeze": 15, "bb_release": 7})
        by = dict((h["symbol"], h) for h in hits)
        # RELIANCE closed 1,311.00 on Nov 4 against 1,307.90, the best close of the 20 sessions before
        rel = by["RELIANCE"]
        self.assertEqual(dict((k, rel[k]) for k in ("symbol", "setups", "price", "close_date", "rsi", "volume")),
                         {"symbol": "RELIANCE", "setups": ["breakout_20d", "ema_cross"], "price": 1311.0,
                          "close_date": "2026-11-04", "rsi": 67.1, "volume": 19451679})
        self.assertEqual((rel["score"], rel["ema20"], rel["ema50"]), (5, 1221.64, 1240.17))
        self.assertEqual(by["SWIGGY"]["setups"], ["oversold", "oversold_bounce"])
        self.assertEqual(by["SWIGGY"]["rsi"], 26.6)
        self.assertEqual(by["POLICYBZR"]["setups"], ["oversold_bounce"])
        self.assertEqual(sorted(h["symbol"] for h in hits if "bull_flag" in h["setups"]), ["INDHOTEL", "TECHM"])
        self.assertEqual([h["symbol"] for h in hits[:3]], ["HDFCBANK", "RELIANCE", "BHARTIARTL"])  # rupees traded

    def test_the_in_progress_bar_is_dropped(self):
        _, _, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-04 12:00"), 0, 0)
        self.assertEqual(last, D(2026, 11, 3))
        _, _, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-04 15:30"), 0, 0)
        self.assertEqual(last, D(2026, 11, 4))
        bars = scan.daily_bars(self.data["RELIANCE"], scan.parse_now("2026-11-04 15:29"))
        self.assertEqual(bars[-1][0], D(2026, 11, 3))
        # a holiday: the clock on Diwali-Balipratipada reads through the last session before it
        bars = scan.daily_bars(self.data["RELIANCE"], scan.parse_now("2026-11-10 12:00"))
        self.assertEqual(bars[-1][0], D(2026, 11, 4))

    def test_liquidity_filter(self):
        hits, counts, _ = scan.scan_daily(self.data, self.syms, self.now, 0, 0)
        self.assertEqual((counts["illiquid"], counts["scanned"], counts["hits"]), (0, 174, 124))
        hits, counts, _ = scan.scan_daily(self.data, ["BOSCHLTD", "IDEA", "ABB"], self.now, 50, 500000)
        self.assertEqual(counts["illiquid"], 3)        # BOSCHLTD and ABB under 500,000 shares, IDEA under INR 50

    def test_circuit_locked_and_short_charts(self):
        closes = [100.0 + i * 0.5 for i in range(40)]
        ok = daily_of(closes)
        locked = daily_of(closes)
        locked[-1]["h"] = locked[-1]["l"] = locked[-1]["c"]   # locked all day
        banded = daily_of(closes)                               # closed on today's upper band
        bands = {"B": (100.0, banded[-1]["c"])}
        short = daily_of(closes[:20])
        data = {"OK": ok, "L": locked, "B": banded, "S": short, "E": []}
        hits, counts, last = scan.scan_daily(data, ["OK", "L", "B", "S", "E", "GONE"], self.now, 0, 0, bands)
        self.assertEqual([h["symbol"] for h in hits], ["OK"])
        # an empty chart ("E") is what fetch_bars reports as failed, so it is not fetched
        self.assertEqual(counts, {"requested": 6, "fetched": 4, "illiquid": 0, "short": 1, "circuit": 2,
                                  "scanned": 1, "hits": 1})
        # the band only applies to today's bar: yesterday's close on today's band is not a lock
        _, counts, _ = scan.scan_daily({"B": banded}, ["B"], scan.parse_now("2026-11-05 09:00"), 0, 0, bands)
        self.assertEqual(counts["circuit"], 0)

    def test_rsi_matches_the_textbook(self):
        self.assertIsNone(scan.rsi([1.0] * 10))
        self.assertEqual(scan.rsi([float(x) for x in range(1, 40)]), 100.0)
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)  # Wilder's example


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.failed, cls.interval = scan.load_offline(ORB)
        cls.syms = sorted(cls.data)

    def scan_at(self, at, min_price=50, syms=None, or_minutes=15, until=T(15, 30)):
        day, cutoff = scan.orb_window(scan.parse_now("2026-11-03 16:00"), scan.parse_hhmm(at), or_minutes, until)
        return scan.scan_orb(self.data, syms or self.syms, day, cutoff, or_minutes, min_price, 0)

    def test_the_fixture(self):
        self.assertEqual((len(self.syms), self.failed, self.interval), (174, {}, "5minute"))
        self.assertTrue(all(len(b) == 75 for b in self.data.values()))   # 09:15 to 15:25, every name

    def test_the_ten_oclock_scan(self):
        hits, counts = self.scan_at("10:00")
        self.assertEqual(counts, {"requested": 174, "fetched": 174, "unusable": 0, "illiquid": 3, "circuit": 0,
                                  "scanned": 171, "hits": 48, "locked": []})
        self.assertEqual(hits[0], {"symbol": "ADANIENSOL", "setups": ["orb"], "price": 1395.2, "at": "09:35",
                                   "bar_start": "09:30", "bar_index": 3, "or_high": 1389.9, "or_low": 1366.8,
                                   "pct_over": 0.38})
        self.assertEqual([h["at"] for h in hits], sorted(h["at"] for h in hits))   # by time, then symbol
        self.assertEqual(sum(1 for h in hits if h["at"] == "09:35"), 15)
        self.assertEqual(hits[-1]["symbol"], "TATACONSUM")
        self.assertNotIn("RELIANCE", [h["symbol"] for h in hits])

    def test_price_floor(self):
        hits, counts = self.scan_at("10:00", min_price=0)
        self.assertEqual((counts["illiquid"], counts["scanned"], len(hits)), (0, 174, 48))
        hits, counts = self.scan_at("10:00", syms=["IDEA", "SUZLON", "YESBANK", "RELIANCE"])
        self.assertEqual(counts["illiquid"], 3)         # 12.26, 35.75 and 15.38: under INR 50

    def test_reliance_broke_out_on_the_last_bar(self):
        # range 09:15 to 09:25: high 1,307.00 (the 09:15 bar), low 1,282.50; the 15:25 bar closed 1,307.90
        hits, _ = self.scan_at("15:00", syms=["RELIANCE"])
        self.assertEqual(hits, [])
        hits, _ = self.scan_at("15:30", syms=["RELIANCE"])
        self.assertEqual(hits, [{"symbol": "RELIANCE", "setups": ["orb"], "price": 1307.9, "at": "15:30",
                                 "bar_start": "15:25", "bar_index": 74, "or_high": 1307.0, "or_low": 1282.5,
                                 "pct_over": 0.07}])
        hits, counts = self.scan_at("15:30")
        self.assertEqual(len(hits), 96)
        hits, counts = self.scan_at("09:35")
        self.assertEqual(len(hits), 15)

    def test_in_progress_bar(self):
        day = D(2026, 11, 3)
        self.assertEqual(len(scan.session_bars(self.data["RELIANCE"], day, DT(2026, 11, 3, 10, 0))), 9)
        self.assertEqual(len(scan.session_bars(self.data["RELIANCE"], day, DT(2026, 11, 3, 9, 59))), 8)
        bars = scan.session_bars(self.data["RELIANCE"], day, DT(2026, 11, 3, 16, 0))
        self.assertEqual(len(bars), 75)
        self.assertTrue(scan.usable(bars, day, DT(2026, 11, 3, 16, 0), 3))
        self.assertFalse(scan.usable(bars[:-1], day, DT(2026, 11, 3, 16, 0), 3))

    def test_a_missing_bar_makes_a_chart_unusable(self):
        bars = bars_of("2026-11-03", [10, 10.2, 10.1, None, 10.5, 10.6, 10.7, 10.8, 10.9])
        got = scan.session_bars(bars, D(2026, 11, 3), DT(2026, 11, 3, 10, 0))
        self.assertEqual(len(got), 8)
        self.assertFalse(scan.usable(got, D(2026, 11, 3), DT(2026, 11, 3, 10, 0), 3))
        off = [dict(b, ts=b["ts"] + 60) for b in bars_of("2026-11-03", [10, 10.2, 10.1, 10.3])]
        self.assertEqual(scan.session_bars(off, D(2026, 11, 3), DT(2026, 11, 3, 10, 0)), [])   # off the grid
        pre = bars_of("2026-11-03", [9, 9.1], start="09:05")
        self.assertEqual(scan.session_bars(pre, D(2026, 11, 3), DT(2026, 11, 3, 10, 0)), [])   # pre-open

    def test_locked_at_the_upper_circuit(self):
        closes = [100, 101, 100.5, 102, 103, 104]
        bars = bars_of("2026-11-03", closes, highs=[c + 0.2 for c in closes])
        day, cut = D(2026, 11, 3), DT(2026, 11, 3, 9, 45)
        hits, counts = scan.scan_orb({"X": bars}, ["X"], day, cut, 15, 0, 0)
        self.assertEqual((hits[0]["at"], hits[0]["price"], hits[0]["or_high"]), ("09:35", 102, 101.2))
        hits, counts = scan.scan_orb({"X": bars}, ["X"], day, cut, 15, 0, 0, bands={"X": (90.0, 102.0)})
        self.assertEqual(hits, [])
        self.assertEqual((counts["circuit"], counts["locked"], counts["scanned"]), (1, ["X"], 1))
        flat = bars_of("2026-11-03", [100] * 6, highs=[100] * 6, lows=[100] * 6)
        hits, counts = scan.scan_orb({"F": flat}, ["F"], day, cut, 15, 0, 0)
        self.assertEqual((hits, counts["circuit"], counts["scanned"]), ([], 1, 0))

    def test_a_30_minute_range(self):
        closes = [100, 101, 100.5, 100.8, 101.2, 100.9, 101.5] + [101.0] * 40
        bars = bars_of("2026-11-03", closes, highs=[c + 0.2 for c in closes])
        hits, counts = scan.scan_orb({"X": bars}, ["X"], D(2026, 11, 3), DT(2026, 11, 3, 12, 0), 30, 0, 0)
        self.assertEqual(hits[0]["at"], "09:50")           # range 09:15 to 09:45 (high 101.4), 09:45 bar 101.5
        self.assertEqual(hits[0]["or_high"], 101.4)

    def test_window_messages(self):
        until = T(15, 0)

        def msg(now, at=None, until=until):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until)
            return str(cm.exception)
        self.assertIn("weekend", msg("2026-11-07 10:00"))
        self.assertIn("Diwali-Balipratipada", msg("2026-11-10 10:00"))
        self.assertIn("Muhurat", msg("2026-11-08 18:30"))
        self.assertIn("Run orb again after 09:35", msg("2026-11-03 09:20"))
        self.assertIn("Run orb again after 09:35", msg("2026-11-03 09:34"))
        self.assertIn("expired", msg("2026-11-03 15:10"))
        self.assertIn("expired", msg("2026-11-03 11:30", until=T(11, 0)))
        self.assertIn("closed at 15:30", msg("2026-11-03 15:45", until=T(15, 30)))
        self.assertIn("later than now", msg("2026-11-03 10:00", at=T(10, 15)))
        self.assertIn("after the 15:30 close", msg("2026-11-03 17:00", at=T(16, 0)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-11-03 17:00"), T(10, 0), 15, until),
                         (D(2026, 11, 3), DT(2026, 11, 3, 10, 0)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-11-03 09:35"), None, 15, until)[1],
                         DT(2026, 11, 3, 9, 35))
        with self.assertRaises(scan.ScanError):
            scan.orb_window(scan.parse_now("2026-11-03 10:00"), None, 7, until)


class Fetch(unittest.TestCase):
    """The networked path without the network: a backend that answers from the bundle."""

    class Fake(object):
        def __init__(self, data, master, fail=()):
            self.data, self.calls, self.fail = data, [], set(fail)
            self.rows = master
            self._codes = scan.trader_data.code_table(master)

        def resolve(self, s):
            if ("NSE", s) not in self._codes:
                raise td.DataError("%s is not in the INDstocks NSE equity master" % s)
            return self._codes[("NSE", s)]

        def candles_many(self, syms, interval, start, end):
            self.calls.append((list(syms), interval, str(start), str(end)))
            if self.fail & set(syms):
                raise td.DataError("INDstocks answered HTTP 500 for /market/historical/5minute: nope")
            return dict((s, self.data.get(s, [])) for s in syms)

        def instruments(self, source="equity"):
            return self.rows

    @classmethod
    def setUpClass(cls):
        cls.data, _, _ = scan.load_offline(ORB)
        cls.rows = td.parse_instruments(td.read_text(MASTER))

    def test_five_a_request_and_partial_failures(self):
        fake = self.Fake(self.data, self.rows, fail=["HAL"])
        syms = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "HAL", "NOSUCH", "ITC"]
        data, failed = scan.fetch_bars(syms, "5m", D(2026, 11, 3), D(2026, 11, 4), backend=fake)
        self.assertEqual([len(c[0]) for c in fake.calls], [5, 3])     # NOSUCH never reached the API
        self.assertEqual(sorted(data), ["HDFCBANK", "ICICIBANK", "INFY", "RELIANCE", "TCS"])
        self.assertIn("NOSUCH is not in", failed["NOSUCH"])
        self.assertIn("HTTP 500", failed["HAL"])
        self.assertIn("HTTP 500", failed["ITC"])                      # the whole batch failed with it
        self.assertEqual(len(failed), 4)

    def test_a_dead_token_stops_the_scan(self):
        class Dead(self.Fake):
            def candles_many(self, syms, interval, start, end):
                raise td.DataError("INDstocks answered HTTP 401 for /market/historical/5minute: the token is "
                                   "invalid or has expired")
        with self.assertRaises(scan.ScanError) as cm:
            scan.fetch_bars(["RELIANCE"], "5m", D(2026, 11, 3), D(2026, 11, 4), backend=Dead(self.data, self.rows))
        self.assertIn("expired", str(cm.exception))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "trader")
        self.wl = os.path.join(self.t, "watchlist.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_daily_orb_end_to_end(self):
        uni = os.path.join(self.t, "universe.txt")
        code, text, err = run(["universe", "--master", MASTER, "--offline", NSE, "--out", uni,
                               "--now", "2026-11-04 18:00"])
        self.assertEqual(code, 0, err)
        self.assertIn("Kept %s NSE EQ-series equity shares." % "{:,}".format(UNIVERSE_COUNT), text)
        self.assertIn("190 ASM, 76 GSM", text)
        with open(uni, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), UNIVERSE_COUNT)
        self.assertIn("RELIANCE|Reliance Industries Ltd", lines)
        surv = self.read(os.path.join(self.t, "surveillance.json"))
        self.assertEqual((surv["as_of"], len(surv["asm"]), len(surv["gsm"])), ("2026-11-04 18:00 IST", 190, 76))

        # daily over that universe with the fixture charts: 174 of them are in both, so partial
        code, text, err = run(["daily", "--universe", uni, "--offline", DAILY, "--now", "2026-11-04 18:00",
                               "--out", self.wl])
        self.assertEqual(code, 0, err)
        self.assertIn("Fetched 174 of %s charts (%s failed or had no data)"
                      % ("{:,}".format(UNIVERSE_COUNT), "{:,}".format(UNIVERSE_COUNT - 174)), text)
        self.assertIn("108 charts matched a setup: 57 20-day closing-high breakout, 2 bull flag, "
                      "22 reclaimed the 20-day average, 1 oversold (RSI under 30), 2 oversold bounce, "
                      "34 EMA 10 crossed over EMA 20, 22 Supertrend turned up, 18 volume over 2x its 20-day "
                      "average, 15 Bollinger squeeze (width at a 20-bar low), 7 Bollinger squeeze released.", text)
        self.assertIn("  RELIANCE       1,311.00  RSI  67.1  score 5/6  20-day closing-high breakout", text)
        doc = self.read(self.wl)
        self.assertEqual((doc["mode"], doc["universe"], doc["scanned"], doc["failed"], doc["as_of"], doc["source"]),
                         ("daily", UNIVERSE_COUNT, 149, UNIVERSE_COUNT - 174, "2026-11-04 close", "indstocks"))
        self.assertEqual(doc["created_at"], "2026-11-04T18:00:00+05:30")
        self.assertEqual(doc["filters"], {"min_price_inr": 50.0, "min_volume": 500000})

        # the 10:00 scan of Nov 3
        code, text, err = run(["orb", "--offline", ORB, "--now", "2026-11-03 10:00", "--out", self.wl])
        self.assertEqual(code, 0, err)
        self.assertIn("Opening-range breakout scan, Tue 2026-11-03 10:00 IST: 15-minute range (09:15 to 09:30)", text)
        self.assertIn("Fetched 174 of 174 charts; 171 usable (3 under the price / volume floor).", text)
        self.assertIn("48 of 171 charts closed above their opening-range high by 10:00:", text)
        self.assertIn("  09:35  ADANIENSOL     1,395.20  range 1,366.80 to 1,389.90  +0.38% over", text)
        self.assertIn("not a list of stocks to buy", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("mode", "as_of", "universe", "scanned", "failed", "circuit", "locked")},
                         {"mode": "orb", "as_of": "2026-11-03 10:00 IST", "universe": 174, "scanned": 171,
                          "failed": 0, "circuit": 0, "locked": []})
        self.assertEqual(len(doc["hits"]), 48)

        # the hits read back as a watchlist, with --at instead of --now
        code, text, _ = run(["orb", "--offline", ORB, "--at", "10:00", "--watchlist", self.wl, "--json", "--no-write"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], len(out["hits"])), (48, 48, 48))

    def test_plain_messages_and_exit_codes(self):
        code, _, err = run(["orb", "--offline", ORB, "--now", "2026-11-07 10:00", "--out", self.wl])
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-11-07 (weekend)", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(os.path.exists(self.wl))
        code, _, err = run(["orb", "--offline", ORB, "--now", "2026-11-03 09:30", "--out", self.wl])
        self.assertIn("after 09:35", err)
        os.makedirs(self.t)
        cfg = os.path.join(self.t, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"no_entries_after": "09:45"}, fh)
        code, _, err = run(["orb", "--offline", ORB, "--now", "2026-11-03 10:00", "--config", cfg])
        self.assertEqual(code, 1)
        self.assertIn("expired", err)
        code, _, err = run(["orb", "--offline", ORB, "--now", "2026-11-03 15:10", "--no-write"])
        self.assertIn("after 15:00", err)                  # the default no_entries_after
        code, _, err = run(["orb", "--offline", ORB])
        self.assertIn("--at HH:MM", err)
        code, text, _ = run(["orb", "--offline", ORB, "--now", "2026-11-03 09:35", "--symbols", "RELIANCE",
                             "--out", self.wl])
        self.assertEqual(code, 1)                          # no breakout yet, but the empty list is written
        self.assertIn("No chart closed above its opening-range high by 09:35.", text)
        self.assertEqual(self.read(self.wl)["hits"], [])
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt")])
        self.assertEqual(code, 1)
        self.assertIn("scan.py universe", err)
        code, _, err = run(["daily", "--offline", os.path.join(self.d, "nothing-here")])
        self.assertIn("no offline file", err)
        empty = os.path.join(self.d, "empty.json")
        with open(empty, "w") as fh:
            fh.write('{"bars": {}}')
        code, _, err = run(["daily", "--offline", empty])
        self.assertEqual(code, 1)
        self.assertIn("no symbols to scan", err)
        with open(empty, "w") as fh:
            fh.write('{"spark": {}}')
        code, _, err = run(["daily", "--offline", empty])
        self.assertIn("not a trader-scanner bundle", err)

    def test_offline_folder_and_save(self):
        folder = os.path.join(self.d, "bundles")
        os.makedirs(folder)
        data, _, _ = scan.load_offline(ORB)
        for sym in ("RELIANCE", "TCS", "ADANIENSOL"):
            with open(os.path.join(folder, sym + ".json"), "w") as fh:
                json.dump({"interval": "5minute", "bars": {sym: data[sym]}, "failed": {}}, fh)
        with open(os.path.join(folder, "gone.json"), "w") as fh:
            json.dump({"bars": {}, "failed": {"GONE": "no bars from INDstocks"}}, fh)
        code, text, _ = run(["orb", "--offline", folder, "--now", "2026-11-03 10:00", "--no-write", "--json"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"], out["failed"]), (4, 3, 1))
        self.assertEqual([h["symbol"] for h in out["hits"]], ["ADANIENSOL"])
        self.assertEqual(code, 0)
        self.assertEqual(scan.bundle({"X": []}, {}, "5minute", D(2026, 11, 3), D(2026, 11, 4), DT(2026, 11, 3, 10, 0)),
                         {"created_by": "trader-scanner", "created_at": "2026-11-03T10:00:00+05:30",
                          "source": "indstocks", "interval": "5minute", "start": "2026-11-03", "end": "2026-11-04",
                          "bars": {"X": []}, "failed": {}})

    def test_symbol_lists(self):
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w") as fh:
            fh.write("# my list\nRELIANCE|Reliance Industries Ltd\ntcs\n\nRELIANCE\n")
        self.assertEqual(scan.symbols_from_file(txt), ["RELIANCE", "TCS", "RELIANCE"])
        js = os.path.join(self.d, "wl.json")
        with open(js, "w") as fh:
            json.dump({"hits": [{"symbol": "reliance"}, {"symbol": "TCS"}]}, fh)
        self.assertEqual(scan.symbols_from_file(js), ["RELIANCE", "TCS"])
        code, text, _ = run(["daily", "--offline", DAILY, "--watchlist", txt, "--now", "2026-11-04 18:00",
                             "--json", "--no-write"])
        out = json.loads(text)
        self.assertEqual((out["universe"], out["scanned"]), (2, 2))
        code, text, _ = run(["daily", "--offline", DAILY, "--limit", "3", "--now", "2026-11-04 18:00", "--json",
                             "--no-write"])
        self.assertEqual(json.loads(text)["universe"], 3)
        code, text, _ = run(["daily", "--offline", DAILY, "--symbols", "RELIANCE,TCS", "--now", "2026-11-04 12:00",
                             "--json", "--no-write"])
        self.assertEqual(json.loads(text)["as_of"], "2026-11-03 close")   # during the session


UNIVERSE_COUNT = 2233


if __name__ == "__main__":
    unittest.main()

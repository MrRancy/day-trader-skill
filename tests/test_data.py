import datetime
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, ROOT

spec = importlib.util.spec_from_file_location("trader_data", os.path.join(ROOT, "trader_data.py"))
td = importlib.util.module_from_spec(spec)
spec.loader.exec_module(td)

DIR = os.path.join(FIX, "trader_data")
MASTER = os.path.join(DIR, "instruments-equity-2026-11-04.csv.gz")   # the real equity master, read 2026-11-04 17:58 IST
BARS = os.path.join(DIR, "RELIANCE-5m-2026-11-03.json")              # real 5-minute bars, Tue Nov 3 2026
QUOTE = os.path.join(DIR, "quote-RELIANCE-2026-11-04-1759.json")     # real full quote, after the close Nov 4
DT = datetime.datetime
D = datetime.date


def doc(path):
    with open(path) as fh:
        return json.load(fh)


def offline():
    """An INDstocks backend that never touches the network: the saved master, bars and quote."""
    b = td.INDstocks(token_value="test-token")
    b.load_instruments(MASTER)
    calls = []

    def raw_candles(codes, interval, start_ms, end_ms):
        calls.append(("candles", list(codes), interval, start_ms, end_ms))
        return doc(BARS)

    def raw_quotes(codes, kind="full"):
        calls.append(("quotes", list(codes), kind))
        return doc(QUOTE)

    b.raw_candles = raw_candles
    b.raw_quotes = raw_quotes
    b.calls = calls
    return b


def run(argv, backend):
    td._default["indstocks"] = backend
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = td.main(argv)
    finally:
        td._default.pop("indstocks", None)
    return code, out.getvalue(), err.getvalue()


class Clock(unittest.TestCase):
    def test_ist_is_utc_plus_530_no_dst(self):
        # the first 5-minute bar of Nov 3 2026 opens at 09:15 IST = 03:45 UTC
        self.assertEqual(td.ist_of(1793677500), DT(2026, 11, 3, 9, 15))
        self.assertEqual(td.ts_of(DT(2026, 11, 3, 9, 15)), 1793677500)
        self.assertEqual(td.ts_of(D(2026, 11, 3)), 1793644200)
        self.assertEqual(td.ms_of(D(2026, 11, 3)), 1793644200000)
        # June and December are the same offset
        self.assertEqual(td.ist_of(td.ts_of(DT(2026, 6, 1, 12, 0))), DT(2026, 6, 1, 12, 0))
        self.assertEqual(td.ist_of(td.ts_of(DT(2026, 12, 1, 12, 0))), DT(2026, 12, 1, 12, 0))

    def test_parse_ist(self):
        self.assertEqual(td.parse_ist("2026-11-03"), DT(2026, 11, 3))
        self.assertEqual(td.parse_ist("2026-11-03 10:15"), DT(2026, 11, 3, 10, 15))
        self.assertEqual(td.parse_ist("2026-11-03T10:15:30"), DT(2026, 11, 3, 10, 15, 30))
        self.assertEqual(td.parse_ist(D(2026, 11, 3)), DT(2026, 11, 3))
        with self.assertRaises(td.DataError):
            td.parse_ist("3 Nov 2026")

    def test_window_defaults_to_one_session_for_intraday_bars(self):
        self.assertEqual(td.window("2026-11-03", None, "5minute"), (DT(2026, 11, 3), DT(2026, 11, 4)))
        self.assertEqual(td.window("2026-11-03", "2026-11-04", "5minute"), (DT(2026, 11, 3), DT(2026, 11, 4)))
        s, e = td.window("2026-11-03", None, "1day")
        self.assertEqual(s, DT(2026, 11, 3))
        self.assertGreater(e, DT(2026, 11, 4))   # now
        with self.assertRaises(td.DataError):
            td.window("2026-11-04", "2026-11-03", "5minute")

    def test_windows_split_at_the_api_maximum(self):
        w = td.windows(DT(2026, 10, 1), DT(2026, 10, 21), 7)
        self.assertEqual(w, [(DT(2026, 10, 1), DT(2026, 10, 8)), (DT(2026, 10, 8), DT(2026, 10, 15)),
                             (DT(2026, 10, 15), DT(2026, 10, 21))])
        self.assertEqual(td.windows(DT(2026, 10, 1), DT(2026, 10, 2), 7), [(DT(2026, 10, 1), DT(2026, 10, 2))])

    def test_intervals(self):
        self.assertEqual(td.api_interval("5m"), "5minute")
        self.assertEqual(td.api_interval("1h"), "60minute")
        self.assertEqual(td.api_interval("1d"), "1day")
        self.assertEqual(td.api_interval("15minute"), "15minute")
        with self.assertRaises(td.DataError):
            td.api_interval("45m")
        self.assertEqual(td.MAX_DAYS["5minute"], 7)
        self.assertEqual(td.MAX_DAYS["60minute"], 15)
        self.assertEqual(td.MAX_DAYS["1day"], 365)


class Master(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = td.parse_instruments(td.read_text(MASTER))
        cls.codes = td.code_table(cls.rows)

    def test_the_file_as_read(self):
        self.assertEqual(len(self.rows), 23044)
        by = {}
        for r in self.rows:
            by[r["EXCH"]] = by.get(r["EXCH"], 0) + 1
        self.assertEqual(by, {"NSE": 10049, "BSE": 12995})
        self.assertEqual(len(self.rows[0]), 26)   # equity carries 26 columns

    def test_reliance(self):
        self.assertEqual(self.codes[("NSE", "RELIANCE")], "NSE_2885")
        row = next(r for r in self.rows if r["EXCH"] == "NSE" and r["TRADING_SYMBOL"] == "RELIANCE")
        self.assertEqual(row["SERIES"], "EQ")
        self.assertEqual(row["ISIN"], "INE002A01018")
        self.assertEqual(row["TICK_SIZE"], "10")
        # the docs say SYMBOL_NAME is the base symbol and UNDERLYING_SCRIP_NAME the full name;
        # the file read on 2026-11-04 has them the other way round on cash rows
        self.assertEqual(row["SYMBOL_NAME"], "Reliance Industries Ltd")
        self.assertEqual(row["UNDERLYING_SCRIP_NAME"], "RELIANCE")
        self.assertEqual(row["CUSTOM_SYMBOL"], "RELIANCE INDUSTRIES LTD")
        bse = self.codes[("BSE", "RELIANCE")]
        self.assertTrue(bse.startswith("BSE_"))
        bse_row = next(r for r in self.rows if r["EXCH"] == "BSE" and "BSE_" + r["SECURITY_ID"] == bse)
        self.assertEqual(bse_row["ISIN"], "INE002A01018")   # the same share on the other exchange

    def test_main_series_wins(self):
        rows = [{"EXCH": "NSE", "TRADING_SYMBOL": "X", "SECURITY_ID": "2", "INSTRUMENT_NAME": "EQUITY", "SERIES": "BE"},
                {"EXCH": "NSE", "TRADING_SYMBOL": "X", "SECURITY_ID": "1", "INSTRUMENT_NAME": "EQUITY", "SERIES": "EQ"},
                {"EXCH": "NSE", "TRADING_SYMBOL": "X", "SECURITY_ID": "3", "INSTRUMENT_NAME": "FUTSTK", "SERIES": ""}]
        self.assertEqual(td.code_table(rows), {("NSE", "X"): "NSE_1"})

    def test_index_file_is_read_by_position(self):
        text = "EXCH,SEGMENT,SECURITY_ID\nNSE,NIFTY 50,40000001\nBSE,BSE Focused IT,40000129\n"
        self.assertEqual(td.parse_instruments(text, "index"),
                         [{"EXCH": "NSE", "INDEX_NAME": "NIFTY 50", "SECURITY_ID": "40000001"},
                          {"EXCH": "BSE", "INDEX_NAME": "BSE Focused IT", "SECURITY_ID": "40000129"}])

    def test_wrong_shape(self):
        with self.assertRaises(td.DataError):
            td.parse_instruments("a,b\n1,2\n")

    def test_resolve(self):
        b = offline()
        self.assertEqual(b.resolve("RELIANCE"), "NSE_2885")
        self.assertEqual(b.resolve("reliance"), "NSE_2885")
        self.assertEqual(b.resolve("NSE:RELIANCE"), "NSE_2885")
        self.assertEqual(b.resolve("NSE_2885"), "NSE_2885")
        self.assertTrue(b.resolve("BSE:RELIANCE").startswith("BSE_"))
        with self.assertRaises(td.DataError) as cm:
            b.resolve("NOSUCHSTOCK")
        self.assertIn("NOSUCHSTOCK is not in the INDstocks NSE equity master", str(cm.exception))


class Bars(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = td.parse_candles(doc(BARS))["NSE_2885"]

    def test_one_full_session(self):
        # 09:15 to 15:25: 75 five-minute bars, every one on the grid
        self.assertEqual(len(self.bars), 75)
        self.assertEqual(td.ist_of(self.bars[0]["ts"]), DT(2026, 11, 3, 9, 15))
        self.assertEqual(td.ist_of(self.bars[-1]["ts"]), DT(2026, 11, 3, 15, 25))
        self.assertEqual([b["ts"] for b in self.bars], [1793677500 + 300 * i for i in range(75)])

    def test_the_first_and_last_bar(self):
        self.assertEqual(self.bars[0], {"ts": 1793677500, "o": 1285.0, "h": 1307.0, "l": 1282.5, "c": 1302.0, "v": 5145542})
        self.assertEqual(self.bars[-1]["c"], 1307.9)
        self.assertEqual(self.bars[-1]["v"], 2685921)

    def test_every_bar_is_sane(self):
        for b in self.bars:
            self.assertGreaterEqual(b["h"], max(b["o"], b["c"]), b)
            self.assertLessEqual(b["l"], min(b["o"], b["c"]), b)
            self.assertGreater(b["v"], 0, b)

    def test_labels(self):
        self.assertEqual(td.bar_label(self.bars[0]), "2026-11-03 09:15")
        self.assertEqual(td.bar_label(self.bars[0], "1day"), "2026-11-03")

    def test_parse_sorts_and_skips_junk(self):
        d = {"success": True, "data": {"NSE_1": {"candles": [
            {"ts": 20, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 3},
            {"ts": 10, "o": 1, "h": 2, "l": 0.5, "c": 1.5},
            {"ts": "x", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 3}]}}}
        self.assertEqual([b["ts"] for b in td.parse_candles(d)["NSE_1"]], [10, 20])
        self.assertEqual(td.parse_candles(d)["NSE_1"][0]["v"], 0)
        with self.assertRaises(td.DataError) as cm:
            td.parse_candles({"success": False, "message": "Bad Request", "debug_info": "invalid interval."})
        self.assertIn("Bad Request", str(cm.exception))

    def test_tidy_drops_duplicates_and_the_outside(self):
        bars = [{"ts": 5}, {"ts": 1}, {"ts": 3}, {"ts": 3}, {"ts": 9}]
        self.assertEqual(td.tidy(bars, 1, 9), [{"ts": 1}, {"ts": 3}, {"ts": 5}])

    def test_candles_through_the_backend(self):
        b = offline()
        bars = b.candles("RELIANCE", "5m", "2026-11-03")
        self.assertEqual(len(bars), 75)
        self.assertEqual(b.calls, [("candles", ["NSE_2885"], "5minute", 1793644200000, 1793730600000)])
        # a time window inside the day trims the bars
        b.calls[:] = []
        bars = b.candles("RELIANCE", "5m", "2026-11-03 09:15", "2026-11-03 09:30")
        self.assertEqual([td.ist_of(x["ts"]).strftime("%H:%M") for x in bars], ["09:15", "09:20", "09:25"])

    def test_many_symbols_go_five_a_request(self):
        b = offline()
        syms = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "ITC"]
        out = b.candles_many(syms, "5m", "2026-11-03")
        self.assertEqual(sorted(out), sorted(syms))
        self.assertEqual([len(c[1]) for c in b.calls], [5, 2])
        self.assertEqual(len(out["RELIANCE"]), 75)
        self.assertEqual(out["TCS"], [])   # the saved answer only has RELIANCE
        with self.assertRaises(td.DataError):
            td.INDstocks(token_value="t").raw_candles(["NSE_%d" % i for i in range(6)], "5minute", 0, 1)

    def test_a_long_history_walks_window_by_window(self):
        b = offline()
        b.candles("RELIANCE", "5m", "2026-10-01", "2026-10-21")
        self.assertEqual(len(b.calls), 3)
        self.assertEqual(b.calls[0][3:], (td.ms_of(DT(2026, 10, 1)), td.ms_of(DT(2026, 10, 8))))
        self.assertEqual(b.calls[-1][3:], (td.ms_of(DT(2026, 10, 15)), td.ms_of(DT(2026, 10, 21))))


class Quotes(unittest.TestCase):
    def test_the_saved_quote(self):
        q = td.parse_quotes(doc(QUOTE))["NSE_2885"]
        self.assertEqual(q["ltp"], 1311)
        self.assertEqual(q["prev_close"], 1307.9)    # Nov 3's last 5-minute close
        self.assertEqual(q["open"], 1301.9)
        self.assertEqual(q["high"], 1318.9)
        self.assertEqual(q["low"], 1296)
        self.assertEqual(q["volume"], 19451679)
        self.assertEqual((q["lower_circuit"], q["upper_circuit"]), (1277.3, 1356.3))
        self.assertEqual(q["change_pct"], 0.24)
        self.assertEqual((q["high_52w"], q["low_52w"]), (1611.8, 1152.2))

    def test_through_the_backend(self):
        b = offline()
        q = b.quote("RELIANCE")
        self.assertEqual(list(q), ["RELIANCE"])
        self.assertEqual(q["RELIANCE"]["ltp"], 1311)
        self.assertEqual(b.calls, [("quotes", ["NSE_2885"], "full")])

    def test_missing_fields_and_errors(self):
        q = td.parse_quotes({"status": "success", "data": {"NSE_1": {"live_price": "1,234.5"}}})["NSE_1"]
        self.assertEqual(q["ltp"], 1234.5)
        self.assertIsNone(q["volume"])
        with self.assertRaises(td.DataError):
            td.parse_quotes({"status": "error", "message": "Invalid scrip codes or mode"})


class Token(unittest.TestCase):
    def test_dotenv(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, ".env")
            with open(p, "w") as fh:
                fh.write("# comment\nexport A=1\nINDSTOCKS_TOKEN='abc.def'\nB = \"x\"\nbad line\n")
            self.assertEqual(td.read_dotenv(p), {"A": "1", "INDSTOCKS_TOKEN": "abc.def", "B": "x"})
            old = os.environ.pop("INDSTOCKS_TOKEN", None)
            try:
                self.assertEqual(td.token(dotenv=p), "abc.def")
                os.environ["INDSTOCKS_TOKEN"] = "fromenv"
                self.assertEqual(td.token(dotenv=p), "fromenv")
                del os.environ["INDSTOCKS_TOKEN"]
                with self.assertRaises(td.DataError) as cm:
                    td.token(dotenv=os.path.join(d, "none"))
                self.assertIn("INDSTOCKS_TOKEN is not set", str(cm.exception))
            finally:
                if old is not None:
                    os.environ["INDSTOCKS_TOKEN"] = old
        self.assertEqual(td.read_dotenv(os.path.join(d, "gone")), {})


class Transport(unittest.TestCase):
    def setUp(self):
        self.slept = []
        self.orig = (td.OPENER.open, td.SLEEP)
        td.SLEEP = self.slept.append

    def tearDown(self):
        td.OPENER.open, td.SLEEP = self.orig

    def serve(self, answers):
        """answers: list of HTTP codes or body strings, one per call."""
        seen = []

        def urlopen(req, timeout=0):
            seen.append(req)
            a = answers.pop(0)
            if isinstance(a, int):
                raise urllib.error.HTTPError(req.full_url, a, "err", {}, io.BytesIO(b'{"message":"nope"}'))

            class R(object):
                def __enter__(self_):
                    return self_

                def __exit__(self_, *x):
                    return False

                def read(self_):
                    return a.encode("utf-8")
            return R()
        td.OPENER.open = urlopen
        return seen

    def test_ipv4_is_tried_first(self):
        # the live host answers AAAA records; a dead IPv6 route must not cost 40 seconds a call
        infos = [(td.socket.AF_INET6, td.socket.SOCK_STREAM, 6, "", ("2606:4700::1", 443, 0, 0)),
                 (td.socket.AF_INET, td.socket.SOCK_STREAM, 6, "", ("104.18.16.94", 443))]
        infos.sort(key=lambda i: i[0] != td.socket.AF_INET)
        self.assertEqual([i[0] for i in infos], [td.socket.AF_INET, td.socket.AF_INET6])
        self.assertTrue(any(isinstance(h, td._IPv4FirstHandler) for h in td.OPENER.handlers))

    def test_the_token_rides_in_the_authorization_header_bare(self):
        seen = self.serve(['{"status":"success","data":{"user_id":"1"}}'])
        b = td.INDstocks(token_value="tok123")
        self.assertEqual(b.whoami(), {"user_id": "1"})
        self.assertEqual(seen[0].get_header("Authorization"), "tok123")
        self.assertEqual(seen[0].full_url, "https://api.indstocks.com/user/profile")

    def test_an_expired_token_is_said_in_words(self):
        self.serve([401])
        with self.assertRaises(td.DataError) as cm:
            td.INDstocks(token_value="old").whoami()
        self.assertIn("expired", str(cm.exception))
        self.assertIn("INDSTOCKS_TOKEN", str(cm.exception))

    def test_one_retry_after_a_429_then_a_plain_error(self):
        seen = self.serve([429, '{"success":true,"data":{}}'])
        self.assertEqual(td.INDstocks(token_value="t").raw_candles(["NSE_1"], "5minute", 0, 1), {"success": True, "data": {}})
        self.assertEqual(len(seen), 2)
        self.assertIn(td.BACKOFF, self.slept)
        self.serve([400])
        with self.assertRaises(td.DataError) as cm:
            td.INDstocks(token_value="t").raw_candles(["NSE_1"], "5minute", 0, 1)
        self.assertEqual(str(cm.exception), "INDstocks answered HTTP 400 for /market/historical/5minute: nope")

    def test_the_candle_url(self):
        seen = self.serve(['{"success":true,"data":{}}'])
        td.INDstocks(token_value="t").raw_candles(["NSE_2885", "NSE_11536"], "5minute", 1793644200000, 1793730600000)
        self.assertEqual(seen[0].full_url, "https://api.indstocks.com/market/historical/5minute?"
                                           "scrip-codes=NSE_2885,NSE_11536&start_time=1793644200000&end_time=1793730600000")

    def test_other_backends_say_so(self):
        for name in ("kite", "yahoo"):
            with self.assertRaises(td.DataError) as cm:
                td.backend(name).candles("RELIANCE", "1d", "2026-11-03")
            self.assertIn("not built yet", str(cm.exception))
        with self.assertRaises(td.DataError):
            td.backend("alpaca")


class CLI(unittest.TestCase):
    def test_candles_table(self):
        code, out, err = run(["candles", "RELIANCE", "5m", "2026-11-03", "--show", "3"], offline())
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "RELIANCE (NSE_2885) 5m bars, 2026-11-03 00:00:00 to 2026-11-04 00:00:00 IST: 75 bars")
        self.assertEqual(lines[2].split(), ["2026-11-03", "09:15", "1285.00", "1307.00", "1282.50", "1302.00", "5145542"])
        self.assertEqual(lines[-1], "... 72 more")

    def test_candles_json_and_out(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "raw.json")
            code, out, err = run(["candles", "RELIANCE", "5m", "2026-11-03", "--json", "--out", p], offline())
            self.assertEqual(code, 0, err)
            self.assertEqual(len(json.loads(out)["bars"]), 75)
            self.assertEqual(doc(p), doc(BARS))   # the raw answer, byte for byte in meaning

    def test_quote_table(self):
        code, out, err = run(["quote", "RELIANCE,TCS"], offline())
        self.assertEqual(code, 0, err)
        self.assertIn("RELIANCE        1311.00    1301.90    1318.90    1296.00    1307.90     19451679   1277.30 to 1356.30", out)
        self.assertIn("TCS          no quote", out)

    def test_errors_are_one_line(self):
        code, out, err = run(["candles", "NOSUCH", "5m", "2026-11-03"], offline())
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "NOSUCH is not in the INDstocks NSE equity master")
        code, out, err = run(["candles", "RELIANCE", "45m", "2026-11-03"], offline())
        self.assertEqual(code, 1)
        self.assertIn("unknown interval", err)
        self.assertEqual(run([], offline())[0], 1)


if __name__ == "__main__":
    unittest.main()

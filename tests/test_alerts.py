import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

alerts = load("trader-alerts", "alerts")
td = alerts.trader_data

QDIR = os.path.join(FIX, "trader-alerts")
PLAN = os.path.join(FIX, "trader-budget", "plan-tataconsum.json")
DT = datetime.datetime


def ist(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules():
    with open(PLAN) as fh:
        return alerts.rules_from_plan(json.load(fh))


class Calendar(unittest.TestCase):
    def test_the_calendar_is_trader_datas(self):
        self.assertIs(alerts.NSE_HOLIDAYS, td.NSE_HOLIDAYS)
        self.assertEqual(len(alerts.NSE_HOLIDAYS), 20)
        self.assertIn("2026-11-08", alerts.MUHURAT)

    def test_parse_now(self):
        self.assertEqual(ist("2026-11-03T09:15"), DT(2026, 11, 3, 9, 15))
        self.assertEqual(ist("2026-11-03 10:00"), DT(2026, 11, 3, 10, 0))
        self.assertEqual(ist("2026-11-03T04:30:00Z"), DT(2026, 11, 3, 10, 0))
        self.assertEqual(ist("2026-11-03T10:00:00+05:30"), DT(2026, 11, 3, 10, 0))
        with self.assertRaises(alerts.AlertError):
            ist("tomorrow")

    def test_market_status(self):
        st = alerts.market_status(ist("2026-11-03T10:00"))
        self.assertTrue(st["open"])
        self.assertEqual((st["opens"], st["closes"]), ("09:15", "15:30"))
        self.assertFalse(alerts.market_status(ist("2026-11-03T09:14"))["open"])
        self.assertEqual(alerts.market_status(ist("2026-11-03T09:14"))["next_open"], "2026-11-03 09:15")
        after = alerts.market_status(ist("2026-11-03T15:30"))
        self.assertFalse(after["open"])
        self.assertEqual(after["next_open"], "2026-11-04 09:15")
        diwali = alerts.market_status(ist("2026-11-10T11:00"))
        self.assertFalse(diwali["open"])
        self.assertIn("Diwali-Balipratipada", diwali["reason"])
        self.assertEqual(diwali["next_open"], "2026-11-11 09:15")
        muhurat = alerts.market_status(ist("2026-11-08T18:30"))
        self.assertFalse(muhurat["open"])
        self.assertIn("Muhurat", muhurat["muhurat"])
        self.assertEqual(alerts.market_status(ist("2026-11-07T10:00"))["reason"], "weekend")
        self.assertEqual(alerts.market_status(ist("2026-12-24T15:29"))["open"], True)   # no early close in India
        self.assertFalse(alerts.market_status(ist("2028-03-01T10:00"))["calendar_known"])


class Quotes(unittest.TestCase):
    def test_offline_fixtures(self):
        q, errors = alerts.get_quotes(["TATACONSUM", "RELIANCE", "NOPE"], QDIR, when=DT(2026, 11, 4, 18, 0))
        self.assertEqual(sorted(q), ["RELIANCE", "TATACONSUM"])
        self.assertEqual(len(errors), 1)
        self.assertIn("no offline quote file", errors[0])
        r = q["RELIANCE"]
        self.assertEqual((r["price"], r["prev_close"], r["open"], r["high"], r["low"]), (1311.0, 1307.9, 1301.9, 1318.9, 1296.0))
        self.assertEqual((r["upper_circuit"], r["lower_circuit"], r["source"], r["currency"]), (1356.3, 1277.3, "indstocks", "INR"))
        self.assertEqual(r["time"], "2026-11-04T18:00:00+05:30")
        t = q["TATACONSUM"]
        self.assertEqual((t["price"], t["prev_close"], t["upper_circuit"], t["lower_circuit"]), (1068.0, 1073.0, 1180.3, 965.7))

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "indstocks-RELIANCE.json"), "rb") as src:
                with gzip.open(os.path.join(d, "indstocks-RELIANCE.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["RELIANCE"], d)
            self.assertEqual((q["RELIANCE"]["price"], errors), (1311.0, []))
        finally:
            shutil.rmtree(d)

    def test_a_fake_backend_answers_one_request_for_every_symbol(self):
        calls = []

        class Fake(object):
            def resolve(self, s):
                if s == "NOPE":
                    raise td.DataError("NOPE is not in the INDstocks NSE equity master")
                return "NSE_1"

            def quote(self, syms):
                calls.append(list(syms))
                return {"RELIANCE": {"ltp": 1311.0, "prev_close": 1307.9, "open": 1301.9, "high": 1318.9,
                                     "low": 1296.0, "upper_circuit": 1356.3, "lower_circuit": 1277.3, "volume": 1},
                        "TATACONSUM": {"ltp": None}}
        q, errors = alerts.get_quotes(["RELIANCE", "TATACONSUM", "NOPE"], backend=Fake(), when=DT(2026, 11, 4, 18, 0))
        self.assertEqual(calls, [["RELIANCE", "TATACONSUM"]])
        self.assertEqual(list(q), ["RELIANCE"])
        self.assertEqual(len(errors), 2)
        self.assertIn("NOPE is not in", errors[0])
        self.assertIn("no last price for TATACONSUM", errors[1])

        class Dead(object):
            def resolve(self, s):
                raise td.DataError("INDstocks answered HTTP 403 for /market/instruments: the token is invalid or has expired")
        q, errors = alerts.get_quotes(["RELIANCE"], backend=Dead())
        self.assertEqual(q, {})
        self.assertIn("expired", errors[0])


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(["TATACONSUM", "RELIANCE"], QDIR, when=DT(2026, 11, 3, 10, 0))
        self.open = alerts.market_status(ist("2026-11-03T10:00"))
        self.now = ist("2026-11-03T10:00")

    def at(self, text, rules, state=None, quotes=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now),
                               state or {}, now)

    def test_a_target_rings_once(self):
        rules = [{"id": "rel-1300", "symbol": "RELIANCE", "type": "above", "price": 1300}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertIn("RELIANCE is Rs 1311.00: at or above your alert level Rs 1300", rings[0]["message"])
        self.assertEqual(st["rung"]["rel-1300"], "2026-11-03T10:00:00+05:30")
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])

    def test_stop_and_rearm(self):
        rules = [{"id": "tc-stop", "symbol": "TATACONSUM", "type": "stop", "price": 1070, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("your stop Rs 1070", rings[0]["message"])
        self.q["TATACONSUM"]["price"] = 1075.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("tc-stop", st["rung"])
        self.q["TATACONSUM"]["price"] = 1069.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(len(rings), 1)

    def test_pct_move_from_previous_close(self):
        big = [{"id": "tc-3", "symbol": "TATACONSUM", "type": "pct_move", "pct": 3}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        small = [{"id": "tc-04", "symbol": "TATACONSUM", "type": "pct_move", "pct": 0.4, "direction": "down"}]
        rings, st = alerts.evaluate(small, self.q, self.open, {}, self.now)
        self.assertIn("TATACONSUM moved -0.47% to Rs 1068.00 (from Rs 1073.00)", rings[0]["message"])
        self.assertEqual(alerts.evaluate(small, self.q, self.open, st, self.now)[0], [])
        up = [{"id": "tc-up", "symbol": "TATACONSUM", "type": "pct_move", "pct": 0.4, "direction": "up"}]
        self.assertEqual(alerts.evaluate(up, self.q, self.open, {}, self.now)[0], [])
        # rings again the next day
        self.assertEqual(len(self.at("2026-11-04 10:00", small, st)[0]), 1)

    def test_circuit_once_per_day(self):
        rules = [{"id": "rel-circuit", "symbol": "RELIANCE", "type": "circuit"}]
        self.assertEqual(alerts.evaluate(rules, self.q, self.open, {}, self.now)[0], [])
        q = json.loads(json.dumps(self.q))
        q["RELIANCE"]["price"] = 1356.3
        rings, st = alerts.evaluate(rules, q, self.open, {}, self.now)
        self.assertIn("RELIANCE is Rs 1356.30, at its upper circuit band Rs 1356.30: a sell may not fill", rings[0]["message"])
        self.assertEqual(alerts.evaluate(rules, q, self.open, st, self.now)[0], [])
        q["RELIANCE"]["price"] = 1277.0
        rings, _ = alerts.evaluate(rules, q, self.open, {}, self.now)
        self.assertIn("lower circuit band Rs 1277.30: a buy may not fill, and a stop below it cannot", rings[0]["message"])

    def test_opening_bell_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        closed = alerts.market_status(ist("2026-11-03T08:00"))
        self.assertEqual(alerts.evaluate(rules, {}, closed, {}, ist("2026-11-03T08:00"))[0], [])
        rings, st = alerts.evaluate(rules, {}, self.open, {}, self.now)
        self.assertIn("Opening bell: the NSE is open (2026-11-03 10:00 IST)", rings[0]["message"])
        self.assertEqual(alerts.evaluate(rules, {}, self.open, st, ist("2026-11-03T11:00"))[0], [])
        nxt = alerts.market_status(ist("2026-11-04T09:16"))
        self.assertEqual(len(alerts.evaluate(rules, {}, nxt, st, ist("2026-11-04T09:16"))[0]), 1)

    def test_the_plan_gives_a_stop_a_target_and_flat_by(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("tataconsum-plan-stop", "stop"), ("tataconsum-plan-target", "target"), ("tataconsum-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (1039.4, 1061.0))
        self.assertEqual((rules[2]["at"], rules[2]["label"]), ("15:15", "flat by close"))
        self.assertTrue(all(r["date"] == "2026-11-03" for r in rules))
        q = json.loads(json.dumps(self.q))
        q["TATACONSUM"]["price"] = 1055.0
        rings, st = self.at("2026-11-03 10:53", rules, None, q)
        self.assertEqual(rings, [])
        q["TATACONSUM"]["price"] = 1039.1
        rings, st = self.at("2026-11-03 11:30", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["tataconsum-plan-stop"])
        self.assertIn("TATACONSUM is Rs 1039.10: at or below your stop Rs 1039.40 (plan stop: opening-range midpoint)",
                      rings[0]["message"])
        q["TATACONSUM"]["price"] = 1061.0
        self.assertEqual([r["id"] for r in self.at("2026-11-03 12:00", rules, st, q)[0]], ["tataconsum-plan-target"])
        # the fixture quote (1,068.00) is above the target: it rings at once
        rings, _ = self.at("2026-11-03 10:00", rules)
        self.assertEqual([r["id"] for r in rings], ["tataconsum-plan-target"])

    def test_flat_by_rings_once_per_trading_day(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-11-03 15:14", rules)[0], [])
        rings, st = self.at("2026-11-03 15:15", rules)
        self.assertEqual(len(rings), 1)
        self.assertIn("Flat by close: it is 15:15 IST (set for 15:15), the market closes at 15:30 and the broker "
                      "squares off MIS from about 15:20", rings[0]["message"])
        self.assertIn("trader-broker flatten --symbol TATACONSUM", rings[0]["message"])
        self.assertEqual(self.at("2026-11-03 15:19", rules, st)[0], [])
        rings, _ = self.at("2026-11-03 15:40", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-11-04 15:16", rules, st)[0], [])     # plan rules: their day only
        plain = [{"id": "t", "type": "time", "at": "15:15", "label": "check"}]
        self.assertEqual(self.at("2026-11-07 15:16", plain)[0], [])         # Saturday: no session
        self.assertEqual(self.at("2026-11-10 15:16", plain)[0], [])         # a holiday
        rings, st = self.at("2026-12-01 15:15", plain)
        self.assertEqual(st["rung"]["t"], "2026-12-01T15:15:00+05:30")

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "trader-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("tataconsum")], plan_rules())

    def test_bad_rules(self):
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "moon"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "above", "symbol": "RELIANCE"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "time", "at": "3pm"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "circuit"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "TATACONSUM", "stop": 1039.4})


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", QDIR, "--now", now])

    def test_the_trade_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", os.path.join(self.d, "none")])
        self.assertEqual(code, 0)
        self.assertIn("Added tataconsum-flat-by", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)

        code, text, _ = self.check("2026-11-03 10:00")
        self.assertEqual(code, 0)                                      # the bell and the target (the quote is 1,068)
        self.assertIn("ALERT Opening bell", text)
        self.assertIn("ALERT TATACONSUM is Rs 1068.00: at or above your target Rs 1061", text)
        self.assertIn("not an order", text)
        code, text, _ = self.check("2026-11-03 10:05")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 4 rule(s) watched today, TATACONSUM Rs 1068.00.", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"TATACONSUM": 1068.0})
        self.assertEqual(doc["detail"]["TATACONSUM"]["fetched"], "2026-11-03T10:05:00+05:30")
        self.assertEqual((doc["currency"], doc["source"]), ("INR", "indstocks"))

        code, text, _ = self.check("2026-11-03 15:15")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by close", text)
        self.assertEqual(self.check("2026-11-03 15:17")[0], 1)
        code, text, _ = self.check("2026-11-04 09:20")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-04 09:25")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state])
        with open(self.rules) as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["opening-bell", "tataconsum-flat-by", "tataconsum-plan-stop", "tataconsum-plan-target"])
        with open(self.state) as fh:
            self.assertNotIn("tataconsum-plan-target", json.load(fh)["rung"])

    def test_levels_evening_and_morning(self):
        for args in (["--opening-bell"], ["--symbol", "RELIANCE", "--above", "1320"], ["--symbol", "TATACONSUM", "--stop", "1000"],
                     ["--symbol", "RELIANCE", "--circuit"]):
            code, _, _ = run(["add", "--rules", self.rules] + args)
            self.assertEqual(code, 0)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "RELIANCE", "--above", "1320"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        code, text, _ = self.check("2026-11-04T18:00")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"RELIANCE": 1311.0, "TATACONSUM": 1068.0})

        code, text, _ = self.check("2026-11-05T09:16")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-11-05T09:45")
        self.assertEqual(code, 1)

        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang", text)
        self.assertIn("reliance-circuit", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_add_time_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--at", "15:15", "--label", "flat by close", "--symbol", "tataconsum"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "TATACONSUM", "--target", "1061.00",
                             "--date", "2026-11-03"])
        self.assertEqual(code, 0)
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1515", "type": "time", "at": "15:15", "label": "flat by close",
                                    "symbol": "TATACONSUM"})
        self.assertEqual(rules[1], {"id": "tataconsum-target-1061", "symbol": "TATACONSUM", "type": "target",
                                    "price": 1061.0, "date": "2026-11-03"})
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "3pm"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "RELIANCE", "--offline", QDIR, "--write", self.prices, "--now", "2026-11-04T18:00:00+05:30"])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE     Rs 1311.00 (+0.24% vs prev close Rs 1307.90)  band 1277.30 to 1356.30", text)
        self.assertIn("lag", text)
        run(["quote", "TATACONSUM", "--offline", QDIR, "--write", self.prices, "--now", "2026-11-04T18:05:00+05:30"])
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["RELIANCE", "TATACONSUM"])
        self.assertEqual(doc["detail"]["TATACONSUM"]["prev_close"], 1073.0)
        self.assertEqual(doc["detail"]["RELIANCE"]["fetched"], "2026-11-04T18:00:00+05:30")
        self.assertEqual(doc["as_of"], "2026-11-04T18:05:00+05:30")
        # the risk check reads that file as fresh at 18:10
        check = load("trader-risk", "check")
        prices, times = check.load_prices(doc)
        self.assertEqual(prices["TATACONSUM"], 1068.0)
        self.assertEqual(times["TATACONSUM"], DT(2026, 11, 4, 18, 5))

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"symbol": "RELIANCE", "shares": 1}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["RELIANCE", "TATACONSUM"])

    def test_clock_exit_codes(self):
        self.assertEqual(run(["clock", "--now", "2026-11-03 10:00"])[0], 0)
        code, text, _ = run(["clock", "--now", "2026-12-25T10:00"])
        self.assertEqual(code, 1)
        self.assertIn("Christmas", text)
        code, text, _ = run(["clock", "--now", "2026-11-08T18:30"])
        self.assertIn("Muhurat", text)
        self.assertIn("15:20", run(["clock", "--now", "2026-11-03 10:00"])[1])

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-alerts", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-alerts\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("circuit", "muhurat", "15:20", "09:15", "15:30", "never an order", "indstocks", "1,039.40", "1,061.00",
                     "once per day"):
            self.assertIn(must, low)
        for never in ("—", "yahoo", "stooq", "new york", "nyse", "1:00 p.m.", "$5"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()

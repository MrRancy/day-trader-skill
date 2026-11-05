import gzip
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

size = load("trader-budget", "size")
td = size.trader_data

CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
PLAN = os.path.join(FIX, "trader-budget", "plan-tataconsum.json")
TARGETS = os.path.join(ROOT, "trader-budget", "targets.example.json")
MASTER = os.path.join(FIX, "trader_data", "instruments-equity-2026-11-04.csv.gz")
DAILY = os.path.join(FIX, "trader-scanner", "daily-2026-11-04.json.gz")
NOW = "2026-11-03 10:01"
TODAY = "2026-11-03"
# the contract's v2 config, key for key
CONTRACT_CFG = {
    "budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 75000, "min_order_inr": 500,
    "max_loss_per_trade_inr": 1000, "max_daily_loss_inr": 3000, "max_trades_per_day": 3, "max_orders_per_day": 10,
    "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "15:00", "flat_by": "15:15",
    "product": "INTRADAY", "stop_limit_buffer_pct": 0.25, "broker": "indstocks", "paper": True, "blocked": [],
    "allow_fno": False, "allow_margin": False, "allow_short": False,
    "charges": {"brokerage_per_order_inr": 10, "stt_intraday_sell_pct": 0.025, "exchange_txn_pct": 0.00307,
                "sebi_fee_pct": 0.0001, "ipft_pct": 0.0001, "stamp_duty_buy_pct": 0.003, "gst_pct": 18},
}
# a long-term split's rules, for the weights mode tests
SPLIT = {"budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 25, "max_order_inr": 25000,
         "min_order_inr": 500, "paper": True, "blocked": []}


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def split(**over):
    c = dict(SPLIT)
    c.update(over)
    return c


def plan(**over):
    with open(PLAN) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def rows(path):
    with open(path) as fh:
        return size.parse_weights(json.load(fh))


def closes():
    """The Nov 4 2026 closes of the five example names, from the scanner's daily fixture."""
    with gzip.open(DAILY, "rt", encoding="utf-8") as fh:
        d = json.load(fh)
    return dict((s, d["bars"][s][-1]["c"]) for s in ("HDFCBANK", "ICICIBANK", "RELIANCE", "INFY", "TCS"))


def rupees(result):
    return dict((p["symbol"], p["rupees"]) for p in result["positions"])


def shares(result):
    return dict((p["symbol"], p["shares"]) for p in result["positions"])


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


def prices_doc(price, quote_time="2026-11-03T10:28:00+05:30"):
    return {"as_of": quote_time, "source": "indstocks", "prices": {"TATACONSUM": price},
            "detail": {"TATACONSUM": {"prev_close": 1034.9, "time": quote_time, "source": "indstocks"}}}


class Charges(unittest.TestCase):
    def test_one_share_round_trip(self):
        c = td.charges(1, 1050.20, 1039.40)
        self.assertEqual(c["brokerage"], 20.0)                    # two Rs 10 orders
        self.assertEqual(c["stt"], 0.26)                          # 0.025 % of 1,039.40
        self.assertEqual(c["stamp"], 0.03)                        # 0.003 % of 1,050.20
        self.assertEqual(c["gst"], 3.61)                          # 18 % of 20 + 0.06 + 0 + 0
        self.assertEqual(c["total"], 23.97)

    def test_seventy_one_shares(self):
        c = td.charges(71, 1050.20, 1039.40)
        self.assertEqual(c, {"brokerage": 20.0, "stt": 18.45, "exchange": 4.55, "sebi": 0.15, "ipft": 0.15,
                             "stamp": 2.24, "gst": 4.47, "total": 50.01, "per_share": 0.7044})
        self.assertEqual(td.charges(88, 1050.20, 1039.40)["total"], 56.33)
        self.assertEqual(td.charges(0, 1050.20, 1039.40)["total"], 0.0)

    def test_rates_can_be_overridden(self):
        c = td.charges(71, 1050.20, 1039.40, {"brokerage_per_order_inr": 0, "gst_pct": 0})
        self.assertEqual((c["brokerage"], c["gst"]), (0.0, 0.0))
        self.assertEqual(c["total"], 25.54)


class TataConsum(unittest.TestCase):
    """Rs 100,000, 10% buffer, Rs 1,000 max loss, risk 10.80 -> 71 shares, bound by the 75 % position cap."""

    def test_config_example_is_the_contract(self):
        with open(CFG) as fh:
            self.assertEqual(json.load(fh), CONTRACT_CFG)
        self.assertEqual(size.DEFAULTS["charges"], td.CHARGE_RATES)

    def test_71_shares_bound_by_the_position_cap(self):
        s = size.size_trade(plan(), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 71)
        self.assertEqual(s["bound_by"], "position cap")
        self.assertEqual(s["risk_per_share"], 10.8)
        self.assertEqual(s["tick"], 0.1)
        self.assertEqual(s["cost"], 74564.20)
        self.assertEqual((s["max_loss"], s["charges_at_stop"], s["max_loss_net"]), (766.80, 50.01, 816.81))
        self.assertEqual(s["reward_risk"], 1.0)
        self.assertEqual(s["cash_buffer"], 10000)
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 87), ("position cap", 71), ("max order", 71), ("cash", 85)])
        self.assertEqual([l["name"] for l in s["limits"] if l["binds"]], ["position cap"])
        self.assertIsNone(s["refusal"])

    def test_the_max_loss_counts_charges(self):
        # 88 shares lose 950.40 + 56.33 = 1,006.73 at the stop: one too many for Rs 1,000
        self.assertEqual(size.shares_for_loss(1000, 10.8, 1050.2, 1039.4, td.CHARGE_RATES), 87)
        self.assertEqual(size.loss_at_stop(87, 10.8, 1050.2, 1039.4, td.CHARGE_RATES)["total"], 995.56)
        self.assertEqual(size.loss_at_stop(88, 10.8, 1050.2, 1039.4, td.CHARGE_RATES)["total"], 1006.73)
        # without charges it would be 92
        self.assertEqual(size.shares_for(1000, 10.8), 92)

    def test_the_smart_order(self):
        o = size.smart_order(size.size_trade(plan(), cfg(), today=TODAY), "3432")
        why = o.pop("why")
        self.assertEqual(o, {
            "symbol": "TATACONSUM", "security_id": "3432", "exchange": "NSE", "segment": "EQUITY",
            "product": "INTRADAY", "txn_type": "BUY", "order_type": "MARKET", "validity": "DAY", "qty": 71,
            "sl_trigger_price": 1039.4, "sl_limit_price": 1036.8, "tgt_trigger_price": 1061.0, "tgt_limit_price": 1061.1,
            "remarks": "day-trader-india/orb/2026-11-03",
            "plan": {"entry": 1050.2, "stop": 1039.4, "target": 1061.0, "tick": 0.1, "risk_per_share": 10.8,
                     "max_loss": 766.8, "charges_at_stop": 50.01, "max_loss_net": 816.81, "setup": "orb",
                     "entry_time": "10:00", "flat_by": "15:15"}})
        self.assertIn("bound by your position cap", why)
        self.assertIn("Rs 816.81", why)
        self.assertIsInstance(o["qty"], int)
        for k in ("sl_trigger_price", "sl_limit_price", "tgt_trigger_price", "tgt_limit_price"):
            self.assertIsInstance(o[k], float)

    def test_the_fixture_orders_file_is_what_it_writes(self):
        with open(os.path.join(FIX, "trader-risk", "orders-tataconsum.json")) as fh:
            fixture = json.load(fh)["orders"][0]
        self.assertEqual(size.smart_order(size.size_trade(plan(), cfg(), today=TODAY), "3432"), fixture)
        self.assertEqual(size.security_id_for("TATACONSUM", MASTER), "3432")
        self.assertIsNone(size.security_id_for("NOSUCH", MASTER))

    def test_the_stop_leg_limit_sits_on_the_tick_below_the_trigger(self):
        s = size.size_trade(plan(), cfg(stop_limit_buffer_pct=0), today=TODAY)
        o = size.smart_order(s)
        self.assertEqual(o["sl_limit_price"], 1039.3)             # never equal: one tick under
        s = size.size_trade(plan(), cfg(stop_limit_buffer_pct=1.0), today=TODAY)
        self.assertEqual(size.smart_order(s)["sl_limit_price"], 1029.0)   # 1039.40 x 0.99 = 1029.006 -> down to 1029.00

    def test_a_limit_order(self):
        o = size.smart_order(size.size_trade(plan(), cfg(), limit=1050.2, today=TODAY))
        self.assertEqual((o["order_type"], o["limit_price"], o["qty"]), ("LIMIT", 1050.2, 71))
        s = size.size_trade(plan(), cfg(), limit=1050.00, today=TODAY)
        self.assertEqual((s["risk_per_share"], s["qty"], s["max_loss_net"]), (10.6, 71, 802.61))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), limit=1050.23, today=TODAY)
        self.assertIn("not on TATACONSUM's 0.10 tick (try 1050.20)", str(e.exception))

    def test_cli_writes_one_smart_order(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--master", MASTER, "--now", NOW, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("Size: 71 shares of TATACONSUM = Rs 74,564.20 at 1050.20", text)
            self.assertIn("71 x 10.80 = Rs 766.80, plus Rs 50.01 charges = Rs 816.81", text)
            self.assertIn("brokerage Rs 20, STT Rs 18.45", text)
            self.assertIn("<- binds", text)
            self.assertIn("stop-loss trigger 1039.40 (limit 1036.80), target trigger 1061.00 (limit 1061.10)", text)
            self.assertIn("not financial advice", text)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual((doc["status"], doc["paper"], doc["broker"], doc["currency"]),
                             ("proposed", True, "indstocks", "INR"))
            self.assertEqual(doc["created_at"], "2026-11-03T10:01:00+05:30")
            self.assertEqual(len(doc["orders"]), 1)
            self.assertEqual(doc["orders"][0]["security_id"], "3432")
            self.assertEqual(doc["orders"][0]["plan"]["max_loss_net"], 816.81)

    def test_cli_json_and_limit(self):
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--portfolio", "/nonexistent", "--now", NOW,
                             "--limit", "--json"])
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(out["sizing"]["qty"], 71)
        self.assertEqual(out["orders"][0]["limit_price"], 1050.2)
        self.assertIsNone(out["orders"][0]["security_id"])


class PlanRules(unittest.TestCase):
    def test_the_max_loss_decides_it_with_a_wider_stop(self):
        s = size.size_trade(plan(stop=1030.0, target=1070.4), cfg(), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (47, "max loss"))
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 47), ("position cap", 71), ("max order", 71), ("cash", 85)])
        self.assertEqual((s["max_loss"], s["charges_at_stop"], s["max_loss_net"], s["cost"]), (949.4, 40.96, 990.36, 49359.4))
        self.assertIn("bound by your Rs 1,000 max loss per trade", size.smart_order(s)["why"])

    def test_zero_shares_is_a_plain_refusal(self):
        s = size.size_trade(plan(stop=10.0, target=3000.0), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("one share risks Rs 1,040.20 (entry 1050.20 minus stop 10.00) plus Rs 23.67 charges = Rs 1,063.87",
                      s["refusal"])
        self.assertIn("more than your Rs 1,000 max loss per trade", s["refusal"])
        self.assertIsNone(size.smart_order(s))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "plan.json")
            with open(p, "w") as fh:
                json.dump(plan(stop=10.0, target=3000.0), fh)
            out = os.path.join(d, "orders.json")
            code, text, _ = run(["--plan", p, "--config", CFG, "--portfolio", os.path.join(d, "none"),
                                 "--now", NOW, "--write", out])
            self.assertEqual(code, 1)
            self.assertIn("No order:", text)
            self.assertFalse(os.path.exists(out))

    def test_each_limit_can_bind(self):
        self.assertEqual(size.size_trade(plan(), cfg(max_order_inr=20000), today=TODAY)["bound_by"], "max order")
        s = size.size_trade(plan(), cfg(max_position_pct=20), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"]), (19, "position cap"))
        s = size.size_trade(plan(), cfg(), cash=20000, today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["cash_available"]), (9, "cash", 10000))
        s = size.size_trade(plan(), cfg(max_loss_per_trade_inr=500), today=TODAY)
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss_net"]), (42, "max loss", 492.82))

    def test_a_fresh_higher_price_means_a_bigger_risk(self):
        s = size.size_trade(plan(), cfg(min_reward_risk=0), price=1058.0, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"]), (1058.0, "price", 18.6))
        self.assertEqual((s["qty"], s["bound_by"], s["max_loss_net"], s["reward_risk"]), (51, "max loss", 991.2, 0.16))
        self.assertIsNone(s["refusal"])
        s = size.size_trade(plan(), cfg(), price=1058.0, today=TODAY)
        self.assertIn("reward:risk 0.16 (target 1061.00, entry 1058.00, stop 1039.40) is under your minimum 1",
                      s["refusal"])
        self.assertIsNone(size.smart_order(s))
        lower = size.size_trade(plan(), cfg(), price=1040.0, today=TODAY)
        self.assertEqual((lower["entry"], lower["qty"]), (1050.2, 71))

    def test_prices_file_is_used_only_when_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump(prices_doc(1058.0), fh)
            base = ["--plan", PLAN, "--config", CFG, "--portfolio", os.path.join(d, "none"), "--prices", pp, "--json"]
            code, text, _ = run(base + ["--now", "2026-11-03 10:30"])
            out = json.loads(text)
            self.assertEqual((code, out["sizing"]["qty"], out["orders"]), (1, 51, []))
            self.assertIn("above the plan's entry", out["notes"][0])
            self.assertIn("as of 10:28 IST", out["notes"][0])
            out = json.loads(run(base + ["--now", "2026-11-03 11:30"])[1])
            self.assertEqual(out["sizing"]["qty"], 71)
            self.assertIn("not used", out["notes"][0])
            # the same quote stamped in UTC reads the same
            with open(pp, "w") as fh:
                json.dump(prices_doc(1058.0, "2026-11-03T04:58:00Z"), fh)
            out = json.loads(run(base + ["--now", "2026-11-03 10:30"])[1])
            self.assertEqual(out["sizing"]["qty"], 51)

    def test_the_portfolio_cash_caps_the_size(self):
        with tempfile.TemporaryDirectory() as d:
            pf = os.path.join(d, "portfolio.json")
            with open(pf, "w") as fh:
                json.dump({"cash": 20000, "positions": []}, fh)
            out = json.loads(run(["--plan", PLAN, "--config", CFG, "--portfolio", pf, "--now", NOW, "--json"])[1])
            self.assertEqual((out["sizing"]["qty"], out["sizing"]["bound_by"]), (9, "cash"))

    def test_bad_plans(self):
        for p, word in ((plan(stop=1051.0), "not below the entry"),
                        (plan(target=1040.0), "already happened"),
                        (plan(target=None), "positive target"),
                        (plan(side="sell"), "long (buy) day trades only"),
                        (plan(symbol=""), "no symbol"),
                        (plan(stop=1039.45), "stop 1039.45 is not on TATACONSUM's 0.10 tick")):
            with self.assertRaises(size.SizeError) as e:
                size.size_trade(p, cfg(), today=TODAY)
            self.assertIn(word, str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), price=1062.0, today=TODAY)
        self.assertIn("already happened", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(blocked=["tataconsum"]), today=TODAY)
        self.assertIn("blocked list", str(e.exception))
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(plan(), cfg(), today="2026-11-04")
        self.assertIn("make a new plan", str(e.exception))
        s = size.size_trade(plan(stop=900.0), cfg(), today=TODAY)
        self.assertEqual(s["qty"], 6)
        self.assertIn("reward:risk 0.07", s["refusal"])

    def test_a_plan_without_a_tick_uses_the_price_band(self):
        p = plan()
        del p["tick"]
        self.assertEqual(size.size_trade(p, cfg(), today=TODAY)["tick"], 0.1)
        p = plan(symbol="IDEA", entry=12.26, stop=12.0, target=12.6)
        del p["tick"]
        self.assertEqual(size.size_trade(p, cfg(), today=TODAY)["tick"], 0.01)

    def test_under_the_minimum_order(self):
        cheap = plan(symbol="IDEA", entry=12.26, stop=12.0, target=12.6, tick=0.01)
        s = size.size_trade(cheap, cfg(max_loss_per_trade_inr=30), today=TODAY)
        self.assertEqual((s["qty"], s["cost"]), (24, 294.24))
        self.assertIn("under your Rs 500 minimum order", s["refusal"])
        self.assertIsNone(size.smart_order(s))

    def test_missing_plan_and_weights_only_flags(self):
        code, _, err = run(["--plan", "/nonexistent/plan.json", "--config", CFG, "--now", NOW])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--top", "2"])
        self.assertEqual(code, 1)
        self.assertIn("--weights", err)
        code, _, err = run(["--plan", PLAN, "--config", CFG, "--now", "tomorrow"])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)


class WeightsMode(unittest.TestCase):
    """A five-stock split: Rs 100,000, 10% cash, 25% cap, whole shares at the Nov 4 2026 closes."""

    @classmethod
    def setUpClass(cls):
        cls.prices = closes()

    def test_the_closes(self):
        self.assertEqual(self.prices, {"HDFCBANK": 736.7, "ICICIBANK": 1424.1, "RELIANCE": 1311.0, "INFY": 1128.2,
                                       "TCS": 2436.0})

    def check_example(self, r):
        self.assertEqual(shares(r), {"HDFCBANK": 33, "ICICIBANK": 16, "RELIANCE": 14, "INFY": 13, "TCS": 4})
        self.assertEqual(rupees(r)["HDFCBANK"], 24311.1)
        self.assertEqual(r["cash_buffer"], 10000)
        self.assertEqual(r["invested"], 89861.3)
        self.assertEqual(r["idle"], 138.7)
        self.assertEqual(r["cap"], 25000)
        self.assertEqual([p["symbol"] for p in r["positions"] if p["capped"]], ["HDFCBANK"])

    def test_from_the_example_file(self):
        self.check_example(size.size(rows(TARGETS), split(), prices=self.prices))

    def test_from_inline_weights(self):
        w = size.parse_weights(size.weights_from_arg("HDFCBANK=30,ICICIBANK=25,RELIANCE=20,INFY=15,TCS=10"))
        self.check_example(size.size(w, split(), prices=self.prices))

    def test_orders_are_post_order_shaped(self):
        r = size.size(rows(TARGETS), split(), prices=self.prices)
        orders = size.orders_from(r, "a split")
        self.assertEqual(orders[0], {"symbol": "HDFCBANK", "security_id": None, "exchange": "NSE", "segment": "EQUITY",
                                     "product": "CNC", "txn_type": "BUY", "order_type": "MARKET", "validity": "DAY",
                                     "qty": 33, "why": orders[0]["why"]})
        self.assertIn("capped at 25%", orders[0]["why"])
        self.assertEqual([o["qty"] for o in orders], [33, 16, 14, 13, 4])

    def test_a_wider_cap(self):
        r = size.size(rows(TARGETS), cfg(), prices=self.prices)     # the example config: 75 % cap
        self.assertEqual(shares(r), {"HDFCBANK": 36, "ICICIBANK": 16, "RELIANCE": 13, "INFY": 12, "TCS": 4})
        self.assertEqual((r["invested"], r["idle"]), (89632.2, 367.8))
        self.assertEqual([p["symbol"] for p in r["positions"] if p["capped"]], [])

    def test_cap_redistributes_until_nobody_is_over(self):
        w = size.parse_weights({"A": 60, "B": 30, "C": 10})
        r = size.size(w, {"budget": 1000, "cash_buffer_pct": 0, "max_position_pct": 40, "min_order_inr": 1},
                      prices={"A": 1.0, "B": 1.0, "C": 1.0})
        self.assertEqual(rupees(r), {"A": 400, "B": 400, "C": 200})
        self.assertEqual(sorted(p["symbol"] for p in r["positions"] if p["capped"]), ["A", "B"])

    def test_everything_capped_leaves_money_idle(self):
        w = size.parse_weights({"HDFCBANK": 50, "TCS": 50})
        r = size.size(w, split(), prices=self.prices)
        self.assertEqual(shares(r), {"HDFCBANK": 33, "TCS": 10})
        self.assertEqual(r["idle"], 41328.9)
        self.assertIn("Left idle Rs 41,328.90", size.report(r, size.orders_from(r)))

    def test_slices_under_the_minimum_are_dropped_and_shared_out(self):
        w = size.parse_weights({"A": 90, "B": 7, "C": 3})
        r = size.size(w, {"budget": 10000, "cash_buffer_pct": 10, "max_position_pct": 100, "min_order_inr": 500},
                      prices={"A": 1.0, "B": 1.0, "C": 1.0})
        self.assertNotIn("C", rupees(r))
        self.assertEqual(sum(rupees(r).values()), 9000)
        self.assertTrue(all(v >= 500 for v in rupees(r).values()))
        self.assertEqual(r["dropped"][0]["symbol"], "C")

    def test_blocked_symbols_are_dropped_first(self):
        r = size.size(rows(TARGETS), split(blocked=["tcs"]), prices=self.prices)
        self.assertNotIn("TCS", rupees(r))
        self.assertIn("blocked", r["dropped"][0]["reason"])

    def test_top_n(self):
        r = size.size(rows(TARGETS), split(max_position_pct=100), prices=self.prices, top=2)
        self.assertEqual(sorted(rupees(r)), ["HDFCBANK", "ICICIBANK"])

    def test_whole_shares_need_prices(self):
        with self.assertRaises(size.SizeError) as e:
            size.size(rows(TARGETS), split())
        self.assertIn("need a price for: HDFCBANK, ICICIBANK", str(e.exception))
        with self.assertRaises(size.SizeError):
            size.size(rows(TARGETS), split(), prices={"HDFCBANK": 736.7})

    def test_no_budget_and_duplicates_are_errors(self):
        with self.assertRaises(size.SizeError):
            size.size(rows(TARGETS), {"cash_buffer_pct": 10}, prices=self.prices)
        with self.assertRaises(size.SizeError):
            size.size([{"symbol": "TCS", "name": "", "weight": 1}, {"symbol": "TCS", "name": "", "weight": 2}], split(),
                      prices=self.prices)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(split(), fh)
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"as_of": "2026-11-04T18:00:00+05:30", "prices": self.prices}, fh)
            out = os.path.join(d, "trader", "orders.json")
            code, text, _ = run(["--targets", TARGETS, "--config", c, "--prices", pp, "--write", out])
            self.assertEqual(code, 0)
            self.assertIn("left idle", text)
            self.assertIn("require_stop", text)
            self.assertIn("BUY 33 HDFCBANK, CNC, MARKET, DAY", text)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertEqual([o["symbol"] for o in doc["orders"]], ["HDFCBANK", "ICICIBANK", "RELIANCE", "INFY", "TCS"])
            code, text, _ = run(["--targets", TARGETS, "--config", c, "--prices", pp, "--json"])
            self.assertEqual(json.loads(text)["result"]["idle"], 138.7)
            code, _, err = run(["--targets", "/nonexistent/targets.json", "--config", c])
            self.assertEqual(code, 1)
            self.assertIn("no weights file", err)
            code, _, err = run(["--targets", TARGETS, "--config", c])
            self.assertEqual(code, 1)
            self.assertIn("need a price", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-budget", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-budget\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("71 shares", "rs 74,564.20", "rs 816.81", "rs 50.01", "stt", "stamp duty", "gst", "whole shares",
                     "not financial advice", "/trader-risk", "15:20", "fully funded"):
            self.assertIn(must, low)
        for never in ("—", "alpaca", "yahoo", "pro" + "fit", "$5", "dollar", "new york"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()

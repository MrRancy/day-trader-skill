import datetime
import gzip
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("trader-risk", "check")
size = load("trader-budget", "size")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-tataconsum.json")
TARGETS = os.path.join(ROOT, "trader-budget", "targets.example.json")
DAILY = os.path.join(FIX, "trader-scanner", "daily-2026-11-04.json.gz")
NOW = "2026-11-03 10:01"
TODAY = "2026-11-03"
QUOTE_TIME = datetime.datetime(2026, 11, 3, 10, 28)   # a 1,058.00 quote at 10:28 IST
# a long-term split's rules with every day-trade check switched off: the long-term checks still hold
SPLIT = {"budget": 100000, "cash_buffer_pct": 10, "max_position_pct": 25, "max_order_inr": 25000,
         "min_order_inr": 500, "max_orders_per_day": 10, "require_stop": False, "no_entries_before": None,
         "no_entries_after": None, "max_trades_per_day": 0, "max_daily_loss_inr": 0, "max_loss_per_trade_inr": 0,
         "min_reward_risk": 0, "product": "CNC"}


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def wide(**over):
    """The example config with the cap, the max order and the cash buffer lifted: the loss rule alone."""
    return cfg(max_position_pct=100, max_order_inr=100000, cash_buffer_pct=0, **over)


def split(**over):
    c = dict(SPLIT)
    c.update(over)
    return c


def tc(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def smart(sym, qty, entry, stop, target, tick=0.05):
    return {"symbol": sym, "security_id": None, "exchange": "NSE", "segment": "EQUITY", "product": "INTRADAY",
            "txn_type": "BUY", "order_type": "MARKET", "validity": "DAY", "qty": qty,
            "sl_trigger_price": stop, "sl_limit_price": round(stop - tick, 2),
            "tgt_trigger_price": target, "tgt_limit_price": round(target + tick, 2),
            "plan": {"entry": entry, "stop": stop, "target": target, "tick": tick}}


def buy(sym, qty, product="CNC"):
    return {"symbol": sym, "exchange": "NSE", "segment": "EQUITY", "product": product, "txn_type": "BUY",
            "order_type": "MARKET", "validity": "DAY", "qty": qty}


def closes():
    with gzip.open(DAILY, "rt", encoding="utf-8") as fh:
        d = json.load(fh)
    return dict((s, d["bars"][s][-1]["c"]) for s in ("HDFCBANK", "ICICIBANK", "RELIANCE", "INFY", "TCS"))


def split_orders():
    with open(TARGETS) as fh:
        rows = size.parse_weights(json.load(fh))
    return size.orders_from(size.size(rows, split(), prices=closes()))


def placed(sym, side="BUY", day=TODAY, max_loss=816.81, kind=None, status="placed"):
    r = {"symbol": sym, "txn_type": side, "status": status, "date": day, "at": day + "T10:05:00+05:30"}
    if max_loss is not None:
        r["max_loss_net"] = max_loss
    if kind:
        r["kind"] = kind
    return r


def verdicts(res):
    return [r["verdict"] for r in res["results"]]


def reasons(res, i=0):
    return " | ".join(res["results"][i]["reasons"])


def warnings(res, i=0):
    return " | ".join(res["results"][i]["warnings"])


class TataConsum(unittest.TestCase):
    def test_the_smart_order_passes_every_check(self):
        res = check.check([tc()], cfg(), now=at(NOW))
        r = res["results"][0]
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["reasons"], [])
        self.assertEqual(r["warnings"], [])
        self.assertEqual((r["kind"], r["entry_ref"], r["stop"], r["target"]), ("entry", 1050.2, 1039.4, 1061.0))
        self.assertEqual((r["risk_per_share"], r["max_loss"], r["charges"], r["max_loss_net"], r["reward_risk"]),
                         (10.8, 766.8, 50.01, 816.81, 1.0))
        self.assertEqual(r["value_inr"], 74564.2)
        self.assertEqual(res["cash_after"], 25435.8)
        self.assertEqual(res["date"], TODAY)
        self.assertIs(res["paper"], True)

    def test_one_share_over_the_cap(self):
        res = check.check([tc(qty=72)], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["BLOCK"])
        self.assertEqual(res["results"][0]["reasons"],
                         ["size: Rs 75,614.40 is over your Rs 75,000 max order",
                          "position: TATACONSUM would be Rs 75,614.40, 75.6% of the Rs 100,000 account, over your 75% cap"])

    def test_the_loss_rule_counts_charges(self):
        res = check.check([tc(qty=88)], wide(), now=at(NOW))
        self.assertEqual(res["results"][0]["reasons"],
                         ["loss: 88 shares x 10.80 risk a share = Rs 950.40 at the stop, plus Rs 56.33 charges = "
                          "Rs 1,006.73, over your Rs 1,000 max loss per trade"])
        res = check.check([tc(qty=87)], wide(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["results"][0]["max_loss_net"], 995.56)

    def test_fingerprint_covers_the_indstocks_fields_only(self):
        o = tc()
        self.assertEqual(check.fingerprint(dict(o, why="anything", plan={}, remarks="x")), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, qty=72)), check.fingerprint(o))
        self.assertNotEqual(check.fingerprint(dict(o, sl_trigger_price=1039.0)), check.fingerprint(o))
        self.assertEqual(len(check.fingerprint(o)), 16)

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "state", "risk.json")
            none = os.path.join(d, "none")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", none,
                                   "--prices", none, "--state", none, "--now", NOW, "--out", out])
            text = buf.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("PASS  #1 BUY TATACONSUM 71 shares, INTRADAY, entry", text)
            self.assertIn("entry 1050.20 (the plan's entry), stop 1039.40, target 1061.00: risk 10.80 a share", text)
            self.assertIn("max loss Rs 766.80 + Rs 50.01 charges = Rs 816.81 (limit Rs 1,000), reward:risk 1.00 (min 1)", text)
            self.assertIn("today before this: 0 of 3 trades, Rs 0 of Rs 3,000 daily loss", text)
            self.assertIn("1 passed, 0 blocked at 2026-11-03 10:01 IST. Cash after the passed orders: Rs 25,435.80", text)
            self.assertIn("not financial advice", text)
            with open(out) as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertEqual(rec["results"][0]["kind"], "entry")
            self.assertEqual(rec["results"][0]["fingerprint"], check.fingerprint(tc()))
            self.assertTrue(rec["checked_at"].endswith("+05:30"))


class DayTradeChecks(unittest.TestCase):
    def one(self, order, now=NOW, **over):
        return check.check([order], cfg(**over), now=at(now))

    def test_a_stop_is_required(self):
        o = tc()
        for k in ("sl_trigger_price", "sl_limit_price", "tgt_trigger_price", "tgt_limit_price", "plan"):
            o.pop(k)
        o["plan"] = {"entry": 1050.2}
        res = self.one(o)
        self.assertIn("stop: no stop", reasons(res))
        res = self.one(o, require_stop=False, min_reward_risk=0)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("no stop", warnings(res))

    def test_a_plan_stop_counts_but_warns(self):
        o = tc()
        for k in ("sl_trigger_price", "sl_limit_price", "tgt_trigger_price", "tgt_limit_price"):
            o.pop(k)
        res = self.one(o)
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("in the plan only", warnings(res))
        self.assertEqual(res["results"][0]["max_loss_net"], 816.81)

    def test_stop_and_target_on_the_right_side(self):
        self.assertIn("must be below the entry 1050.20", reasons(self.one(tc(sl_trigger_price=1051.0, sl_limit_price=1050.0))))
        res = self.one(tc(tgt_trigger_price=1050.0, tgt_limit_price=1050.1))
        self.assertIn("target: the target 1050.00 must be above the entry 1050.20", reasons(res))

    def test_reward_risk(self):
        res = self.one(tc(tgt_trigger_price=1056.0, tgt_limit_price=1056.1))
        self.assertIn("reward:risk: 0.54", reasons(res))
        self.assertEqual(verdicts(self.one(tc(tgt_trigger_price=1056.0, tgt_limit_price=1056.1), min_reward_risk=0.5)), ["PASS"])

    def test_a_fresh_higher_price_is_the_entry(self):
        res = check.check([tc()], cfg(), prices={"TATACONSUM": 1058.0}, now=at("2026-11-03 10:30"),
                          price_times={"TATACONSUM": QUOTE_TIME})
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (1058.0, "price", 18.6))
        self.assertEqual(len(r["reasons"]), 4)
        self.assertIn("loss: 71 shares x 18.60 risk a share = Rs 1,320.60 at the stop, plus Rs 50.05 charges = Rs 1,370.65",
                      reasons(res))
        self.assertIn("reward:risk: 0.16", reasons(res))
        self.assertIn("size: Rs 75,118 is over", reasons(res))

    def test_a_stale_or_future_price_is_not_used(self):
        for now in ("2026-11-03 11:30", "2026-11-03 10:01"):
            res = check.check([tc()], cfg(), prices={"TATACONSUM": 1058.0}, now=at(now),
                              price_times={"TATACONSUM": QUOTE_TIME})
            self.assertEqual(verdicts(res), ["PASS"], now)
            self.assertEqual(res["results"][0]["entry_ref"], 1050.2)
            self.assertIn("older than 15 minutes", warnings(res))

    def test_a_lower_price_does_not_shrink_the_risk(self):
        res = check.check([tc()], cfg(), prices={"TATACONSUM": 1040.00}, now=at(NOW))
        self.assertEqual(res["results"][0]["entry_ref"], 1050.2)

    def test_a_limit_order_uses_its_limit_price(self):
        o = tc(order_type="LIMIT", limit_price=1050.0)
        res = check.check([o], cfg(), prices={"TATACONSUM": 1058.0}, now=at(NOW))
        r = res["results"][0]
        self.assertEqual((r["entry_ref"], r["max_loss_net"], r["verdict"]), (1050.0, 802.61, "PASS"))
        self.assertIn("needs a limit_price", reasons(self.one(tc(order_type="LIMIT"))))
        self.assertIn("limit_price on a MARKET order is ignored", warnings(self.one(tc(limit_price=1050.0))))

    def test_daily_loss_and_trades_per_day(self):
        two = {"orders": [placed("HDFCBANK"), placed("INFY")]}
        res = check.check([tc()], cfg(), state=two, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertIn("today before this: 2 of 3 trades, Rs 1,633.62 of Rs 3,000 daily loss", res["results"][0]["info"][-1])
        three = {"orders": [placed("HDFCBANK"), placed("INFY"), placed("RELIANCE")]}
        res = check.check([tc()], cfg(), state=three, now=at(NOW))
        why = reasons(res)
        self.assertIn("trades: 3 entries placed today", why)
        self.assertIn("daily loss: Rs 2,450.43 already at risk today plus Rs 816.81 for this one = Rs 3,267.24, over your Rs 3,000",
                      why)
        yesterday = {"orders": [placed(s, day="2026-11-02") for s in ("HDFCBANK", "INFY", "RELIANCE")]}
        self.assertEqual(verdicts(check.check([tc()], cfg(), state=yesterday, now=at(NOW))), ["PASS"])
        old_style = {"orders": [dict(placed("HDFCBANK"), max_loss_net=None, max_loss=800.0)]}
        res = check.check([tc()], cfg(), state=old_style, now=at(NOW))
        self.assertEqual(res["today"]["loss_at_risk"], 800.0)

    def test_exits_and_declines_do_not_count_as_trades(self):
        st = {"orders": [placed("HDFCBANK"), placed("HDFCBANK", side="SELL", kind="exit", max_loss=None),
                         placed("INFY", status="declined"), placed("RELIANCE", status="failed")]}
        res = check.check([tc()], cfg(), state=st, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS"])
        self.assertEqual(res["today"]["entries_placed"], 1)

    def test_the_batch_counts_toward_the_daily_limits(self):
        orders = [smart(s, 2, 500.0, 300.0, 900.0) for s in ("AAA", "BBB", "CCC", "DDD")]   # 423.84 each at the stop
        res = check.check(orders, cfg(max_daily_loss_inr=1300), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "PASS", "BLOCK"])
        why = reasons(res, 3)
        self.assertIn("trades: 0 entries placed today plus 3 passed earlier", why)
        self.assertIn("daily loss: Rs 1,271.52 already at risk today plus Rs 423.84 for this one = Rs 1,695.36", why)
        self.assertEqual((res["results"][0]["max_loss"], res["results"][0]["max_loss_net"]), (400.0, 423.84))

    def test_entry_window_ist(self):
        cases = [("2026-11-03 09:29", "no new entries before 09:30 IST (it is 09:29)"),
                 ("2026-11-03 15:01", "no new entries after 15:00 IST (it is 15:01)"),
                 ("2026-11-03 09:10", "the market is closed (Tue 2026-11-03 09:10"),
                 ("2026-11-03 15:30", "the market is closed"),
                 ("2026-11-07 10:00", "the market is closed today (weekend"),
                 ("2026-11-10 10:00", "the market is closed today (NSE holiday: Diwali-Balipratipada"),
                 ("2026-11-08 18:30", "2026-11-08 is the Muhurat session"),
                 ("2026-11-03T03:50:00Z", "no new entries before 09:30 IST (it is 09:20)")]
        for now, want in cases:
            res = self.one(tc(), now=now)
            self.assertIn("window: " + want, reasons(res), now)
        for now in ("2026-11-03 09:30", "2026-11-03 15:00", "2026-11-03T04:40:00Z", "2026-06-01 12:00"):
            self.assertEqual(verdicts(self.one(tc(), now=now)), ["PASS"], now)
        res = self.one(tc(), now="2026-11-07 10:00", no_entries_before=None, no_entries_after=None)
        self.assertEqual(verdicts(res), ["PASS"])
        with self.assertRaises(check.RiskError):
            self.one(tc(), no_entries_before="9.30am")
        self.assertIn("nseindia.com", " ".join(self.one(tc(), now="2027-01-04 10:00")["warnings"]))

    def test_the_window_uses_ist_not_the_machine(self):
        self.assertEqual(at("2026-11-03 10:00"), datetime.datetime(2026, 11, 3, 10, 0))
        self.assertEqual(at("2026-11-03T04:30:00Z"), datetime.datetime(2026, 11, 3, 10, 0))
        self.assertEqual(check.parse_ts("2026-11-03T10:28:00+05:30"), QUOTE_TIME)
        self.assertEqual(check.parse_ts("2026-11-03T04:58:00Z"), QUOTE_TIME)

    def test_a_short_entry_when_allowed(self):
        short = tc(txn_type="SELL", sl_trigger_price=1061.0, sl_limit_price=1061.1, tgt_trigger_price=1039.4,
                   tgt_limit_price=1039.3)
        res = self.one(short)
        self.assertIn("short: selling more TATACONSUM than you hold", reasons(res))
        res = self.one(short, allow_short=True)
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"], r["max_loss"], r["reward_risk"]), ("PASS", "entry", 766.8, 1.0))
        res = self.one(dict(short, sl_trigger_price=1045.0, sl_limit_price=1045.1), allow_short=True)
        self.assertIn("must be above the entry 1050.20 for a SELL", reasons(res))

    def test_smart_order_format(self):
        self.assertIn("no notional", reasons(self.one(dict(tc(), notional="70000"))))
        self.assertIn("whole shares", reasons(self.one(tc(qty=71.5))))
        self.assertIn("qty must be a positive whole number", reasons(self.one(tc(qty=0))))
        self.assertIn("validity must be DAY", reasons(self.one(tc(validity="IOC"))))
        self.assertIn("unknown order_type 'STOP'", reasons(self.one(tc(order_type="STOP"))))
        self.assertIn("product MARGIN is for derivatives", reasons(self.one(tc(product="MARGIN"))))
        self.assertIn("unknown product", reasons(self.one(tc(product="NRML"))))
        o = tc()
        o.pop("sl_limit_price")
        self.assertIn("needs sl_limit_price", reasons(self.one(o)))
        o = tc()
        o.pop("tgt_limit_price")
        self.assertIn("needs tgt_limit_price", reasons(self.one(o)))
        self.assertIn("stop-loss limit 1040.00 must sit at or below its trigger 1039.40",
                      reasons(self.one(tc(sl_limit_price=1040.0))))
        self.assertIn("target limit 1060.90 must sit at or above its trigger 1061.00",
                      reasons(self.one(tc(tgt_limit_price=1060.9))))
        self.assertIn("sl_trigger_price 1039.45 is not on TATACONSUM's 0.1 tick",
                      reasons(self.one(tc(sl_trigger_price=1039.45, sl_limit_price=1039.0))))
        self.assertIn("as a number", warnings(self.one(tc(sl_trigger_price="1039.40"))))
        self.assertIn("as an integer", warnings(self.one(tc(qty="71"))))
        self.assertIn("a TRIGGER order needs a trigger_price", reasons(self.one(tc(order_type="TRIGGER"))))
        trig = tc(order_type="TRIGGER", trigger_price=1052.0, trigger_limit_price=1052.2)
        res = self.one(trig, min_reward_risk=0)
        self.assertEqual((res["results"][0]["verdict"], res["results"][0]["entry_ref"]), ("PASS", 1052.2))
        self.assertIn("takes trigger_limit_price, not limit_price", reasons(self.one(dict(trig, limit_price=1052.0))))
        self.assertIn("after-market order", warnings(self.one(tc(is_amo=True))))

    def test_fno_and_other_assets(self):
        opt = {"symbol": "NIFTY26NOV24000CE", "exchange": "NSE", "segment": "DERIVATIVE", "product": "MARGIN",
               "txn_type": "BUY", "order_type": "LIMIT", "limit_price": 120.0, "validity": "DAY", "qty": 75,
               "plan": {"entry": 120.0, "stop": 100.0, "target": 160.0}}
        res = self.one(opt)
        self.assertIn("asset: NIFTY26NOV24000CE is an F&O contract", reasons(res))
        res = self.one(opt, allow_fno=True)
        self.assertNotIn("asset:", reasons(res))
        self.assertIn("F&O order", warnings(res))
        fut = dict(opt, symbol="RELIANCE26NOVFUT")
        self.assertIn("F&O contract", reasons(self.one(fut)))
        gold = dict(buy("GOLDM26DECFUT", 1), exchange="MCX", segment="COMMODITY", product="MARGIN")
        self.assertIn("commodity", reasons(self.one(gold)))
        self.assertIn("does not look like an NSE symbol", reasons(self.one(buy("BTC/INR", 1))))
        self.assertIn("format: no symbol", reasons(self.one(buy("", 1))))
        self.assertIn("txn_type must be BUY or SELL", reasons(self.one(dict(buy("TCS", 1), txn_type="HOLD"))))


class Exits(unittest.TestCase):
    PF = {"cash": 25435.8, "positions": [{"symbol": "TATACONSUM", "shares": 71,
                                          "lots": [{"date": TODAY, "shares": 71, "cost": 74564.2}]}]}
    SELL = {"symbol": "TATACONSUM", "exchange": "NSE", "segment": "EQUITY", "product": "INTRADAY", "txn_type": "SELL",
            "order_type": "MARKET", "validity": "DAY", "qty": 71}

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        full = {"orders": [placed("TATACONSUM"), placed("INFY"), placed("RELIANCE")] +
                          [placed("T%d" % i, max_loss=1) for i in range(6)] +
                          [placed("TATACONSUM", side="SELL", kind="exit", max_loss=None)]}
        c = cfg(blocked=["TATACONSUM"], max_order_inr=1000, max_daily_loss_inr=500)
        res = check.check([self.SELL], c, portfolio=self.PF, state=full, now=at("2026-11-03 15:10"))
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"), r["reasons"])
        why = warnings(res)
        for word in ("blocked:", "duplicate:", "daily limit:"):
            self.assertIn("exit, not blocked: " + word, why)
        self.assertNotIn("price", why)
        res = check.check([self.SELL], cfg(), portfolio=self.PF, now=at("2026-11-07 12:00"))
        self.assertEqual(verdicts(res), ["PASS"])

    def test_a_partial_exit_and_the_rest(self):
        half = dict(self.SELL, qty=35)
        res = check.check([half, half], cfg(), portfolio=self.PF, prices={"TATACONSUM": 1055.0}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual([r["kind"] for r in res["results"]], ["exit", "exit"])
        self.assertIn("exit, not blocked: duplicate", warnings(res, 1))
        res = check.check([half, half, half], cfg(), portfolio=self.PF, prices={"TATACONSUM": 1055.0}, now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS", "BLOCK"])
        self.assertIn("short:", reasons(res, 2))

    def test_format_asset_and_short_still_apply_to_a_sell(self):
        res = check.check([dict(self.SELL, order_type="LIMIT")], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("needs a limit_price", reasons(res))
        res = check.check([dict(self.SELL, qty=72)], cfg(), portfolio=self.PF, now=at(NOW))
        self.assertIn("short: selling more TATACONSUM than you hold (71 shares)", reasons(res))
        res = check.check([self.SELL], cfg(), now=at(NOW))
        self.assertIn("record the fill first", reasons(res))

    def test_buy_then_sell_in_one_batch(self):
        res = check.check([tc(), self.SELL], cfg(), now=at(NOW))
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        self.assertEqual(res["results"][1]["kind"], "exit")


class LongTermChecks(unittest.TestCase):
    """The split's long-term checks hold with the day-trade checks off."""

    @classmethod
    def setUpClass(cls):
        cls.prices = closes()
        cls.orders = split_orders()

    def one(self, order, **over):
        prices = dict(self.prices, IDEA=12.26)
        return check.check([order], split(**over), prices=prices, today=TODAY)["results"][0]

    def test_the_five_buys_pass_on_a_fresh_account(self):
        res = check.check(self.orders, split(), prices=self.prices, today=TODAY)
        self.assertEqual(verdicts(res), ["PASS"] * 5)
        self.assertEqual(res["cash_after"], 10138.7)
        self.assertEqual(res["results"][0]["value_inr"], 24311.1)
        self.assertEqual(res["results"][0]["amount"], "33 shares")
        self.assertEqual(res["results"][0]["product"], "CNC")

    def test_a_sixth_buy_is_blocked_three_ways(self):
        res = check.check(self.orders + [buy("HDFCBANK", 10)], split(), prices=self.prices, today=TODAY)
        self.assertEqual(verdicts(res), ["PASS"] * 5 + ["BLOCK"])
        why = reasons(res, 5)
        self.assertIn("cash:", why)
        self.assertIn("position:", why)
        self.assertIn("duplicate:", why)

    def test_the_v2_config_blocks_buys_without_a_stop(self):
        res = check.check(self.orders, cfg(), prices=self.prices, now=at(NOW))
        self.assertEqual(verdicts(res)[0], "BLOCK")
        self.assertIn("stop: no stop", reasons(res))

    def test_max_and_min_order(self):
        self.assertIn("over your Rs 25,000 max order", " ".join(self.one(buy("TCS", 11), max_position_pct=100)["reasons"]))
        self.assertIn("under your Rs 500 minimum", " ".join(self.one(buy("IDEA", 1))["reasons"]))

    def test_blocked_list(self):
        r = self.one(buy("TCS", 1), blocked=["TCS"])
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("blocked list", r["reasons"][0])

    def test_share_orders_need_a_price(self):
        q = buy("ZZZZ", 2)
        self.assertIn("no price for ZZZZ", " ".join(self.one(q)["reasons"]))
        r = self.one(buy("TCS", 2))
        self.assertEqual(r["verdict"], "PASS")
        self.assertEqual(r["value_inr"], 4872.0)

    def test_margin(self):
        r = check.check([buy("TCS", 5)], split(cash_buffer_pct=0, max_position_pct=100),
                        portfolio={"cash": 10000, "positions": []}, prices=self.prices, today=TODAY)["results"][0]
        self.assertIn("margin", " ".join(r["reasons"]))
        self.assertIn("MIS leverage is off", " ".join(r["reasons"]))

    PF = {"cash": 5000.0, "positions": [{"symbol": "TCS", "shares": 4,
                                         "lots": [{"date": "2026-11-02", "shares": 4, "cost": 9744.0}]}]}

    def test_short_sales_are_blocked(self):
        sell = dict(buy("TCS", 5), txn_type="SELL")
        r = check.check([sell], split(), portfolio=self.PF, prices=self.prices, today=TODAY)["results"][0]
        self.assertIn("short:", " ".join(r["reasons"]))
        sell["qty"] = 3
        r = check.check([sell], split(), portfolio=self.PF, prices=self.prices, today=TODAY)["results"][0]
        self.assertEqual(r["verdict"], "PASS")

    def test_a_sell_frees_cash_for_a_later_buy(self):
        orders = [dict(buy("TCS", 4), txn_type="SELL"), buy("INFY", 10)]
        res = check.check(orders, split(max_position_pct=100), portfolio=self.PF, prices=self.prices, today=TODAY)
        self.assertEqual(verdicts(res), ["PASS", "PASS"])
        res = check.check([buy("INFY", 10)], split(max_position_pct=100), portfolio=self.PF, prices=self.prices,
                          today=TODAY)
        self.assertEqual(verdicts(res), ["BLOCK"])

    def test_daily_limit_and_already_placed_today(self):
        st = {"orders": [{"symbol": "T%d" % i, "txn_type": "BUY", "status": "placed", "at": TODAY + "T10:00:00+05:30"}
                         for i in range(10)]}
        res = check.check([buy("TCS", 1)], split(), prices=self.prices, state=st, today=TODAY)
        self.assertIn("daily limit", reasons(res))
        res = check.check([buy("TCS", 1)], split(), prices=self.prices, state=st, today="2026-11-04")
        self.assertEqual(verdicts(res), ["PASS"])
        tcs = {"orders": [{"symbol": "TCS", "side": "buy", "status": "placed", "at": TODAY + "T10:00:00+05:30"}]}
        res = check.check([buy("TCS", 1)], split(), prices=self.prices, state=tcs, today=TODAY)
        self.assertIn("already placed today", reasons(res))
        declined = {"orders": [{"symbol": "TCS", "txn_type": "BUY", "status": "declined", "at": TODAY + "T10:00:00+05:30"}]}
        self.assertEqual(verdicts(check.check([buy("TCS", 1)], split(), prices=self.prices, state=declined, today=TODAY)),
                         ["PASS"])

    def test_live_account_warns(self):
        res = check.check([buy("TCS", 1)], split(paper=False), prices=self.prices, today=TODAY)
        self.assertIn("LIVE", res["warnings"][0])


class Cli(unittest.TestCase):
    def test_prices_file_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"as_of": "2026-11-03T10:30:00+05:30", "prices": {"TATACONSUM": 1058.0},
                           "detail": {"TATACONSUM": {"time": "2026-11-03T10:28:00+05:30",
                                                     "fetched": "2026-11-03T10:30:00+05:30"}}}, fh)
            none = os.path.join(d, "none")
            base = ["--orders", ORDERS_PATH, "--config", CFG_PATH, "--portfolio", none, "--prices", pp,
                    "--state", none, "--no-write"]
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(check.main(base + ["--now", "2026-11-03 10:30"]), 1)
            self.assertIn("71 shares x 18.60 risk a share = Rs 1,320.60", buf.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(check.main(base + ["--now", "2026-11-03 11:30"]), 0)

    def test_split_orders_with_a_split_config_file(self):
        with tempfile.TemporaryDirectory() as d:
            c = os.path.join(d, "config.json")
            with open(c, "w") as fh:
                json.dump(split(), fh)
            pp = os.path.join(d, "prices.json")
            with open(pp, "w") as fh:
                json.dump({"as_of": "2026-11-04T18:00:00+05:30", "prices": closes()}, fh)
            orders = os.path.join(d, "orders.json")
            with open(orders, "w") as fh:
                json.dump({"orders": split_orders()}, fh)
            none = os.path.join(d, "none")
            out = os.path.join(d, "state", "risk.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = check.main(["--orders", orders, "--config", c, "--portfolio", none, "--prices", pp,
                                   "--state", none, "--today", TODAY, "--out", out])
            self.assertEqual(code, 0)
            self.assertIn("5 passed, 0 blocked", buf.getvalue())
            with open(out) as fh:
                rec = json.load(fh)
            self.assertEqual(rec["date"], TODAY)
            self.assertTrue(all(len(r["fingerprint"]) == 16 for r in rec["results"]))
            with open(orders, "w") as fh:
                json.dump({"orders": split_orders() + [buy("HDFCBANK", 10)]}, fh)
            with redirect_stdout(io.StringIO()):
                code = check.main(["--orders", orders, "--config", c, "--portfolio", none, "--prices", pp,
                                   "--state", none, "--today", TODAY, "--no-write"])
            self.assertEqual(code, 1)

    def test_errors_are_one_line(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", "/nonexistent.json", "--no-write"]), 1)
        self.assertIn("no orders file", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", ORDERS_PATH, "--now", "noon", "--no-write"]), 1)
        self.assertIn("--now wants", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(check.main(["--orders", ORDERS_PATH, "--today", "monday", "--no-write"]), 1)
        self.assertIn("--today wants", err.getvalue())


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-risk\n"))
        self.assertGreater(len(text.splitlines()), 60)
        low = " ".join(text.lower().split())
        for must in ("rs 816.81", "rs 1,006.73", "rs 995.56", "charges", "muhurat", "exit", "fingerprint",
                     "no pattern-day-trader rule", "not financial advice", "15:20"):
            self.assertIn(must, low)
        for never in ("—", "alpaca", "yahoo", "pro" + "fit", "$5", "dollar", "new york", "pdt_guard"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()

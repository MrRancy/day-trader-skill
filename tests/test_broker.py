import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")
td = ticket.trader_data

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-tataconsum.json")
PLAN_PATH = os.path.join(FIX, "trader-budget", "plan-tataconsum.json")
NOW = "2026-11-03 10:01"
TODAY = "2026-11-03"
HDFC = {"symbol": "HDFCBANK", "exchange": "NSE", "segment": "EQUITY", "product": "CNC", "txn_type": "BUY",
        "order_type": "MARKET", "validity": "DAY", "qty": 33}
BATCH = ["HDFCBANK", "ICICIBANK", "RELIANCE", "INFY", "TCS"]
TC_BODY = {"txn_type": "BUY", "exchange": "NSE", "segment": "EQUITY", "product": "INTRADAY", "order_type": "MARKET",
           "validity": "DAY", "security_id": "3432", "qty": 71, "sl_trigger_price": 1039.4, "sl_limit_price": 1036.8,
           "tgt_trigger_price": 1061.0, "tgt_limit_price": 1061.1, "remarks": "day-trader-india/orb/2026-11-03"}
TC_PLAIN = ("BUY 71 TATACONSUM, INTRADAY (MIS, squared off from about 15:20), market, good for today / "
            "stop-loss trigger 1039.40 (limit 1036.80) / target trigger 1061.00 (limit 1061.10) / "
            "max loss about Rs 816.81, charges included, if the stop fills at 1039.40 (it can fill worse in a fast market)")


def tc(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=HDFC, batch=BATCH):
        return ticket.parse_yes(reply, order, batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes HDFCBANK", "yes 33 HDFCBANK",
                  "yes 33", "haan"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "go", "do it", "yeah", "yep", "absolutely", "send it", "theek hai",
                  "yes to all", "yes, all of them", "yes to the rest", "yes both", "yes sab",
                  "yes ICICIBANK", "yes 34", "yes but make it 30", "yes?", "no", "not yes",
                  "yes, wait", "yes if it is under 730", "yes nahi"):
            self.assertFalse(self.yes(r), r)

    def test_amount_must_match_this_order(self):
        sell = {"symbol": "TCS", "txn_type": "SELL", "qty": 3, "order_type": "MARKET"}
        self.assertTrue(self.yes("yes sell 3 TCS", sell))
        self.assertFalse(self.yes("yes sell 4", sell))

    def test_a_smart_order_yes(self):
        o = tc()
        for r in ("yes", "yes 71 TATACONSUM", "yes 1039.40", "yes max loss 816.81", "yes target 1061", "yes 1050.20"):
            self.assertTrue(self.yes(r, o, ["TATACONSUM"]), r)
        for r in ("yes 72", "yes 70 TATACONSUM", "yes but stop at 1038", "yes, stop 1039.40", "yes M&M", "sure"):
            self.assertFalse(self.yes(r, o, ["TATACONSUM", "M&M"]), r)


class TicketShape(unittest.TestCase):
    def test_api_body_is_the_indstocks_call(self):
        self.assertEqual(ticket.api_body(tc()), TC_BODY)
        self.assertEqual(ticket.api_path(tc()), "/smart/order")
        self.assertEqual(ticket.api_body(dict(HDFC, why="x", plan={})),
                         {"txn_type": "BUY", "exchange": "NSE", "segment": "EQUITY", "product": "CNC",
                          "order_type": "MARKET", "validity": "DAY", "qty": 33})
        self.assertEqual(ticket.api_path(HDFC), "/order")
        body = ticket.api_body({"symbol": "TCS", "txn_type": "BUY", "qty": "3", "order_type": "LIMIT", "limit_price": "2436.5"})
        self.assertEqual((body["qty"], body["limit_price"], body["validity"]), (3, 2436.5, "DAY"))
        self.assertNotIn("symbol", body)

    def test_fingerprint_is_shared_with_trader_risk(self):
        o = tc()
        self.assertEqual(ticket.fingerprint(o), check.fingerprint(o))
        self.assertEqual(ticket.INDSTOCKS_FIELDS, check.INDSTOCKS_FIELDS)
        self.assertEqual(ticket.fingerprint(dict(o, why="anything", plan={}, remarks="x")), ticket.fingerprint(o))
        self.assertNotEqual(ticket.fingerprint(dict(o, qty=72)), ticket.fingerprint(o))

    def test_plain_english(self):
        self.assertEqual(ticket.plain(HDFC), "BUY 33 HDFCBANK, CNC (delivery), market, good for today")
        self.assertEqual(ticket.plain({"symbol": "TCS", "txn_type": "SELL", "qty": 1, "order_type": "LIMIT",
                                       "limit_price": 2450, "product": "INTRADAY"}),
                         "SELL 1 TCS, INTRADAY (MIS, squared off from about 15:20), limit 2450.00, good for today")
        self.assertEqual(ticket.plain(tc()), TC_PLAIN)
        lim = tc(order_type="LIMIT", limit_price=1050.0)
        self.assertEqual(ticket.plain(lim).split(" / ")[0],
                         "BUY 71 TATACONSUM, INTRADAY (MIS, squared off from about 15:20), limit 1050.00, good for today")
        self.assertEqual(ticket.max_loss(lim), (802.61, 1039.4))     # 71 x 10.60 + 50.01 charges
        self.assertEqual(ticket.max_loss(tc()), (816.81, 1039.4))     # the plan's own number
        no_plan = dict(TC_BODY, symbol="TATACONSUM")
        self.assertEqual(ticket.max_loss(no_plan), (None, 1039.4))

    def test_one_leg_charges(self):
        self.assertEqual(td.charges_leg(71, 1050.50, "buy")["total"], 16.92)
        self.assertEqual(td.charges_leg(71, 1061.00, "sell")["total"], 33.54)
        self.assertEqual(td.charges_leg(71, 1039.40, "sell")["total"], 33.10)
        buy, sell = td.charges_leg(71, 1050.20, "buy")["total"], td.charges_leg(71, 1039.40, "sell")["total"]
        self.assertAlmostEqual(buy + sell, td.charges(71, 1050.20, 1039.40)["total"], delta=0.02)
        self.assertEqual(td.charges_leg(0, 100, "buy")["total"], 0.0)


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.paper = os.path.join(self.d, "paper.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def place_args(self, *extra, **kw):
        return self.args("place", *extra, **kw) + ["--paper", self.paper, "--portfolio", self.pf, "--no-quote"]

    def tick(self, price, now):
        return run(["paper", "tick", "--price", "TATACONSUM=%s" % price, "--paper", self.paper, "--portfolio", self.pf,
                    "--config", self.cfg, "--state", self.state, "--no-quote", "--now", now])

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--state", self.state, "--now", now, "--out", self.risk])

    def state_doc(self):
        with open(self.state) as fh:
            return json.load(fh)

    def paper_doc(self):
        with open(self.paper) as fh:
            return json.load(fh)

    def pf_doc(self):
        with open(self.pf) as fh:
            return json.load(fh)

    def set_cfg(self, **over):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c.update(over)
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)

    def set_order(self, **over):
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0].update(over)
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("not ready", err)
        code, _, err = run(self.place_args("--index", "1", "--price", "1050.50"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        self.assertFalse(os.path.exists(self.paper))

    def test_the_full_paper_flow_to_the_target(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER ledger (trader/paper.json), INDstocks", text)
        for part in TC_PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("READY: trader-risk PASS", text)
        self.assertIn("The paper ledger will simulate POST /smart/order", text)
        self.assertIn('"sl_trigger_price": 1039.4', text)
        self.assertNotIn('"plan"', text)
        self.assertNotIn('"why"', text)
        self.assertNotIn('"symbol"', text)
        self.assertIn("permission prompt", text)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        self.assertIn("Not recorded", text)
        self.assertFalse(os.path.exists(self.state))

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("ticket.py place --index 1", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("APPROVED", err)

        code, text, _ = run(self.place_args("--index", "1", "--price", "1050.50", now="2026-11-03 10:02"))
        self.assertEqual(code, 0)
        self.assertIn("Placed on the paper ledger: PAPER-EQ-1 with GTT child PAPER-GTT-2, last price 1050.50 from --price", text)
        self.assertIn("BUY 71 TATACONSUM at 1050.50, charges Rs 16.92. Ledger cash Rs 25,397.58", text)
        self.assertIn("Nothing real was sent", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        appr, plc, fil = st["orders"]
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss_net"], appr["date"]), ("yes", "entry", 816.81, TODAY))
        self.assertEqual(appr["at"], "2026-11-03T10:01:00+05:30")
        self.assertEqual((plc["broker_order_id"], plc["child_order_id"], plc["max_loss_net"], plc["kind"], plc["paper"]),
                         ("PAPER-EQ-1", "PAPER-GTT-2", 816.81, "entry", True))
        self.assertEqual((fil["qty"], fil["price"], fil["charges"], fil["broker_order_id"]), (71, 1050.5, 16.92, "PAPER-EQ-1"))
        pf = self.pf_doc()
        self.assertEqual((pf["cash"], pf["positions"][0]["shares"], pf["charges_paid"]), (25397.58, 71, 16.92))
        pd = self.paper_doc()
        self.assertEqual((pd["cash"], pd["positions"]["TATACONSUM"]["qty"], pd["positions"]["TATACONSUM"]["avg_price"]),
                         (25397.58, 71, 1050.5))
        self.assertEqual([o["status"] for o in pd["orders"]], ["SUCCESS", "PENDING"])

        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: placed", text)
        code, text, _ = self.tick(1055, "2026-11-03 11:00")
        self.assertEqual(code, 0)
        self.assertIn("nothing moved", text)
        self.assertIn("open leg PAPER-GTT-2 for TATACONSUM: stop-loss 1039.40, target 1061.00 (71 shares)", text)
        code, text, _ = self.tick(1061.2, "2026-11-03 12:30")
        self.assertEqual(code, 0)
        self.assertIn("PAPER-GTT-2: SELL 71 TATACONSUM at 1061.00 (target leg filled at its trigger (last 1061.20)), "
                      "charges Rs 33.54. Ledger cash Rs 100,695.04", text)
        self.assertIn("no open positions", text)
        pd = self.paper_doc()
        self.assertEqual((pd["cash"], pd["charges_paid"], pd["realized"], pd["positions"]), (100695.04, 50.46, 745.5, {}))
        self.assertEqual(pd["orders"][1]["leg"], "target")
        self.assertEqual((self.pf_doc()["cash"], self.pf_doc()["positions"]), (100695.04, []))
        last = self.state_doc()["orders"][-1]
        self.assertEqual((last["status"], last["txn_type"], last["kind"], last["price"], last["charges"]),
                         ("filled", "SELL", "exit", 1061.0, 33.54))

        code, text, _ = run(["log", "--state", self.state, "--now", NOW])
        self.assertIn("1 placed today", text)
        self.assertIn("max loss Rs 816.81", text)
        self.assertIn("charges Rs 33.54", text)
        # trader-risk now counts it: a second TATACONSUM buy today is a duplicate, and Rs 816.81 is used
        res = check.check([tc()], {"budget": 100000}, state=st, now=check.parse_now("2026-11-03 10:30"))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))
        self.assertEqual((res["today"]["entries_placed"], res["today"]["loss_at_risk"]), (1, 816.81))

    def test_the_stop_leg(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.place_args("--index", "1", "--price", "1050.50", now="2026-11-03 10:02"))
        code, text, _ = self.tick(1039.0, "2026-11-03 11:00")
        self.assertIn("PAPER-GTT-2: SELL 71 TATACONSUM at 1039.40 (stop-loss leg filled at its trigger (last 1039.00)), "
                      "charges Rs 33.10. Ledger cash Rs 99,161.88", text)
        pd = self.paper_doc()
        self.assertEqual((pd["cash"], pd["realized"], pd["orders"][1]["leg"]), (99161.88, -788.1, "stop-loss"))
        self.assertEqual(self.pf_doc()["cash"], 99161.88)

    def test_the_square_off_and_the_close(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.place_args("--index", "1", "--price", "1050.50", now="2026-11-03 10:02"))
        code, text, _ = self.tick(1048.0, "2026-11-03 15:19")
        self.assertIn("nothing moved", text)
        code, text, _ = self.tick(1048.0, "2026-11-03 15:21")
        self.assertIn("PAPER-GTT-2: cancelled: MIS square-off", text)
        self.assertIn("PAPER-SQ-3: SELL 71 TATACONSUM at 1048.00 (MIS square-off by the broker at the quoted price)", text)
        pd = self.paper_doc()
        self.assertEqual(pd["positions"], {})
        self.assertEqual([o["status"] for o in pd["orders"]], ["SUCCESS", "CANCELLED", "SUCCESS"])
        self.assertEqual(self.state_doc()["orders"][-1]["note"], "MIS square-off by the broker at the quoted price")
        # a square-off with no price says so and leaves the position
        run(self.args("decline", "--index", "1", "--reply", "no"))
        out, _, _ = run(["paper", "tick", "--paper", self.paper, "--portfolio", self.pf, "--config", self.cfg,
                         "--state", self.state, "--no-quote", "--now", "2026-11-03 15:31"])

    def test_a_bare_price_ticks_the_only_open_symbol(self):
        code, text, err = run(["paper", "tick", "--price", "1055", "--paper", self.paper, "--portfolio", self.pf,
                               "--config", self.cfg, "--state", self.state, "--no-quote", "--now", "2026-11-03 10:30"])
        self.assertNotEqual(code, 0)
        self.assertIn("nothing is open on the paper ledger", text + err)
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.place_args("--index", "1", "--price", "1050.50", now="2026-11-03 10:02"))
        code, text, _ = run(["paper", "tick", "--price", "1055", "--paper", self.paper, "--portfolio", self.pf,
                             "--config", self.cfg, "--state", self.state, "--no-quote", "--now", "2026-11-03 10:30"])
        self.assertEqual(code, 0, text)
        self.assertIn("nothing moved (prices: TATACONSUM 1055.00)", text)
        code, text, _ = run(["paper", "tick", "--price", "1061.20", "--paper", self.paper, "--portfolio", self.pf,
                             "--config", self.cfg, "--state", self.state, "--no-quote", "--now", "2026-11-03 11:10"])
        self.assertIn("SELL 71 TATACONSUM at 1061.00 (target leg filled", text)
        sell = next(o for o in self.paper_doc()["orders"] if o["txn_type"] == "SELL" and o["status"] == "SUCCESS")
        self.assertEqual((sell["pnl"], sell["avg_cost"]), (745.5, 1050.5))     # 71 x (1061.00 - 1050.50), for the journal
        code, text, err = run(["paper", "tick", "--price", "abc", "--paper", self.paper, "--portfolio", self.pf,
                               "--config", self.cfg, "--state", self.state, "--no-quote", "--now", "2026-11-03 11:11"])
        self.assertNotEqual(code, 0)
        self.assertIn("nothing is open", text + err)                # the position is closed by now

    def test_a_square_off_without_a_price_waits(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.place_args("--index", "1", "--price", "1050.50", now="2026-11-03 10:02"))
        code, text, _ = run(["paper", "tick", "--paper", self.paper, "--portfolio", self.pf, "--config", self.cfg,
                             "--state", self.state, "--no-quote", "--now", "2026-11-03 15:25"])
        self.assertIn("square-off due but no price for TATACONSUM: give --price TATACONSUM=P", text)
        self.assertEqual(self.paper_doc()["positions"]["TATACONSUM"]["qty"], 71)

    def test_a_limit_order_waits_then_fills_and_expires(self):
        self.set_order(order_type="LIMIT", limit_price=1049.0)
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        code, text, _ = run(self.place_args("--index", "1", "--price", "1051.00", now="2026-11-03 10:02"))
        self.assertEqual(code, 0)
        self.assertIn("PAPER-EQ-1: pending: the last price 1051.00 is above the limit 1049.00", text)
        self.assertEqual([r["status"] for r in self.state_doc()["orders"]], ["approved", "placed"])
        self.assertFalse(os.path.exists(self.pf))
        code, text, _ = self.tick(1049.5, "2026-11-03 10:10")
        self.assertIn("nothing moved", text)
        self.assertIn("pending PAPER-EQ-1: BUY 71 TATACONSUM limit 1049.00", text)
        code, text, _ = self.tick(1048.5, "2026-11-03 10:20")
        self.assertIn("PAPER-EQ-1: BUY 71 TATACONSUM at 1048.50 (limit order filled at the quoted price)", text)
        self.assertEqual(self.paper_doc()["positions"]["TATACONSUM"]["avg_price"], 1048.5)
        self.assertEqual(self.pf_doc()["positions"][0]["shares"], 71)
        # now the legs are live: the target
        code, text, _ = self.tick(1061.0, "2026-11-03 10:30")
        self.assertIn("target leg filled", text)
        # a pending limit that never fills expires at the close (straight on the ledger: trader-risk would
        # call a second buy today a duplicate)
        with open(self.cfg) as fh:
            cfg = json.load(fh)
        ledger = ticket.Paper(self.paper, cfg)
        parent, child, events = ledger.place(tc(order_type="LIMIT", limit_price=1000.0), 1050.0,
                                             ticket.parse_now("2026-11-03 14:01"))
        self.assertEqual((parent["status"], child["status"], events[0]["price"]), ("PENDING", "PENDING", None))
        got = ledger.tick({"TATACONSUM": 1050.0}, ticket.parse_now("2026-11-03 15:30"))
        self.assertEqual([e["what"] for e in got], ["expired at the close", "expired at the close"])
        self.assertEqual([o["status"] for o in ledger.doc["orders"][-2:]], ["EXPIRED", "EXPIRED"])

    def test_flatten_by_the_close(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.place_args("--index", "1", "--price", "1050.50", now="2026-11-03 10:02"))
        late = "2026-11-03 15:10"
        code, text, _ = run(["flatten", "--symbol", "TATACONSUM", "--orders", self.orders, "--portfolio", self.pf,
                             "--paper", self.paper, "--plan", PLAN_PATH, "--config", self.cfg, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("SELL 71 TATACONSUM, INTRADAY (MIS, squared off from about 15:20), market, good for today (flat by 15:15 IST)", text)
        self.assertIn("replaced 1 earlier proposal", text)
        self.assertIn("cancels the open stop-loss and target legs", text)
        with open(self.orders) as fh:
            doc = json.load(fh)
        self.assertEqual(ticket.api_body(doc["orders"][0]),
                         {"txn_type": "SELL", "exchange": "NSE", "segment": "EQUITY", "product": "INTRADAY",
                          "order_type": "MARKET", "validity": "DAY", "security_id": "3432", "qty": 71})
        self.assertEqual(self.run_risk(now=late), 0)       # after the 15:00 entry cutoff: an exit still passes
        with open(self.risk) as fh:
            self.assertEqual(json.load(fh)["results"][0]["kind"], "exit")
        code, text, _ = run(self.args("show", now=late))
        self.assertEqual(code, 0)
        self.assertIn("READY", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes sell 71 TATACONSUM", now=late))[0], 0)
        code, text, _ = run(self.place_args("--index", "1", "--price", "1052.30", now="2026-11-03 15:11"))
        self.assertEqual(code, 0)
        self.assertIn("PAPER-GTT-2: cancelled: the position is being sold", text)
        self.assertIn("PAPER-EQ-3: SELL 71 TATACONSUM at 1052.30, charges Rs 33.36. Ledger cash Rs 100,077.52", text)
        self.assertEqual((self.pf_doc()["cash"], self.pf_doc()["positions"]), (100077.52, []))
        self.assertEqual(self.paper_doc()["positions"], {})
        code, _, err = run(["flatten", "--symbol", "TATACONSUM", "--orders", self.orders, "--portfolio", self.pf,
                            "--paper", self.paper, "--now", late])
        self.assertEqual(code, 1)
        self.assertIn("nothing to flatten", err)

    def test_flatten_with_the_broker_qty(self):
        code, text, _ = run(["flatten", "--symbol", "tataconsum", "--qty", "71", "--orders", self.orders,
                             "--portfolio", self.none, "--paper", self.none, "--plan", self.none, "--config", self.cfg,
                             "--now", NOW])
        self.assertEqual(code, 0)
        self.assertNotIn("Check it against the broker", text)
        self.assertEqual(run(["flatten", "--symbol", "TATACONSUM", "--qty", "0", "--orders", self.orders])[0], 1)

    def test_an_edited_order_is_not_ready(self):
        self.run_risk()
        self.set_order(qty=72)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("was not in the last risk check", text)

    def test_yesterdays_check_does_not_count(self):
        self.run_risk(now="2026-11-02 10:01")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.assertEqual(self.run_risk(now="2026-11-03 09:20"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-11-03 09:20"))
        self.assertIn("BLOCKED by trader-risk: window: no new entries before 09:30", text)
        code, _, _ = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-11-03 09:20"))
        self.assertEqual(code, 1)

    def test_result_needs_an_approval_and_decline_is_logged(self):
        self.run_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertEqual(code, 1)
        self.assertIn("no yes recorded", err)
        code, text, _ = run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        self.assertEqual(code, 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: declined", text)

    def test_result_records_a_fill_the_user_made_elsewhere(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "EQ-93586788",
                                      "--filled-qty", "71", "--filled-price", "1050.60", "--portfolio", self.pf,
                                      now="2026-11-03 10:03"))
        self.assertEqual(code, 0)
        self.assertIn("charges Rs 16.92", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        self.assertEqual(st["orders"][1]["broker_order_id"], "EQ-93586788")
        self.assertEqual(self.pf_doc()["cash"], round(100000 - 71 * 1050.60 - 16.92, 2))

    def test_live_needs_paper_false_and_the_flag(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        code, _, err = run(self.place_args("--index", "1", "--live", "--price", "1050.50"))
        self.assertEqual(code, 0)                              # paper config: --live is ignored, the ledger fills it
        shutil.rmtree(os.path.dirname(self.state))
        os.remove(self.pf)
        os.remove(self.paper)
        self.set_cfg(paper=False)
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        code, text, err = run(self.args("place", "--index", "1") + ["--paper", self.paper, "--portfolio", self.pf])
        self.assertEqual(code, 1)
        self.assertIn("REAL order", err)
        self.assertIn("--live", err)
        self.assertEqual([r["status"] for r in self.state_doc()["orders"]], ["approved"])

    def test_live_goes_through_the_broker_class(self):
        self.set_cfg(paper=False)
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        sent = []

        class Fake(object):
            def __init__(self, api=None):
                pass

            def place(self, order):
                sent.append(ticket.api_body(order))
                return {"order_id": "EQ-1", "order_status": "INITIATED", "child_order_id": "GTT-2"}
        old = ticket.INDstocksBroker
        ticket.INDstocksBroker = Fake
        try:
            code, text, _ = run(self.args("place", "--index", "1", "--live") + ["--paper", self.paper, "--portfolio", self.pf])
        finally:
            ticket.INDstocksBroker = old
        self.assertEqual(code, 0)
        self.assertEqual(sent, [TC_BODY])
        self.assertIn("Placed LIVE at INDstocks: order EQ-1 (INITIATED), GTT child GTT-2", text)
        plc = self.state_doc()["orders"][-1]
        self.assertEqual((plc["status"], plc["broker_order_id"], plc["child_order_id"], plc["paper"]),
                         ("placed", "EQ-1", "GTT-2", False))
        self.assertFalse(os.path.exists(self.paper))

        class Refuses(object):
            def __init__(self, api=None):
                pass

            def place(self, order):
                raise td.DataError("INDstocks answered HTTP 403 for /smart/order: the token is invalid or has expired, "
                                   "or this machine's IP is not whitelisted for orders.")
        shutil.rmtree(os.path.dirname(self.state))
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        ticket.INDstocksBroker = Refuses
        try:
            code, _, err = run(self.args("place", "--index", "1", "--live") + ["--paper", self.paper, "--portfolio", self.pf])
        finally:
            ticket.INDstocksBroker = old
        self.assertEqual(code, 1)
        self.assertIn("not whitelisted", err)
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "failed")

    def test_the_broker_class_builds_the_right_calls(self):
        calls = []

        class Api(object):
            def post_json(self, path, body):
                calls.append(("POST", path, body))
                if path == "/smart/order":
                    return {"status": "success", "data": {"order_data": [{"order_id": "EQ-7", "order_status": "CREATED",
                                                                          "child_order_details": {"order_id": "GTT-8"}}]}}
                return {"status": "success", "data": {"order_id": "EQ-9", "order_status": "INITIATED"}}

            def get_json(self, path, params=None):
                calls.append(("GET", path, params))
                return {"status": "success", "data": [{"id": "EQ-7"}]}

            def resolve(self, sym):
                return "NSE_3432"
        b = ticket.INDstocksBroker(Api())
        self.assertEqual(b.place(tc()), {"order_id": "EQ-7", "order_status": "CREATED", "child_order_id": "GTT-8"})
        self.assertEqual(calls[-1], ("POST", "/smart/order", TC_BODY))
        o = dict(HDFC, security_id=None)
        self.assertEqual(b.place(o)["order_id"], "EQ-9")
        self.assertEqual(calls[-1][1], "/order")
        self.assertEqual(calls[-1][2]["security_id"], "3432")
        b.cancel("GTT-8")
        self.assertEqual(calls[-1], ("POST", "/smart/order/cancel", {"order_id": "GTT-8", "segment": "EQUITY"}))
        self.assertEqual(b.order_book(), [{"id": "EQ-7"}])
        b.positions()
        self.assertEqual(calls[-1], ("GET", "/portfolio/positions", {"segment": "equity"}))

    def test_fill_sell_relieves_the_oldest_lot(self):
        pf = {"cash": 0, "positions": [{"symbol": "TCS", "shares": 3, "lots": [
            {"id": "TCS-1", "date": "2025-01-02", "shares": 2, "cost": 4800.0},
            {"id": "TCS-2", "date": "2026-01-02", "shares": 1, "cost": 2500.0}]}]}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, text, _ = run(["fill", "--symbol", "TCS", "--side", "sell", "--qty", "2", "--price", "2436",
                             "--date", TODAY, "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 0)
        chg = td.charges_leg(2, 2436.0, "sell")["total"]
        self.assertIn("charges Rs %.2f" % chg, text)
        pf = self.pf_doc()
        self.assertEqual(pf["cash"], round(4872 - chg, 2))
        lots = pf["positions"][0]["lots"]
        self.assertEqual([l["id"] for l in lots], ["TCS-2"])
        self.assertEqual(lots[0]["shares"], 1)
        self.assertEqual(self.state_doc()["orders"][-1]["status"], "filled")
        code, _, err = run(["fill", "--symbol", "TCS", "--side", "sell", "--qty", "5", "--price", "2436",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)

    def test_paper_show_and_sync_without_a_ledger(self):
        code, text, _ = run(["paper", "show", "--paper", self.paper, "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("No paper ledger", text)
        code, text, _ = run(["sync", "--config", self.cfg])
        self.assertEqual(code, 1)
        self.assertIn("Nothing to sync", text)

    def test_kite_tickets_are_not_placed(self):
        self.set_cfg(broker="kite", paper=False)
        self.run_risk()
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("Zerodha Kite", text)
        self.assertIn("not built yet", text)
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        code, _, err = run(self.args("place", "--index", "1", "--live") + ["--paper", self.paper, "--portfolio", self.pf])
        self.assertEqual(code, 1)
        self.assertIn("only the indstocks backend", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "trader-broker", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "trader-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: trader-broker\n"))
        self.assertGreater(len(text.splitlines()), 80)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("three separate gates", "static ip", "paper ledger", "trader/paper.json", "--live", "15:20",
                     "rs 816.81", "rs 16.92", "rs 33.54", "never add it to an allow list", "tokens never go in the chat",
                     "not financial advice", "kinder than a real one"):
            self.assertIn(must, low)
        for never in ("—", "alpaca", "yahoo", "pro" + "fit", "$5", "new york"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()

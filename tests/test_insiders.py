"""trader-insiders tests. Fixtures in tests/fixtures/trader-insiders/, all read from nseindia.com on 2026-11-05:

pit-TATACONSUM.json   corporates-pit for TATACONSUM: the 20 newest insider-trading rows (ESOPs and market sales)
pit-RELIANCE.json     corporates-pit for RELIANCE: 20 rows, every kind (off-market, pledge, gift, market, others)
largedeal.json        snapshot-capital-market-largedeal as on 2026-11-04: 133 bulk, 2 block, 169 short-sale rows
bulk_deals-2026-11-04.json  historicalOR bulk deals for Nov 4 alone: 70 rows, NSE's cap (the snapshot has 133)
bulk_deals.json       historicalOR bulk deals asked for 2026-10-06 to 2026-11-05: 70 rows, all of Oct 6 (the cap again)
block_deals.json      historicalOR block deals for the same month: 46 rows, complete
"""

import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

ins = load("trader-insiders", "insiders")
DIR = os.path.join(FIX, "trader-insiders")


def doc(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ins.main(args)
    return code, out.getvalue(), err.getvalue()


class Disclosures(unittest.TestCase):
    def setUp(self):
        self.tc = ins.parse_pit(doc("pit-TATACONSUM.json"), "TATACONSUM")
        self.rel = ins.parse_pit(doc("pit-RELIANCE.json"), "RELIANCE")

    def test_an_esop_allotment_row(self):
        self.assertEqual(len(self.tc), 20)
        self.assertEqual(self.tc[0]["disclosed"], "2026-02-24T17:56:00+05:30")
        siva = next(r for r in self.tc if r["name"] == "SIVAKUMAR SIVASANKARAN" and r["disclosed"].startswith("2026-02"))
        self.assertTrue(siva["id"].startswith("pit:"))
        self.assertEqual(siva["category"], "Key Managerial Personnel")
        self.assertEqual((siva["type"], siva["mode"], siva["kind"]), ("Buy", "ESOP", "esop"))
        self.assertEqual((siva["qty"], siva["value_inr"], siva["price"]), (1736, 2019992, 1163.59))
        self.assertEqual((siva["before"], siva["after"]), (0, 1736))       # "Nil" before
        self.assertEqual((siva["traded_from"], siva["traded_to"]), ("2026-02-20", "2026-02-20"))
        self.assertEqual(siva["intimated"], "2026-02-24")
        self.assertEqual(siva["days_to_disclose"], 4)
        self.assertFalse(siva["late"])
        self.assertTrue(siva["xbrl"].startswith("https://nsearchives.nseindia.com/corporate/xbrl/"))

    def test_a_market_sale_is_tagged_market(self):
        sale = next(r for r in self.tc if r["disclosed"].startswith("2025-11-20"))
        self.assertEqual((sale["name"], sale["type"], sale["kind"]), ("KALYANARAMAN K", "Sell", "market"))
        self.assertEqual((sale["qty"], sale["price"]), (1070, 1158.18))
        self.assertEqual((sale["before"], sale["after"]), (5223, 4153))

    def test_every_kind_in_the_reliance_feed(self):
        self.assertEqual(set(r["kind"] for r in self.rel), {"off-market", "other", "pledge", "market", "gift"})
        promoter = next(r for r in self.rel if r["category"] == "Promoter Group")
        self.assertEqual((promoter["kind"], promoter["qty"], promoter["value_inr"], promoter["price"]),
                         ("off-market", 240942006, 0, None))
        gift = next(r for r in self.rel if r["kind"] == "gift")
        self.assertEqual(gift["value_inr"], 0)
        pledge = next(r for r in self.rel if r["type"] == "Pledge Revoke")
        self.assertEqual((pledge["kind"], pledge["mode"]), ("pledge", "Revokation of Pledge"))

    def test_classify(self):
        c = ins.classify
        self.assertEqual(c("Market Purchase", "Buy"), "market")
        self.assertEqual(c("Market Sale", "Sell"), "market")
        self.assertEqual(c("ESOP", "Buy"), "esop")
        self.assertEqual(c("Revokation of Pledge", "Pledge Revoke"), "pledge")
        self.assertEqual(c("Pledge Creation", "Pledge Invoke"), "pledge")
        self.assertEqual(c("Gift", "Sell"), "gift")
        self.assertEqual(c("Off Market", "Buy"), "off-market")
        self.assertEqual(c("Preferential Offer", "Buy"), "corporate")
        self.assertEqual(c("Others", "Buy"), "other")

    def test_net_market_flow(self):
        flow = ins.market_flow(self.rel, 90, datetime.date(2025, 10, 1))
        self.assertEqual((flow["buys"], flow["sells"]), (0, 2))
        self.assertEqual(flow["sold_inr"], 34625000 + 138500000)
        self.assertEqual(flow["net_inr"], -173125000)
        self.assertEqual(ins.market_flow(self.rel, 90, datetime.date(2026, 3, 1))["sells"], 0)

    def test_select(self):
        self.assertEqual(len(ins.select_pit(self.tc, since="2026-02-01")), 3)
        self.assertEqual(len(ins.select_pit(self.rel, kinds={"market"})), 8)
        self.assertEqual([r["qty"] for r in ins.select_pit(self.rel, min_value=30000000)], [25000, 100000])

    def test_late_and_odd_values(self):
        late = ins.parse_pit({"data": [{"symbol": "X", "date": "20-Feb-2026 10:00", "acqfromDt": "01-Feb-2026",
                                        "acqtoDt": "01-Feb-2026", "secAcq": "1,000", "secVal": "-",
                                        "befAcqSharesNo": "Nil", "afterAcqSharesNo": "-", "did": "1",
                                        "tdpTransactionType": "Buy", "acqMode": "Market Purchase"}]})[0]
        self.assertEqual((late["days_to_disclose"], late["late"]), (19, True))
        self.assertEqual((late["qty"], late["value_inr"], late["price"], late["before"], late["after"]),
                         (1000, None, None, 0, None))
        self.assertIn("LATE: over 7", ins.describe_pit(late))
        self.assertEqual(ins.iso("29-OCT-2026"), "2026-10-29")
        self.assertIsNone(ins.iso("-"))
        self.assertEqual(ins.iso_time("04-Nov-2026 12:51:39"), "2026-11-04T12:51:39+05:30")
        self.assertEqual((ins.money(2019992), ins.money(352500000), ins.money(85000)),
                         ("Rs 20.20 lakh", "Rs 35.25 crore", "Rs 85,000"))

    def test_render_and_cli(self):
        out = ins.render_pit("TATACONSUM", self.tc[:3], ins.market_flow(self.tc, 90, datetime.date(2026, 3, 1)), 20)
        self.assertIn("3 insider disclosures on NSE (of 20)", out)
        self.assertIn("bought 1,736 equity shares (ESOP), worth Rs 20.20 lakh at about Rs 1,163.59, "
                      "holding 0 -> 1,736, traded 2026-02-20, disclosed 4 days later", out)
        self.assertIn("Market trades by insiders in the last 90 days: none", out)
        rel = ins.parse_pit(doc("pit-RELIANCE.json"), "RELIANCE")
        out = ins.render_pit("RELIANCE", rel[:1], ins.market_flow(rel, 90, datetime.date(2025, 10, 1)), 20)
        self.assertIn("Market trades by insiders in the last 90 days: 0 buys, 2 sales, net sold Rs 17.31 crore.", out)
        code, out, _ = run(["pit", "TATACONSUM", "--offline", DIR, "--today", "2026-03-01", "--kinds", "market",
                            "--portfolio", "/nonexistent"])
        self.assertEqual(code, 0)
        self.assertIn("Not inside information", out)
        self.assertEqual(out.count("Market Sale"), 7)
        self.assertIn("7 insider disclosures on NSE (of 20)", out)
        self.assertIn("last 90 days: none.", out)                  # the sales shown are older than 90 days
        code, out, _ = run(["pit", "--symbols", "RELIANCE", "--offline", DIR, "--json", "--min-value", "1e9"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["RELIANCE"]["rows"], [])
        self.assertNotIn(chr(0x2014), out)


class Deals(unittest.TestCase):
    def test_todays_snapshot(self):
        d = ins.parse_snapshot(doc("largedeal.json"))
        self.assertEqual(d["as_on"], "2026-11-04")
        self.assertEqual((len(d["bulk"]), len(d["block"]), len(d["short"])), (133, 2, 169))
        b = d["block"][0]
        self.assertEqual((b["symbol"], b["side"], b["qty"], b["price"], b["value_inr"]),
                         ("AJMERA", "BUY", 2500000, 141, 352500000))
        self.assertEqual(b["client"], "CELLO PENS AND STATIONERY PRIVATE LIMITED")
        self.assertEqual(b["id"], "deal:block:2026-11-04:AJMERA:CELLO PENS AND STATIONERY PRIVATE LIMITED:BUY:2500000:141")
        self.assertEqual(d["bulk"][0]["company"], "Ajmera Realty & Inf I Ltd")
        s = d["short"][0]
        self.assertEqual((s["symbol"], s["side"], s["client"], s["qty"]), ("AARTIPHARM", None, None, 290))
        self.assertIn("block deal 2026-11-04: CELLO PENS AND STATIONERY PRIVATE LIMITED buy 2,500,000 shares "
                      "at Rs 141.00 (Rs 35.25 crore)", ins.describe_deal(b))

    def test_history(self):
        bulk = ins.parse_history(doc("bulk_deals-2026-11-04.json"), "bulk")
        self.assertEqual(len(bulk), 70)                                   # NSE's cap: the snapshot has 133
        self.assertTrue(all(r["kind"] == "bulk" and r["date"] == "2026-11-04" for r in bulk))
        ajmera = [r for r in bulk if r["symbol"] == "AJMERA"]
        self.assertEqual(sorted(r["price"] for r in ajmera), [139.56, 141, 141])
        block = ins.parse_history(doc("block_deals.json"), "block")
        self.assertEqual(len(block), 46)
        self.assertEqual(block[0]["date"], "2026-11-04")                  # newest first
        icici = [r for r in block if r["symbol"] == "ICICIBANK"]
        self.assertEqual(len(icici), 2)
        self.assertEqual(icici[0]["value_inr"], 1921581 * 1343)

    def test_history_is_read_a_day_at_a_time(self):
        f = ins.Fetcher(offline=DIR)
        d = ins.load_deals(f, datetime.date(2026, 11, 4), datetime.date(2026, 11, 4))
        self.assertEqual(len(d["bulk"]), 70)
        self.assertEqual(d["truncated"], ["bulk 2026-11-04"])
        # Oct 6 to Oct 10: the per-day files are missing offline, so the month files are filtered to each day
        d = ins.load_deals(f, datetime.date(2026, 10, 6), datetime.date(2026, 10, 10))
        self.assertEqual(sorted(set(r["date"] for r in d["block"])), ["2026-10-08"])   # the month's only block day there
        self.assertEqual(len(d["block"]), 10)                              # five blocks, buyer and seller each
        self.assertEqual(sorted(set(r["date"] for r in d["bulk"])), ["2026-10-06"])
        self.assertEqual(d["truncated"], ["bulk 2026-10-06"])
        with self.assertRaises(ins.InsidersError):
            ins.load_deals(f, datetime.date(2026, 1, 1), datetime.date(2026, 6, 30))
        with self.assertRaises(ins.InsidersError):
            ins.load_deals(f, datetime.date(2026, 10, 10), datetime.date(2026, 10, 6))

    def test_cli(self):
        code, out, _ = run(["deals", "--offline", DIR, "--from", "2026-10-06", "--to", "2026-10-10",
                            "--symbols", "ICICIBANK"])
        self.assertEqual(code, 0)
        self.assertIn("0 bulk, 2 block", out)
        self.assertIn("cut short: bulk 2026-10-06", out)
        self.assertIn("ICICIBANK    block deal 2026-10-08: PIONEER INVESTMENT FUND SCHEME II sell 1,921,581 shares "
                      "at Rs 1,343.00 (Rs 258.07 crore)  <- on your list", out)
        code, out, _ = run(["deals", "--offline", DIR, "--symbols", "TATACONSUM"])
        self.assertEqual(code, 1)
        code, out, _ = run(["deals", "--offline", DIR, "--all-symbols", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual((len(data["bulk"]), len(data["short"])), (133, 0))
        code, out, _ = run(["deals", "--offline", DIR, "--all-symbols", "--short"])
        self.assertIn("169 short-sale disclosures", out)
        self.assertIn("short sale disclosed in AARTIPHARM: 290 shares", out)


class NewSinceLastCheck(unittest.TestCase):
    def test_alerts_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state", "insiders.json")
            base = ["new", "--symbols", "TATACONSUM", "--offline", DIR, "--state", state,
                    "--portfolio", os.path.join(tmp, "none.json")]
            code, out, _ = run(base + ["--today", "2026-03-01"])
            self.assertEqual(code, 0)
            lines = [l for l in out.splitlines() if l.startswith("NEW ")]
            self.assertEqual(len(lines), 3)                       # first run: the last 30 days only
            self.assertTrue(all(l.startswith("NEW insider TATACONSUM 2026-02-24") for l in lines))
            self.assertIn("<- on your list", lines[0])
            self.assertIn("Not inside information", out)
            with open(state) as fh:
                saved = json.load(fh)
            self.assertEqual(len(saved["seen"]), 20)
            self.assertTrue(saved["last_check"].endswith("+05:30"))
            code, out, _ = run(base + ["--today", "2026-03-01"])
            self.assertEqual(code, 1)
            self.assertIn("Nothing new", out)

    def test_deals_for_every_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "insiders.json")
            code, out, _ = run(["new", "--symbols", "RELIANCE", "--offline", DIR, "--state", state,
                                "--today", "2026-11-05", "--all-deals", "--portfolio", os.path.join(tmp, "n.json")])
            self.assertEqual(code, 0)
            self.assertIn("NEW AJMERA block deal 2026-11-04", out)
            self.assertEqual(out.count("NEW "), 135)                # 133 bulk + 2 block; the PIT rows are old
            with open(state) as fh:
                seen = json.load(fh)["seen"]
            self.assertEqual(len(seen), 135 + 4)                    # the state keeps 400 days: 4 of the 20 PIT rows
            code, out, _ = run(["new", "--symbols", "RELIANCE", "--offline", DIR, "--state", state,
                                "--today", "2026-11-06", "--all-deals", "--portfolio", os.path.join(tmp, "n.json")])
            self.assertEqual(code, 1)                               # the pruned 2021-2023 rows do not come back

    def test_dry_run_keeps_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "insiders.json")
            run(["new", "--symbols", "TATACONSUM", "--offline", DIR, "--state", state, "--today", "2026-03-01",
                 "--dry-run", "--portfolio", os.path.join(tmp, "n.json")])
            self.assertFalse(os.path.exists(state))

    def test_new_since(self):
        rows = ins.parse_pit(doc("pit-TATACONSUM.json"), "TATACONSUM")
        state = {"seen": dict((r["id"], r["disclosed"][:10]) for r in rows[1:]), "last_check": "2026-02-24T18:00:00+05:30"}
        self.assertEqual([r["id"] for r in ins.new_since(rows, state, datetime.date(2026, 3, 1))], [rows[0]["id"]])


class YourSymbols(unittest.TestCase):
    def test_watchlist_plan_and_portfolio(self):
        with tempfile.TemporaryDirectory() as tmp:
            wl, pl, pf = (os.path.join(tmp, n) for n in ("watchlist.json", "plan.json", "portfolio.json"))
            with open(wl, "w") as fh:
                json.dump({"hits": [{"symbol": "TATACONSUM", "name": "Tata Consumer"}]}, fh)
            with open(pl, "w") as fh:
                json.dump({"symbol": "reliance"}, fh)
            with open(pf, "w") as fh:
                json.dump({"positions": [{"symbol": "INFY"}]}, fh)
            self.assertEqual(sorted(ins.watch_from_files(pf, wl, pl)), ["INFY", "RELIANCE", "TATACONSUM"])
        self.assertEqual(ins.watch_from_files("/none", "/none", "/none"), {})


if __name__ == "__main__":
    unittest.main()

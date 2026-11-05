"""trader-analyst tests. Fixtures in tests/fixtures/trader-analyst/, all read from NSE on 2026-11-05:

integrated-TATACONSUM.json   integrated-filing-results for TATACONSUM: 14 filings, Mar 2025 to Sep 2026
integrated-RELIANCE.json     the same for RELIANCE (14 filings)
integrated-HDFCBANK.json     the same for HDFCBANK, a bank (14 filings)
legacy-TATACONSUM.json       corporates-financial-results for TATACONSUM, the 2023 and 2024 rows (16 of 156)
board-meetings-TATACONSUM.json  corporate-board-meetings for TATACONSUM (the Oct 26 results meeting on top)
INTEGRATED_FILING_INDAS_*.xml.gz   TATACONSUM's seven consolidated quarterly XBRLs, Mar 2025 to Sep 2026
INDAS_*.xml.gz                     TATACONSUM's four consolidated quarterly XBRLs of 2024 (the older feed)
INTEGRATED_FILING_BANKING_*.xml.gz HDFCBANK's consolidated XBRL for the quarter ended Sep 2026
"""

import datetime
import gzip
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

res = load("trader-analyst", "results")
DIR = os.path.join(FIX, "trader-analyst")


def doc(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def xbrl(name):
    with gzip.open(os.path.join(DIR, name + ".gz"), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = res.main(args)
    return code, out.getvalue(), err.getvalue()


class TataConsumer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result, cls.meeting = res.run_symbol("TATACONSUM", res.Fetcher(offline=DIR), 6,
                                                 today=datetime.date(2026, 11, 5))

    def test_latest_quarter(self):
        q = self.result["rows"][0]
        self.assertEqual(q["end"], "2026-09-30")
        self.assertEqual(q["fiscal"], "Q2 FY27")
        self.assertEqual(q["revenue"], 56611300000)
        self.assertEqual(q["net_profit"], 4701400000)
        self.assertEqual(q["eps_diluted"], 4.74)
        self.assertEqual(q["revenue_yoy"], 14.0)       # vs 4,965.9 crore a year earlier
        self.assertEqual(q["net_profit_yoy"], 15.7)    # vs 406.5 crore
        self.assertEqual(q["eps_yoy"], 15.9)           # vs 4.09
        self.assertEqual(q["ebitda"], 6322800000 + 257000000 + 1638100000)
        self.assertEqual((q["ebitda_margin"], q["pbt_margin"], q["net_margin"]), (14.5, 11.2, 8.3))
        self.assertEqual(q["revenue_qoq"], 5.8)
        self.assertEqual(q["exceptional"], -73700000)
        self.assertFalse(q["audited"])
        self.assertEqual(q["filed"], "2026-10-26T18:46:29+05:30")
        self.assertEqual(q["concepts"]["revenue"], "RevenueFromOperations")
        self.assertEqual(self.result["basis"], "consolidated")
        self.assertEqual(self.result["name"], "TATA CONSUMER PRODUCTS LIMITED")
        self.assertEqual(self.result["notes"], [])

    def test_six_quarters_with_fiscal_labels(self):
        self.assertEqual([r["fiscal"] for r in self.result["rows"]],
                         ["Q2 FY27", "Q1 FY27", "Q4 FY26", "Q3 FY26", "Q2 FY26", "Q1 FY26"])
        self.assertEqual(self.result["quarters_known"], 10)        # four more, for the growth figures
        q4 = self.result["rows"][2]
        self.assertTrue(q4["audited"])
        self.assertEqual((q4["revenue"], q4["revenue_yoy"]), (54336200000, 17.9))
        self.assertTrue(all(r["revenue_yoy"] is not None for r in self.result["rows"]))

    def test_trailing_twelve_months(self):
        t = self.result["ttm"]
        self.assertEqual(t["revenue"], 215556300000)
        self.assertEqual(t["net_profit"], 17058700000)
        self.assertEqual(t["eps_diluted"], 17.17)

    def test_what_changed_in_words(self):
        lines = res.changes(self.result)
        self.assertIn("revenue Rs 5,661.13 crore (+14.0% vs a year earlier)", lines[0])
        self.assertIn("diluted EPS Rs 4.74 (+15.9%)", lines[0])
        self.assertIn("(Q2 FY27, unaudited, consolidated)", lines[0])
        self.assertTrue(any(l.startswith("EBITDA margin 14.5% (-0.4 points") for l in lines))
        self.assertTrue(any(l.startswith("Exceptional items of Rs -7.37 crore") for l in lines))
        self.assertTrue(any(l.startswith("Revenue +5.8% vs the quarter before") for l in lines))

    def test_next_results_from_board_meetings(self):
        self.assertFalse(self.meeting["upcoming"])
        self.assertEqual(self.meeting["date"], "2026-10-26")
        meetings = res.parse_meetings(doc("board-meetings-TATACONSUM.json"))
        m = res.next_results(meetings, datetime.date(2026, 10, 20))
        self.assertTrue(m["upcoming"])
        self.assertEqual((m["date"], m["intimated"][:10]), ("2026-10-26", "2026-10-19"))
        self.assertIn("period ended September 2026", m["desc"])

    def test_render(self):
        out = res.render("TATACONSUM", self.result, self.meeting, True)
        self.assertIn("TATA CONSUMER PRODUCTS LIMITED (TATACONSUM), consolidated. Rs crore except EPS.", out)
        self.assertIn("2026-09-30  Q2 FY27     5,661.1   +14.0%   14.5%   11.2%    8.3%      470.1     4.74  +15.9%", out)
        self.assertIn("2026-03-31  Q4 FY26     5,433.6   +17.9%", out)
        self.assertIn("  audited", out)
        self.assertIn("Last 4 quarters: revenue Rs 21,555.63 crore, net profit Rs 1,705.87 crore, diluted EPS Rs 17.17.", out)
        self.assertIn("No results board meeting intimated yet; the last one was 2026-10-26.", out)
        self.assertIn("iXBRL_WEB.html", out)
        self.assertNotIn(chr(0x2014), out)


class TheXbrl(unittest.TestCase):
    def test_quarter_context_and_facts(self):
        p = res.parse_xbrl(xbrl("INTEGRATED_FILING_INDAS_1746148_26102026064816_WEB.xml"))
        self.assertEqual((p["context"], p["start"], p["end"]), ("OneD", "2026-07-01", "2026-09-30"))
        self.assertEqual(p["meta"]["name"], "TATA CONSUMER PRODUCTS LIMITED")
        self.assertEqual(p["meta"]["basis"], "Consolidated")
        self.assertEqual(p["meta"]["rounding"], "Crores")        # the XBRL itself is in whole rupees
        self.assertEqual(p["facts"]["RevenueFromOperations"], 56611300000)
        self.assertFalse(p["bank"])

    def test_older_files_stamp_the_year_with_the_quarters_dates(self):
        p = res.parse_xbrl(xbrl("INDAS_104658_1106904_24042024113607.xml"))
        self.assertEqual((p["context"], p["start"], p["end"]), ("OneD", "2024-01-01", "2024-03-31"))
        self.assertEqual(p["facts"]["RevenueFromOperations"], 39269400000)    # not the 15,205.9 crore year
        row = res.quarter_row(p)
        self.assertEqual((row["fiscal"], row["audited"]), ("Q4 FY24", True))

    def test_a_bank(self):
        p = res.parse_xbrl(xbrl("INTEGRATED_FILING_BANKING_1740913_17102026054812_WEB.xml"))
        self.assertTrue(p["bank"])
        row = res.quarter_row(p)
        self.assertEqual(row["revenue"], 951316300000)
        self.assertEqual(row["concepts"]["revenue"], "InterestEarned")
        self.assertEqual(row["operating_profit"], 328715400000)
        self.assertEqual(row["provisions"], 42090000000)
        self.assertEqual(row["eps_diluted"], 13.41)
        self.assertIsNone(row["ebitda"])
        self.assertEqual(row["net_margin"], 21.8)

    def test_bad_xml(self):
        with self.assertRaises(res.ResultsError):
            res.parse_xbrl(b"<xbrl></xbrl>")
        with self.assertRaises(res.ResultsError):
            res.parse_xbrl(b"not xml")

    def test_fiscal_labels(self):
        self.assertEqual(res.fiscal_label("2026-09-30"), "Q2 FY27")
        self.assertEqual(res.fiscal_label("2026-03-31"), "Q4 FY26")
        self.assertEqual(res.fiscal_label("2025-12-31"), "Q3 FY26")
        self.assertEqual(res.fiscal_label("2026-06-30"), "Q1 FY27")


class TheFilingsList(unittest.TestCase):
    def test_consolidated_wins_and_lists_merge(self):
        rows = res.parse_integrated(doc("integrated-TATACONSUM.json")) + res.parse_legacy(doc("legacy-TATACONSUM.json"))
        picked, basis = res.choose(rows)
        self.assertEqual(basis, "consolidated")
        self.assertEqual(len(picked), 15)                 # 7 integrated + 8 legacy quarters
        self.assertEqual(picked[0]["end"], "2026-09-30")
        self.assertEqual(picked[0]["source"], "integrated")
        self.assertEqual(picked[7]["end"], "2024-12-31")
        self.assertEqual(picked[7]["source"], "legacy")
        self.assertEqual(picked[0]["name"], "TATA CONSUMER PRODUCTS LIMITED")
        stand, basis = res.choose(rows, "standalone")
        self.assertEqual(basis, "standalone")
        self.assertTrue(all(f["basis"] == "standalone" for f in stand))
        self.assertEqual(len(stand), 15)

    def test_a_revision_wins(self):
        a = {"end": "2026-09-30", "basis": "consolidated", "seq": 1, "source": "integrated", "xbrl": "a"}
        b = dict(a, seq=2, xbrl="b", revised=True)
        picked, _ = res.choose([a, b])
        self.assertEqual(picked[0]["xbrl"], "b")


class Cli(unittest.TestCase):
    def test_bank_run_and_missing_quarters_are_said(self):
        code, out, err = run(["HDFCBANK", "--offline", DIR, "--quarters", "1", "--no-meetings"])
        self.assertEqual(code, 0, err)
        self.assertIn("HDFC Bank Limited (HDFCBANK), consolidated.", out)   # the name comes from the filings list
        self.assertIn("A bank: revenue here is interest earned. Operating profit before provisions "
                      "Rs 32,871.54 crore, provisions Rs 4,209.00 crore.", out)
        self.assertIn("Bank taxonomy", out)
        self.assertNotIn("Other income", out)
        self.assertNotIn("board-meeting", out)
        self.assertEqual(err.count("skipped: offline: no file"), 4)   # the four older quarters are not saved

    def test_metrics_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "trader", "metrics.json")
            code, out, err = run(["TATACONSUM", "--offline", DIR, "--metrics", path, "--today", "2026-11-05"])
            self.assertEqual(code, 0, err)
            with open(path) as fh:
                data = json.load(fh)
        self.assertEqual((data["as_of"], data["currency"]), ("2026-11-05", "INR"))
        row = data["companies"][0]
        self.assertEqual((row["symbol"], row["fiscal"], row["eps_ttm"]), ("TATACONSUM", "Q2 FY27", 17.17))
        self.assertEqual((row["revenue_growth_yoy"], row["ebitda_margin"]), (14.0, 14.5))
        self.assertEqual(row["revenue_ttm_inr"], 215556300000)
        self.assertIn("wrote", out)

    def test_json_and_errors(self):
        code, out, err = run(["TATACONSUM", "--offline", DIR, "--json", "--quarters", "4", "--today", "2026-11-05"])
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(len(data[0]["result"]["rows"]), 4)
        self.assertEqual(data[0]["next_results"]["date"], "2026-10-26")
        code, _, err = run(["ZZZZ", "--offline", DIR])
        self.assertEqual(code, 2)
        self.assertIn("no file", err)
        code, _, err = run(["TATACONSUM", "--offline", DIR, "--standalone", "--no-meetings"])
        self.assertEqual(code, 2)                               # no standalone XBRL is saved
        self.assertIn("none of the XBRL filings", err)


if __name__ == "__main__":
    unittest.main()

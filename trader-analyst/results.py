#!/usr/bin/env python3
"""Quarterly results reader for trader-analyst. Standard library only.

Reads the Ind-AS XBRL a company files with NSE for every quarter and lays out
what changed: revenue from operations, EBITDA, profit before tax and net
margins, net profit and diluted EPS, quarter by quarter, each against the same
quarter a year earlier, with the trailing twelve months. Also reads NSE's
board-meeting intimations, so you know whether the next results fall inside a
trade.

    python3 results.py TATACONSUM
    python3 results.py TATACONSUM RELIANCE --quarters 8
    python3 results.py HDFCBANK --standalone
    python3 results.py TATACONSUM --json
    python3 results.py TATACONSUM RELIANCE --metrics trader/metrics.json

Sources (nseindia.com wants a browser's headers; no key, no login):
  https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=X&period_ended=all&type=Integrated%20Filing-%20Financials
      the quarterly filings since the quarter ended March 2025 (SEBI's integrated filing)
  https://www.nseindia.com/api/corporates-financial-results?index=equities&symbol=X&period=Quarterly
      the older filings, up to the quarter ended December 2024
  https://nsearchives.nseindia.com/corporate/xbrl/<file>.xml
      each filing's XBRL instance (the numbers, in INR, as the company tagged them)
  https://www.nseindia.com/api/corporate-board-meetings?index=equities&symbol=X
      board meetings intimated to the exchange (the next results date)

How the quarters are built:
  - One XBRL per quarter. The quarter is the context covering about three
    months (80 to 100 days); the year-to-date context is ignored. Indian
    companies file the March quarter too (audited), so nothing is derived.
  - Consolidated results are used when the company files them, standalone
    otherwise (--standalone forces standalone). A revised filing for the same
    quarter wins over the original.
  - Growth is against the same quarter a year earlier, so the tool fetches
    four more quarters than it shows.
  - Banks tag a different taxonomy: revenue here is interest earned, and
    there is no EBITDA; operating profit before provisions is shown instead.
  - EBITDA = profit before exceptional items and tax + finance costs +
    depreciation, from the tagged lines. Companies' own "EBITDA" in a press
    release may differ (other income, lease treatment).

--metrics FILE merges one row per symbol into a JSON file (TTM revenue, net
profit and EPS, margins, growth). Offline: --offline DIR reads
integrated-<SYMBOL>.json, legacy-<SYMBOL>.json, board-meetings-<SYMBOL>.json
and each XBRL by its file name (optionally .gz) from DIR. The tests use it.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import datetime as _dt
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trader_data  # noqa: E402

NSE_API = "https://www.nseindia.com/api/"
INTEGRATED_URL = (NSE_API + "integrated-filing-results?index=equities&symbol=%s&period_ended=all"
                  "&type=Integrated%%20Filing-%%20Financials")
LEGACY_URL = NSE_API + "corporates-financial-results?index=equities&symbol=%s&period=Quarterly"
MEETINGS_URL = NSE_API + "corporate-board-meetings?index=equities&symbol=%s"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
CRORE = 1e7
YOY_EXTRA = 4
SOURCE = "Ind-AS XBRL filed on NSE"

CONCEPTS = {
    "revenue": ["RevenueFromOperations", "InterestEarned"],
    "other_income": ["OtherIncome"],
    "total_income": ["Income", "TotalIncome"],
    "expenses": ["Expenses", "ExpenditureExcludingProvisionsAndContingencies"],
    "finance_costs": ["FinanceCosts"],
    "depreciation": ["DepreciationDepletionAndAmortisationExpense"],
    "operating_profit": ["OperatingProfitBeforeProvisionAndContingencies"],
    "provisions": ["ProvisionsOtherThanTaxAndContingencies"],
    "pbt_before_exceptional": ["ProfitBeforeExceptionalItemsAndTax"],
    "exceptional": ["ExceptionalItemsBeforeTax", "ExceptionalItems"],
    "pbt": ["ProfitBeforeTax", "ProfitLossFromOrdinaryActivitiesBeforeTax"],
    "tax": ["TaxExpense"],
    "net_profit": ["ProfitLossForPeriod", "ProfitLossForThePeriod"],
    "net_profit_owners": ["ProfitOrLossAttributableToOwnersOfParent",
                          "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates"],
    "eps_basic": ["BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                  "BasicEarningsPerShareAfterExtraordinaryItems",
                  "BasicEarningsLossPerShareFromContinuingOperations"],
    "eps_diluted": ["DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                    "DilutedEarningsPerShareAfterExtraordinaryItems",
                    "DilutedEarningsLossPerShareFromContinuingOperations"],
}
TEXT_FACTS = {
    "NameOfTheCompany": "name", "Symbol": "symbol",
    "NatureOfReportStandaloneConsolidated": "basis", "WhetherResultsAreAuditedOrUnaudited": "audited",
    "DateOfBoardMeetingWhenFinancialResultsWereApproved": "board_meeting",
    "DateOfEndOfFinancialYear": "fy_end", "LevelOfRounding": "rounding",
    "LevelOfRoundingUsedInFinancialStatements": "rounding", "DescriptionOfPresentationCurrency": "currency",
}
RESULTS_RX = re.compile(r"financial results|results", re.I)


class ResultsError(Exception):
    pass


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.5):
        self.offline, self.pause, self._last = offline, pause, 0.0

    def get(self, url, name, nse=True):
        if self.offline:
            for path in (os.path.join(self.offline, name), os.path.join(self.offline, name + ".gz")):
                if os.path.isfile(path):
                    opener = gzip.open if path.endswith(".gz") else open
                    with opener(path, "rb") as fh:
                        return fh.read()
            raise ResultsError("offline: no file %s" % os.path.join(self.offline, name))
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        headers = {"User-Agent": BROWSER_UA, "Accept": "*/*", "Referer": "https://www.nseindia.com/",
                   "Accept-Language": "en-US,en;q=0.9"}
        if nse:
            headers["Accept"] = "application/json, text/plain, */*"
        req = urllib.request.Request(url, headers=headers)
        try:
            with trader_data.OPENER.open(req, timeout=60) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ResultsError("HTTP %s for %s" % (exc.code, url.split("?")[0]))
        except (urllib.error.URLError, OSError) as exc:
            raise ResultsError("could not reach %s: %s" % (url.split("/")[2], getattr(exc, "reason", exc)))

    def json(self, url, name):
        data = self.get(url, name, nse=True)
        try:
            return json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            raise ResultsError("nseindia.com did not answer JSON for %s (a block page?)" % url.split("?")[0])


# ---------------------------------------------------------------- the filings list

def iso(dmy):
    s = (dmy or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s[:11].strip().title(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def iso_time(stamp):
    s = (stamp or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return _dt.datetime.strptime(s, fmt).replace(tzinfo=IST).isoformat(timespec="seconds")
        except ValueError:
            continue
    return iso(s)


def parse_integrated(doc):
    """integrated-filing-results rows -> {end, basis, audited, filed, seq, xbrl, ixbrl, revised, source}."""
    rows = doc.get("data", []) if isinstance(doc, dict) else (doc or [])
    out = []
    for r in rows:
        if "Financial" not in (r.get("type") or "Financials"):
            continue
        xbrl = r.get("xbrl") or ""
        if not xbrl.lower().endswith(".xml"):
            continue
        out.append({"end": iso(r.get("qe_Date")), "basis": _basis(r.get("consolidated")),
                    "audited": (r.get("audited") or "").lower().startswith("aud"),
                    "filed": iso_time(r.get("broadcast_Date")), "seq": int(r.get("seq_Id") or 0),
                    "name": " ".join((r.get("cmName") or r.get("smName") or "").split()) or None,
                    "xbrl": xbrl, "ixbrl": r.get("ixbrl") or None,
                    "revised": (r.get("type_Sub") or "Original") != "Original" or bool(r.get("revised_Date")),
                    "source": "integrated"})
    return out


def parse_legacy(doc):
    rows = doc if isinstance(doc, list) else (doc or {}).get("data", [])
    out = []
    for r in rows:
        xbrl = r.get("xbrl") or ""
        if (r.get("period") or "Quarterly") != "Quarterly" or not xbrl.lower().endswith(".xml"):
            continue
        out.append({"end": iso(r.get("toDate")), "basis": _basis(r.get("consolidated")),
                    "audited": (r.get("audited") or "").lower().startswith("aud"),
                    "filed": iso_time(r.get("broadCastDate")), "seq": int(r.get("seqNumber") or 0),
                    "name": " ".join((r.get("companyName") or "").split()) or None,
                    "xbrl": xbrl, "ixbrl": None, "revised": (r.get("reInd") or "N") not in ("N", "-", ""),
                    "source": "legacy"})
    return out


def _basis(text):
    return "consolidated" if (text or "").strip().lower().startswith("consol") else "standalone"


def choose(filings, basis=None):
    """Newest filing per quarter end for one basis (consolidated if any, else standalone), newest first."""
    if basis is None:
        basis = "consolidated" if any(f["basis"] == "consolidated" for f in filings) else "standalone"
    best = {}
    for f in filings:
        if f["basis"] != basis or not f["end"]:
            continue
        key = f["end"]
        rank = (f["source"] == "integrated", f["seq"])
        if key not in best or rank > best[key][0]:
            best[key] = (rank, f)
    out = [v[1] for v in best.values()]
    out.sort(key=lambda f: f["end"], reverse=True)
    return out, basis


def filings_for(symbol, fetcher, needed, basis=None):
    """The integrated list, plus the legacy list when it does not reach back far enough."""
    rows = parse_integrated(fetcher.json(INTEGRATED_URL % symbol, "integrated-%s.json" % symbol))
    picked, basis = choose(rows, basis)
    if len(picked) < needed:
        try:
            rows += parse_legacy(fetcher.json(LEGACY_URL % symbol, "legacy-%s.json" % symbol))
        except ResultsError:
            pass
        picked, basis = choose(rows, basis)
    return picked, basis


# ---------------------------------------------------------------- the XBRL

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_xbrl(data):
    """One Ind-AS XBRL instance -> {"meta": {...}, "start", "end", "facts": {concept: value}, "bank": bool}.

    The quarter is the plain (dimension-free) context spanning 80 to 100 days;
    "OneD" wins when several do (older files stamp the year-to-date context
    with the quarter's dates)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ResultsError("the XBRL is not valid XML: %s" % exc)
    spans = {}
    for el in root.iter():
        if _local(el.tag) != "context":
            continue
        if any(_local(c.tag) in ("explicitMember", "typedMember") for c in el.iter()):
            continue
        start = end = None
        for c in el.iter():
            if _local(c.tag) == "startDate":
                start = (c.text or "").strip()
            elif _local(c.tag) == "endDate":
                end = (c.text or "").strip()
        if start and end:
            try:
                days = (_dt.date.fromisoformat(end) - _dt.date.fromisoformat(start)).days
            except ValueError:
                continue
            if 80 <= days <= 100:
                spans[el.attrib.get("id")] = (start, end)
    if not spans:
        raise ResultsError("no three-month context in the XBRL")
    ctx = "OneD" if "OneD" in spans else sorted(spans)[0]
    start, end = spans[ctx]
    facts, meta = {}, {}
    for el in root.iter():
        if el.attrib.get("contextRef") != ctx:
            continue
        name = _local(el.tag)
        text = (el.text or "").strip()
        if name in TEXT_FACTS:
            meta[TEXT_FACTS[name]] = text
        elif el.attrib.get("unitRef") and name not in facts:
            try:
                facts[name] = float(text.replace(",", ""))
            except ValueError:
                pass
    bank = "InterestEarned" in facts
    return {"meta": meta, "start": start, "end": end, "facts": facts, "bank": bank, "context": ctx}


def pick(facts, key):
    for concept in CONCEPTS[key]:
        if concept in facts:
            return facts[concept], concept
    return None, None


def fiscal_label(end):
    """'2026-09-30' -> 'Q2 FY27' (the Indian year ends 31 March)."""
    d = _dt.date.fromisoformat(end)
    fy = d.year + 1 if d.month > 3 else d.year
    q = ((d.month - 4) % 12) // 3 + 1
    return "Q%d FY%02d" % (q, fy % 100)


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round(100.0 * (a - b) / abs(b), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def quarter_row(parsed, filing=None):
    f = parsed["facts"]
    row = {"end": parsed["end"], "start": parsed["start"], "fiscal": fiscal_label(parsed["end"]),
           "bank": parsed["bank"], "concepts": {}}
    for key in CONCEPTS:
        row[key], row["concepts"][key] = pick(f, key)
    if row["eps_diluted"] is not None:
        row["eps_diluted"] = round(row["eps_diluted"], 2)
    if not row["bank"] and None not in (row["pbt_before_exceptional"], row["finance_costs"], row["depreciation"]):
        row["ebitda"] = row["pbt_before_exceptional"] + row["finance_costs"] + row["depreciation"]
    else:
        row["ebitda"] = None
    rev = row["revenue"]
    row["ebitda_margin"] = margin(row["ebitda"], rev)
    row["pbt_margin"] = margin(row["pbt_before_exceptional"] if row["pbt_before_exceptional"] is not None
                               else row["pbt"], rev)
    row["net_margin"] = margin(row["net_profit"], rev)
    meta = parsed["meta"]
    row["audited"] = (meta.get("audited") or "").lower().startswith("aud") or bool(filing and filing["audited"])
    row["basis"] = _basis(meta.get("basis")) if meta.get("basis") else (filing or {}).get("basis")
    row["filed"] = (filing or {}).get("filed")
    row["xbrl"] = (filing or {}).get("xbrl")
    row["ixbrl"] = (filing or {}).get("ixbrl")
    row["revised"] = bool((filing or {}).get("revised"))
    row["name"] = meta.get("name") or (filing or {}).get("name")
    return row


def _yoy_match(rows_by_end, end, days=365, slack=10):
    target = _dt.date.fromisoformat(end) - _dt.timedelta(days=days)
    best = None
    for e in rows_by_end:
        gap = abs((_dt.date.fromisoformat(e) - target).days)
        if gap <= slack and (best is None or gap < best[0]):
            best = (gap, e)
    return rows_by_end[best[1]] if best else None


def analyse(rows, quarters=6, symbol=None):
    """Quarter rows (any order) -> the report dict: rows newest first with growth, plus TTM."""
    by_end = dict((r["end"], r) for r in rows)
    ends = sorted(by_end, reverse=True)
    out = []
    for i, end in enumerate(ends[:quarters]):
        r = dict(by_end[end])
        prior = _yoy_match(by_end, end)
        prev = by_end[ends[i + 1]] if i + 1 < len(ends) else None
        r["revenue_yoy"] = pct(r["revenue"], prior["revenue"] if prior else None)
        r["net_profit_yoy"] = pct(r["net_profit"], prior["net_profit"] if prior else None)
        r["eps_yoy"] = pct(r["eps_diluted"], prior["eps_diluted"] if prior else None)
        r["revenue_qoq"] = pct(r["revenue"], prev["revenue"] if prev else None)
        r["ebitda_margin_change"] = (round(r["ebitda_margin"] - prev["ebitda_margin"], 1)
                                     if prev and r["ebitda_margin"] is not None and prev["ebitda_margin"] is not None
                                     else None)
        r["net_margin_change"] = (round(r["net_margin"] - prev["net_margin"], 1)
                                  if prev and r["net_margin"] is not None and prev["net_margin"] is not None
                                  else None)
        out.append(r)
    ttm = {}
    last4 = ends[:4]
    if len(last4) == 4 and (_dt.date.fromisoformat(last4[0]) - _dt.date.fromisoformat(last4[3])).days < 300:
        for key in ("revenue", "net_profit", "eps_diluted"):
            vals = [by_end[e][key] for e in last4]
            if None not in vals:
                ttm[key] = round(sum(vals), 2)
    name = next((r["name"] for r in out if r.get("name")), None)
    return {"symbol": symbol, "name": name, "basis": out[0]["basis"] if out else None,
            "bank": any(r["bank"] for r in out), "rows": out, "ttm": ttm, "quarters_known": len(ends)}


# ---------------------------------------------------------------- what changed

def crore(v):
    if v is None:
        return "n/a"
    return "Rs {:,.2f} crore".format(v / CRORE)


def changes(result):
    rows = result["rows"]
    if not rows:
        return []
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    out = []
    label = "Quarter ending %s (%s, %s, %s)" % (cur["end"], cur["fiscal"], "audited" if cur["audited"] else "unaudited",
                                               cur["basis"] or "?")
    line = "%s: revenue %s" % (label, crore(cur["revenue"]))
    if cur["revenue_yoy"] is not None:
        line += " (%+.1f%% vs a year earlier)" % cur["revenue_yoy"]
    line += ", net profit %s" % crore(cur["net_profit"])
    if cur["net_profit_yoy"] is not None:
        line += " (%+.1f%%)" % cur["net_profit_yoy"]
    if cur["eps_diluted"] is not None:
        line += ", diluted EPS Rs %.2f" % cur["eps_diluted"]
        if cur["eps_yoy"] is not None:
            line += " (%+.1f%%)" % cur["eps_yoy"]
    out.append(line + ".")
    if cur["bank"]:
        out.append("A bank: revenue here is interest earned. Operating profit before provisions %s, provisions %s." % (
            crore(cur["operating_profit"]), crore(cur["provisions"])))
    for key, name in (("ebitda_margin", "EBITDA margin"), ("pbt_margin", "Profit-before-tax margin"),
                      ("net_margin", "Net margin")):
        if cur[key] is None:
            continue
        s = "%s %.1f%%" % (name, cur[key])
        if prev and prev[key] is not None:
            s += " (%+.1f points vs the quarter before)" % (cur[key] - prev[key])
        out.append(s + ".")
    if cur["exceptional"]:
        out.append("Exceptional items of %s sit inside profit before tax: read the notes before "
                   "reading the profit as operating." % crore(cur["exceptional"]))
    if not cur["bank"] and cur["other_income"] and cur["pbt"] and cur["other_income"] > 0.25 * abs(cur["pbt"]):
        out.append("Other income (%s) is %.0f%% of profit before tax: not the business itself." % (
            crore(cur["other_income"]), 100.0 * cur["other_income"] / abs(cur["pbt"])))
    if cur["tax"] is not None and cur["tax"] < 0:
        out.append("The tax line is a credit of %s, which lifts net profit." % crore(-cur["tax"]))
    if cur["revenue_qoq"] is not None:
        out.append("Revenue %+.1f%% vs the quarter before (many businesses are seasonal: compare with "
                   "a year earlier first)." % cur["revenue_qoq"])
    return out


# ---------------------------------------------------------------- the next results date

def parse_meetings(doc):
    rows = doc if isinstance(doc, list) else (doc or {}).get("data", [])
    out = []
    for r in rows:
        out.append({"date": iso(r.get("bm_date")), "purpose": (r.get("bm_purpose") or "").strip(),
                    "desc": " ".join((r.get("bm_desc") or "").split()), "intimated": iso_time(r.get("bm_timestamp")),
                    "attachment": r.get("attachment") or r.get("ixbrl") or None})
    out.sort(key=lambda m: (m["date"] or "", m["intimated"] or ""), reverse=True)
    return out


def next_results(meetings, today):
    """The nearest board meeting on or after today about financial results, else the last one held."""
    today = today.isoformat()
    about = [m for m in meetings if m["date"] and RESULTS_RX.search(m["purpose"] + " " + m["desc"])]
    future = sorted((m for m in about if m["date"] >= today), key=lambda m: m["date"])
    if future:
        return dict(future[0], upcoming=True)
    past = [m for m in about if m["date"] < today]
    return dict(past[0], upcoming=False) if past else None


# ---------------------------------------------------------------- output

def _c(v):
    return "-" if v is None else "{:,.1f}".format(v / CRORE)


def _p(v):
    return "-" if v is None else "%+.1f%%" % v


def _pm(v):
    return "-" if v is None else "%.1f%%" % v


def render(symbol, result, meeting=None, meetings_checked=False):
    out = ["%s (%s), %s. Rs crore except EPS. Source: %s." % (
        result["name"] or symbol, symbol, result["basis"] or "?", SOURCE), ""]
    out.append("%-11s %-8s %10s %8s %7s %7s %7s %10s %8s %7s" % (
        "quarter end", "fiscal", "revenue", "rev yoy", "ebitda", "pbt", "net", "net profit", "EPS dil", "EPS yoy"))
    for r in result["rows"]:
        out.append("%-11s %-8s %10s %8s %7s %7s %7s %10s %8s %7s%s%s" % (
            r["end"], r["fiscal"], _c(r["revenue"]), _p(r["revenue_yoy"]), _pm(r["ebitda_margin"]),
            _pm(r["pbt_margin"]), _pm(r["net_margin"]), _c(r["net_profit"]),
            "-" if r["eps_diluted"] is None else "%.2f" % r["eps_diluted"], _p(r["eps_yoy"]),
            "  audited" if r["audited"] else "", "  revised" if r["revised"] else ""))
    t = result["ttm"]
    if t:
        out.append("")
        out.append("Last 4 quarters: revenue %s, net profit %s, diluted EPS %s." % (
            crore(t.get("revenue")), crore(t.get("net_profit")),
            "Rs %.2f" % t["eps_diluted"] if t.get("eps_diluted") is not None else "-"))
    out.append("")
    out.append("What changed:")
    for line in changes(result):
        out.append("  - " + line)
    if result["rows"]:
        cur = result["rows"][0]
        out.append("")
        out.append("Latest filing: quarter ended %s, filed %s  %s" % (
            cur["end"], (cur["filed"] or "?")[:16].replace("T", " "), cur["ixbrl"] or cur["xbrl"] or ""))
    if meeting:
        if meeting["upcoming"]:
            out.append("Next results: board meeting on %s (intimated %s): %s" % (
                meeting["date"], (meeting["intimated"] or "?")[:10], meeting["desc"] or meeting["purpose"]))
        else:
            out.append("No results board meeting intimated yet; the last one was %s. Expect the next "
                       "intimation a week or so before the meeting." % meeting["date"])
    elif meetings_checked:
        out.append("No board-meeting intimation about results on NSE's feed.")
    if result["bank"]:
        out.append("")
        out.append("Bank taxonomy: revenue is interest earned; EBITDA does not apply.")
    return "\n".join(out) + "\n"


def metrics_row(symbol, result):
    cur = result["rows"][0] if result["rows"] else {}
    t = result["ttm"]
    return {"symbol": symbol, "name": result["name"], "basis": result["basis"], "period_end": cur.get("end"),
            "fiscal": cur.get("fiscal"), "revenue_ttm_inr": t.get("revenue"), "net_profit_ttm_inr": t.get("net_profit"),
            "eps_ttm": t.get("eps_diluted"), "revenue_growth_yoy": cur.get("revenue_yoy"),
            "net_profit_growth_yoy": cur.get("net_profit_yoy"), "eps_growth_yoy": cur.get("eps_yoy"),
            "ebitda_margin": cur.get("ebitda_margin"), "pbt_margin": cur.get("pbt_margin"),
            "net_margin": cur.get("net_margin"), "currency": "INR", "source": SOURCE}


# ---------------------------------------------------------------- CLI

def run_symbol(symbol, fetcher, quarters, basis=None, want_meetings=True, today=None):
    needed = quarters + YOY_EXTRA
    filings, basis = filings_for(symbol, fetcher, needed, basis)
    if not filings:
        raise ResultsError("no quarterly XBRL filings on NSE for %s (is it an NSE-listed company?)" % symbol)
    rows, notes = [], []
    for f in filings[:needed]:
        name = f["xbrl"].rsplit("/", 1)[-1]
        try:
            parsed = parse_xbrl(fetcher.get(f["xbrl"], name, nse=False))
        except ResultsError as exc:
            notes.append("quarter ended %s skipped: %s" % (f["end"], exc))
            continue
        if parsed["end"] != f["end"]:
            notes.append("quarter ended %s: the XBRL covers %s to %s" % (f["end"], parsed["start"], parsed["end"]))
        rows.append(quarter_row(parsed, f))
    if not rows:
        raise ResultsError("none of the XBRL filings for %s could be read" % symbol)
    result = analyse(rows, quarters, symbol)
    result["basis"] = basis
    result["notes"] = notes
    meeting = None
    if want_meetings:
        try:
            doc = fetcher.json(MEETINGS_URL % symbol, "board-meetings-%s.json" % symbol)
            meeting = next_results(parse_meetings(doc), today or _dt.date.today())
        except ResultsError as exc:
            notes.append("board meetings: %s" % exc)
    return result, meeting


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("symbols", nargs="*", help="NSE symbols, e.g. TATACONSUM RELIANCE")
    ap.add_argument("--quarters", type=int, default=6)
    ap.add_argument("--standalone", action="store_true", help="standalone results even when consolidated exist")
    ap.add_argument("--no-meetings", action="store_true", help="skip the next-results lookup")
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per symbol into this JSON")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--today", help="YYYY-MM-DD, for reproducible runs")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.symbols:
        ap.print_help()
        return 2
    today = _dt.date.fromisoformat(args.today) if args.today else _dt.date.today()
    fetcher = Fetcher(offline=args.offline)
    results, rows, failed = [], [], 0
    for sym in args.symbols:
        sym = sym.upper()
        try:
            result, meeting = run_symbol(sym, fetcher, args.quarters, "standalone" if args.standalone else None,
                                         not args.no_meetings, today)
        except ResultsError as exc:
            print("results: %s: %s" % (sym, exc), file=sys.stderr)
            failed += 1
            continue
        for note in result["notes"]:
            print("results: %s: %s" % (sym, note), file=sys.stderr)
        results.append({"symbol": sym, "result": result, "next_results": meeting, "changes": changes(result)})
        rows.append(metrics_row(sym, result))
        if not args.json:
            sys.stdout.write(render(sym, result, meeting, not args.no_meetings))
            sys.stdout.write("\n")
    if args.json:
        print(json.dumps(results, indent=2))
    if args.metrics and rows:
        existing = {}
        if os.path.isfile(args.metrics):
            with open(args.metrics, encoding="utf-8") as fh:
                existing = dict((r["symbol"], r) for r in json.load(fh).get("companies", []))
        for r in rows:
            existing[r["symbol"]] = r
        folder = os.path.dirname(args.metrics)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.metrics, "w", encoding="utf-8") as fh:
            json.dump({"as_of": today.isoformat(), "currency": "INR",
                       "companies": sorted(existing.values(), key=lambda r: r["symbol"])}, fh, indent=2)
            fh.write("\n")
        if not args.json:
            print("wrote %s (%d companies)" % (args.metrics, len(existing)))
    return 0 if results and not failed else (1 if results else 2)


if __name__ == "__main__":
    sys.exit(main())

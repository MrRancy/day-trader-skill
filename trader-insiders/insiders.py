#!/usr/bin/env python3
"""Insider and large-deal tracker for trader-insiders. Standard library only.

Reads two public NSE feeds: the insider-trading disclosures listed companies
must file under SEBI's Prohibition of Insider Trading regulations (promoters,
directors, key managerial personnel, designated employees and their immediate
relatives: every trade once their trades in a quarter pass Rs 10 lakh), and
the bulk and block deals NSE publishes after the close.

    python3 insiders.py pit TATACONSUM RELIANCE          # insider disclosures for these symbols
    python3 insiders.py pit --kinds market --min-value 1000000
    python3 insiders.py deals                            # today's bulk and block deals, every symbol
    python3 insiders.py deals --from 2026-10-06 --to 2026-11-05 --symbols ICICIBANK
    python3 insiders.py new                              # what is new for your symbols since the last check

Where the data comes from (nseindia.com wants a browser's headers; it answers
JSON to this tool's):
  - https://www.nseindia.com/api/corporates-pit?index=equities&symbol=X
    One symbol at a time: the index-wide call answered no rows on 2026-11-05.
    Each row is one line of a Reg 7(2) disclosure, with the XBRL the company filed.
  - https://www.nseindia.com/api/snapshot-capital-market-largedeal
    Today's bulk deals, block deals and short-sale disclosures.
  - https://www.nseindia.com/api/historicalOR/bulk-block-short-deals?optionType=bulk_deals&from=DD-MM-YYYY&to=DD-MM-YYYY
    Past bulk (or block) deals. It answers at most 70 rows per call, oldest
    date first (Nov 4 2026 had 133 bulk deals; the history gave 70), so the
    tool asks one trading day at a time and says when a day was cut short.
    The evening snapshot is complete; keep it with `new` on a schedule.

What a disclosure is, and is not:
  - It is PUBLIC. The insider has two trading days to tell the company and the
    company two trading days to tell the exchange, so it arrives days after the
    trade. Reading it fast is not inside information: there is none here.
  - Most rows are not market trades. ESOP allotments, pledges, gifts and
    off-market transfers between promoter entities fill the feed. Each row is
    tagged with its kind; `--kinds market` keeps only market purchases and sales.
  - A bulk deal is one client trading more than 0.5% of a company's shares in a
    day, reported after the close. A block deal is a single large trade (Rs 10
    crore or more) in the exchange's block windows. Both name the client; neither
    says why.

State: `new` remembers which rows it has shown in trader/state/insiders.json, so
each disclosure alerts once. Offline: --offline DIR reads pit-<SYMBOL>.json,
largedeal.json and <kind>_deals-<YYYY-MM-DD>.json (else <kind>_deals.json)
from DIR. The tests use it.
"""

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # realpath: works through a junction
import trader_data  # noqa: E402

NSE_API = "https://www.nseindia.com/api/"
PIT_URL = NSE_API + "corporates-pit?index=equities&symbol=%s"
SNAPSHOT_URL = NSE_API + "snapshot-capital-market-largedeal"
HISTORY_URL = NSE_API + "historicalOR/bulk-block-short-deals?optionType=%s&from=%s&to=%s"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
HISTORY_CAP = 70        # rows NSE's history endpoint answers per call (measured 2026-11-05)
MAX_HISTORY_DAYS = 31   # trading days a `deals --from --to` range may span (one call per day per kind)
LATE_DAYS = 7           # 2 trading days (insider) + 2 (company) + a weekend; more than this is late
FIRST_DAYS = 30         # first run of `new`: only the last 30 days
KEEP_DAYS = 400         # how long `new` remembers a row
KINDS = [
    ("market", re.compile(r"market (purchase|sale)|on market", re.I)),
    ("esop", re.compile(r"esop|esps|employee|stock option|sweat", re.I)),
    ("pledge", re.compile(r"pledge|encumbr", re.I)),
    ("gift", re.compile(r"gift", re.I)),
    ("corporate", re.compile(r"preferential|rights|bonus|buy ?back|open offer|allotment|conversion|"
                             r"merger|amalgamation|scheme|ipo|public issue|qip|warrant", re.I)),
    ("off-market", re.compile(r"off market|off-market|inter[- ]se|block", re.I)),
]
PLEDGE_TYPES = re.compile(r"pledge|invoke|revoke", re.I)


class InsidersError(Exception):
    pass


# ---------------------------------------------------------------- dates and numbers

def iso(dmy):
    """'13-Feb-2026' or '29-OCT-2026' -> '2026-02-13'; None when it is not a date."""
    s = (dmy or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s[:11].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def iso_time(stamp):
    """'18-Feb-2026 19:06' or '18-Feb-2026 19:06:37' (IST) -> '2026-02-18T19:06:00+05:30'."""
    s = (stamp or "").strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            d = _dt.datetime.strptime(s, fmt).replace(tzinfo=IST)
            return d.isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def num(v):
    """'2,320', '1600', 'Nil' -> number; '-', '', None -> None."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s.lower() in ("nil", "na", "n.a."):
        return 0
    if s in ("", "-"):
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return int(f) if f == int(f) and "." not in s else f


def nse_date(d):
    """date -> 'DD-MM-YYYY' as NSE's history endpoint wants it."""
    return d.strftime("%d-%m-%Y")


def money(v):
    """Rs 20.20 lakh / Rs 3.29 crore / Rs 85,000 (Indian grouping)."""
    if v is None:
        return "n/a"
    v = float(v)
    if abs(v) >= 1e7:
        return "Rs %.2f crore" % (v / 1e7)
    if abs(v) >= 1e5:
        return "Rs %.2f lakh" % (v / 1e5)
    return "Rs {:,.0f}".format(v)


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.5):
        self.offline, self.pause, self._last = offline, pause, 0.0

    def json(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise InsidersError("offline: no file %s" % path)
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={
            "User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.nseindia.com/"})
        try:
            with trader_data.OPENER.open(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            exc.close()
            raise InsidersError("nseindia.com answered HTTP %s for %s" % (exc.code, url.split("?")[0]))
        except (urllib.error.URLError, OSError) as exc:
            raise InsidersError("could not reach nseindia.com: %s" % getattr(exc, "reason", exc))
        try:
            return json.loads(text)
        except ValueError:
            raise InsidersError("nseindia.com did not answer JSON for %s (a block page?)" % url.split("?")[0])


# ---------------------------------------------------------------- insider disclosures

def classify(mode, txn_type):
    """The kind of row: market, esop, pledge, gift, corporate, off-market or other."""
    if PLEDGE_TYPES.search(txn_type or "") and not re.search(r"buy|sell", txn_type or "", re.I):
        return "pledge"
    text = "%s %s" % (mode or "", txn_type or "")
    for kind, rx in KINDS:
        if rx.search(text):
            return kind
    return "other"


def parse_pit(doc, symbol=None):
    """NSE's corporates-pit rows -> clean dicts, newest first.

    Each: id, symbol, company, name, category, type (Buy, Sell, Pledge ...),
    mode (Market Purchase, ESOP, Off Market ...), kind, security, qty, value_inr,
    price (value / qty), before, after, after_pct, traded_from, traded_to,
    intimated, disclosed (IST), days_to_disclose, late, exchange, xbrl."""
    rows = doc.get("data", []) if isinstance(doc, dict) else (doc or [])
    out = []
    for r in rows:
        sym = (r.get("symbol") or symbol or "").strip().upper()
        if symbol and sym != symbol.upper():
            continue
        qty = num(r.get("secAcq"))
        value = num(r.get("secVal"))
        disclosed = iso_time(r.get("date"))
        traded_to = iso(r.get("acqtoDt")) or iso(r.get("acqfromDt"))
        days = None
        if disclosed and traded_to:
            days = (_dt.date.fromisoformat(disclosed[:10]) - _dt.date.fromisoformat(traded_to)).days
        txn = (r.get("tdpTransactionType") or "").strip()
        mode = (r.get("acqMode") or "").strip()
        row = {
            "id": "pit:%s" % (r.get("did") or "%s:%s:%s" % (sym, r.get("date"), r.get("acqName"))),
            "symbol": sym, "company": (r.get("company") or "").strip(),
            "name": " ".join((r.get("acqName") or "").split()),
            "category": (r.get("personCategory") or "").strip(),
            "type": txn, "mode": mode, "kind": classify(mode, txn),
            "security": (r.get("secType") or "").strip(),
            "qty": qty, "value_inr": value,
            "price": round(float(value) / qty, 2) if qty and value else None,
            "before": num(r.get("befAcqSharesNo")), "after": num(r.get("afterAcqSharesNo")),
            "after_pct": num(r.get("afterAcqSharesPer")),
            "traded_from": iso(r.get("acqfromDt")), "traded_to": traded_to,
            "intimated": iso(r.get("intimDt")), "disclosed": disclosed,
            "days_to_disclose": days, "late": days is not None and days > LATE_DAYS,
            "exchange": (r.get("exchange") or "").strip() or None,
            "xbrl": r.get("xbrl") or None,
        }
        out.append(row)
    out.sort(key=lambda x: (x["disclosed"] or "", x["id"]), reverse=True)
    return out


def select_pit(rows, since=None, kinds=None, min_value=None):
    out = []
    for r in rows:
        if since and (r["disclosed"] or "")[:10] < since:
            continue
        if kinds and r["kind"] not in kinds:
            continue
        if min_value and (r["value_inr"] or 0) < min_value:
            continue
        out.append(r)
    return out


def market_flow(rows, days=90, today=None):
    """Net market buying by insiders in the last `days`: {"buys", "sells", "bought_inr", "sold_inr", "net_inr"}."""
    today = today or _dt.date.today()
    cutoff = (today - _dt.timedelta(days=days)).isoformat()
    buys = sells = 0
    bought = sold = 0.0
    for r in rows:
        if r["kind"] != "market" or (r["disclosed"] or "")[:10] < cutoff:
            continue
        if re.search(r"buy|purchase", "%s %s" % (r["type"], r["mode"]), re.I):
            buys += 1
            bought += r["value_inr"] or 0
        elif re.search(r"sell|sale", "%s %s" % (r["type"], r["mode"]), re.I):
            sells += 1
            sold += r["value_inr"] or 0
    return {"days": days, "buys": buys, "sells": sells, "bought_inr": bought, "sold_inr": sold,
            "net_inr": bought - sold}


# ---------------------------------------------------------------- bulk and block deals

def _deal(kind, date, symbol, company, client, side, qty, price, remarks=None):
    qty = num(qty)
    price = num(price)
    value = round(qty * float(price), 2) if qty and price else None
    bits = [kind, date or "", symbol or "", client or "", side or "", str(qty or ""), str(price or "")]
    return {"id": "deal:" + ":".join(bits), "kind": kind, "date": date, "symbol": (symbol or "").upper(),
            "company": " ".join((company or "").split()), "client": " ".join((client or "").split()) or None,
            "side": (side or "").upper() or None, "qty": qty, "price": price, "value_inr": value,
            "remarks": (remarks or "").strip() or None}


def parse_snapshot(doc):
    """snapshot-capital-market-largedeal -> {"as_on": date, "bulk": [...], "block": [...], "short": [...]}."""
    if not isinstance(doc, dict):
        raise InsidersError("NSE's large-deal snapshot is not the shape this tool knows")
    out = {"as_on": iso(doc.get("as_on_date")), "bulk": [], "block": [], "short": []}
    for kind, key in (("bulk", "BULK_DEALS_DATA"), ("block", "BLOCK_DEALS_DATA"), ("short", "SHORT_DEALS_DATA")):
        for r in doc.get(key) or []:
            out[kind].append(_deal(kind, iso(r.get("date")), r.get("symbol"), r.get("name"), r.get("clientName"),
                                   r.get("buySell"), r.get("qty"), r.get("watp"), r.get("remarks")))
    return out


def parse_history(doc, kind):
    """historicalOR/bulk-block-short-deals rows -> deals of `kind`, newest first."""
    rows = doc.get("data", doc) if isinstance(doc, dict) else doc
    out = []
    for r in rows or []:
        out.append(_deal(kind, iso(r.get("BD_DT_DATE")), r.get("BD_SYMBOL"), r.get("BD_SCRIP_NAME"),
                         r.get("BD_CLIENT_NAME"), r.get("BD_BUY_SELL"), r.get("BD_QTY_TRD"), r.get("BD_TP_WATP"),
                         r.get("BD_REMARKS")))
    out.sort(key=lambda d: (d["date"] or "", d["symbol"], d["side"] or ""), reverse=True)
    return out


def load_deals(fetcher, start=None, end=None, kinds=("bulk", "block")):
    """Today's snapshot, or a date range from the history endpoint.

    NSE's history answers at most HISTORY_CAP rows per call, oldest date
    first, so the range is fetched one trading day at a time (up to
    MAX_HISTORY_DAYS) and a day that hits the cap is listed in "truncated".
    Offline: <kind>_deals-<YYYY-MM-DD>.json per day, else <kind>_deals.json
    filtered to the day."""
    if not (start or end):
        return parse_snapshot(fetcher.json(SNAPSHOT_URL, "largedeal.json"))
    end = end or _dt.date.today()
    start = start or end - _dt.timedelta(days=30)
    if start > end:
        raise InsidersError("--from is after --to")
    days = []
    d = start
    while d <= end:
        if trader_data.trading_day(d)[0]:
            days.append(d)
        d += _dt.timedelta(days=1)
    if len(days) > MAX_HISTORY_DAYS:
        raise InsidersError("that is %d trading days; NSE's history is read a day at a time, so ask for %d or fewer"
                            % (len(days), MAX_HISTORY_DAYS))
    out = {"as_on": end.isoformat(), "bulk": [], "block": [], "short": [], "truncated": []}
    for kind in kinds:
        if kind == "short":
            continue
        for day in days:
            url = HISTORY_URL % (kind + "_deals", nse_date(day), nse_date(day))
            try:
                rows = parse_history(fetcher.json(url, "%s_deals-%s.json" % (kind, day.isoformat())), kind)
            except InsidersError:
                if not fetcher.offline:
                    raise
                rows = [r for r in parse_history(fetcher.json(url, "%s_deals.json" % kind), kind)
                        if r["date"] == day.isoformat()]
            if len(rows) >= HISTORY_CAP:
                out["truncated"].append("%s %s" % (kind, day.isoformat()))
            out[kind] += rows
    return out


# ---------------------------------------------------------------- your symbols

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json", plan="trader/plan.json"):
    """{symbol: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    for h in (load_json(watchlist) or {}).get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[sym.upper()] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(pl["symbol"].upper(), pl.get("name"))
    for p in (load_json(portfolio) or {}).get("positions", []):
        sym = p.get("symbol") or p.get("ticker")
        if sym:
            out[sym.upper()] = p.get("name") or out.get(sym.upper())
    return out


def symbols_for(args):
    if args.symbols:
        return [s.upper() for s in args.symbols]
    return sorted(watch_from_files(args.portfolio, args.watchlist, args.plan))


# ---------------------------------------------------------------- state

def load_state(path):
    state = load_json(path) or {}
    state.setdefault("seen", {})
    state.setdefault("last_check", None)
    return state


def save_state(path, state, today):
    cutoff = (today - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = dict((k, v) for k, v in state["seen"].items() if (v or "") >= cutoff)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
        fh.write("\n")


def item_date(it):
    return (it.get("disclosed") or it.get("date") or "")[:10]


def new_since(items, state, today, first_days=FIRST_DAYS):
    """Items not seen before. On the first run only the last first_days days;
    after that, nothing older than the state keeps (KEEP_DAYS) counts as new."""
    if state.get("last_check") is None:
        cutoff = (today - _dt.timedelta(days=first_days)).isoformat()
        return [it for it in items if item_date(it) >= cutoff]
    keep = (today - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    return [it for it in items if it["id"] not in state["seen"] and item_date(it) >= keep]


# ---------------------------------------------------------------- output

def describe_pit(r):
    verb = {"Buy": "bought", "Sell": "sold"}.get(r["type"], r["type"].lower() or "moved")
    what = "%s %s" % ("{:,}".format(r["qty"]) if r["qty"] is not None else "?", r["security"].lower() or "securities")
    bits = ["%s %s: %s %s (%s)" % (r["category"] or "insider", r["name"], verb, what, r["mode"] or r["kind"])]
    if r["value_inr"]:
        s = "worth " + money(r["value_inr"])
        if r["price"]:
            s += " at about Rs {:,.2f}".format(r["price"])
        bits.append(s)
    if r["before"] is not None and r["after"] is not None:
        hold = "holding {:,} -> {:,}".format(r["before"], r["after"])
        if r["after_pct"]:
            hold += " (%.2f%%)" % r["after_pct"]
        bits.append(hold)
    when = "traded %s" % (r["traded_to"] or "?")
    if r["traded_from"] and r["traded_from"] != r["traded_to"]:
        when = "traded %s to %s" % (r["traded_from"], r["traded_to"])
    if r["days_to_disclose"] is not None:
        when += ", disclosed %d day%s later%s" % (r["days_to_disclose"], "" if r["days_to_disclose"] == 1 else "s",
                                                 " (LATE: over %d)" % LATE_DAYS if r["late"] else "")
    bits.append(when)
    return ", ".join(bits)


def describe_deal(d):
    if d["kind"] == "short":
        return "short sale disclosed in %s: %s shares" % (d["symbol"], "{:,}".format(d["qty"] or 0))
    s = "%s deal %s: %s %s %s shares" % (d["kind"], d["date"], d["client"] or "?", (d["side"] or "?").lower(),
                                        "{:,}".format(d["qty"] or 0))
    if d["price"]:
        s += " at Rs {:,.2f}".format(d["price"])
    if d["value_inr"]:
        s += " (%s)" % money(d["value_inr"])
    return s


CAVEAT = ("Public disclosures: SEBI PIT Reg 7(2) rows reach the exchange up to four trading days after "
          "the trade; bulk and block deals are reported after the close. Not inside information.")


def render_pit(symbol, rows, flow=None, total=None):
    out = ["%s: %d insider disclosure%s on NSE%s, newest first." % (
        symbol, len(rows), "" if len(rows) == 1 else "s",
        " (of %d)" % total if total is not None and total != len(rows) else "")]
    for r in rows:
        out.append("  %s  %s" % ((r["disclosed"] or "")[:10], describe_pit(r)))
    if flow and not flow["buys"] and not flow["sells"]:
        why = ("" if any(r["kind"] == "market" for r in rows) else
               " (the rows above are ESOPs, pledges, gifts or off-market transfers, not market trades)")
        out.append("  Market trades by insiders in the last %d days: none%s." % (flow["days"], why))
    elif flow:
        out.append("  Market trades by insiders in the last %d days: %d buy%s, %d sale%s, net %s%s." % (
            flow["days"], flow["buys"], "" if flow["buys"] == 1 else "s", flow["sells"],
            "" if flow["sells"] == 1 else "s", "bought " if flow["net_inr"] >= 0 else "sold ",
            money(abs(flow["net_inr"]))))
    return "\n".join(out) + "\n"


def render_deals(deals, watch=(), show_short=False):
    out = ["NSE large deals as on %s: %d bulk, %d block%s." % (
        deals["as_on"], len(deals["bulk"]), len(deals["block"]),
        ", %d short-sale disclosures" % len(deals["short"]) if deals["short"] else "")]
    if deals.get("truncated"):
        out.append("  NSE's history answers at most %d rows a day; cut short: %s. The evening snapshot "
                   "(`deals` without dates) is complete." % (HISTORY_CAP, ", ".join(deals["truncated"])))
    kinds = ["block", "bulk"] + (["short"] if show_short else [])
    for kind in kinds:
        for d in deals[kind]:
            flag = "  <- on your list" if d["symbol"] in watch else ""
            out.append("  %-12s %s%s" % (d["symbol"], describe_deal(d), flag))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- CLI

def _parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--symbols", "--tickers", dest="symbols", nargs="*",
                       help="default: watchlist + plan + portfolio symbols")
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--watchlist", default="trader/watchlist.json")
        p.add_argument("--plan", default="trader/plan.json")
        p.add_argument("--offline", metavar="DIR", help="saved NSE answers instead of the network")
        p.add_argument("--today", help="YYYY-MM-DD, for reproducible runs")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("pit", help="insider-trading disclosures for symbols")
    p.add_argument("symbols_pos", nargs="*", metavar="SYMBOL")
    common(p)
    p.add_argument("--since", help="only rows disclosed on or after YYYY-MM-DD")
    p.add_argument("--kinds", help="comma list of market,esop,pledge,gift,corporate,off-market,other")
    p.add_argument("--min-value", type=float, default=0, help="only rows worth at least this many INR")
    p.add_argument("--flow-days", type=int, default=90, help="window for the net market-flow line")

    p = sub.add_parser("deals", help="bulk and block deals (today, or a date range)")
    common(p)
    p.add_argument("--from", dest="start", help="YYYY-MM-DD: history from this date (one call per trading day)")
    p.add_argument("--to", dest="end", help="YYYY-MM-DD: history to this date (default today)")
    p.add_argument("--all-symbols", action="store_true", help="every symbol, not only yours")
    p.add_argument("--short", action="store_true", help="include short-sale disclosures (snapshot only)")

    p = sub.add_parser("new", help="disclosures and deals not seen at the last check")
    common(p)
    p.add_argument("--state", default="trader/state/insiders.json")
    p.add_argument("--days", type=int, default=FIRST_DAYS, help="first run: how far back to look")
    p.add_argument("--all-deals", action="store_true", help="show every bulk/block deal, not only your symbols")
    p.add_argument("--dry-run", action="store_true", help="do not update the state file")
    return ap


def main(argv=None):
    ap = _parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2
    try:
        return _run(args)
    except (InsidersError, OSError) as exc:
        print("insiders: %s" % exc, file=sys.stderr)
        return 2


def _run(args):
    today = _dt.date.fromisoformat(args.today) if args.today else _dt.date.today()
    fetcher = Fetcher(offline=args.offline)
    if getattr(args, "symbols_pos", None):
        args.symbols = (args.symbols or []) + args.symbols_pos
    symbols = symbols_for(args)

    if args.cmd == "pit":
        if not symbols:
            print("insiders: no symbols (pass them, or run /trader-scanner to write trader/watchlist.json).",
                  file=sys.stderr)
            return 2
        kinds = set(k.strip() for k in args.kinds.split(",")) if args.kinds else None
        result, shown = {}, 0
        for sym in symbols:
            rows = parse_pit(fetcher.json(PIT_URL % sym, "pit-%s.json" % sym), sym)
            picked = select_pit(rows, args.since, kinds, args.min_value)
            result[sym] = {"rows": picked, "total": len(rows), "flow": market_flow(rows, args.flow_days, today)}
            shown += len(picked)
            if not args.json:
                sys.stdout.write(render_pit(sym, picked, result[sym]["flow"], len(rows)))
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(CAVEAT)
        return 0 if shown else 1

    if args.cmd == "deals":
        start = _dt.date.fromisoformat(args.start) if args.start else None
        end = _dt.date.fromisoformat(args.end) if args.end else (today if start else None)
        deals = load_deals(fetcher, start, end)
        if not args.all_symbols and symbols:
            want = set(symbols)
            for kind in ("bulk", "block", "short"):
                deals[kind] = [d for d in deals[kind] if d["symbol"] in want]
        if not args.short:
            deals["short"] = []
        count = len(deals["bulk"]) + len(deals["block"]) + len(deals["short"])
        if args.json:
            print(json.dumps(deals, indent=2))
        else:
            sys.stdout.write(render_deals(deals, set(symbols), args.short))
            print(CAVEAT)
        return 0 if count else 1

    # new
    state = load_state(args.state)
    items = []
    for sym in symbols:
        try:
            items += parse_pit(fetcher.json(PIT_URL % sym, "pit-%s.json" % sym), sym)
        except InsidersError as exc:
            print("insiders: %s: %s" % (sym, exc), file=sys.stderr)
    deals = load_deals(fetcher)
    for kind in ("block", "bulk"):
        for d in deals[kind]:
            if args.all_deals or d["symbol"] in set(symbols):
                items.append(d)
    fresh = new_since(items, state, today, args.days)
    fresh.sort(key=lambda it: (item_date(it), it["id"]), reverse=True)
    if args.json:
        print(json.dumps({"new": fresh, "symbols": symbols, "as_on": deals["as_on"]}, indent=2))
    else:
        if not fresh:
            print("Nothing new since %s." % (state.get("last_check") or "the last check"))
        for it in fresh:
            flag = "  <- on your list" if it["symbol"] in set(symbols) else ""
            if it["id"].startswith("pit:"):
                print("NEW insider %s %s: %s%s" % (it["symbol"], (it["disclosed"] or "")[:10], describe_pit(it), flag))
            else:
                print("NEW %s %s%s" % (it["symbol"], describe_deal(it), flag))
        if fresh:
            print(CAVEAT)
    if not args.dry_run:
        for it in items:
            state["seen"].setdefault(it["id"], item_date(it))
        state["last_check"] = _dt.datetime.now(IST).replace(microsecond=0).isoformat()
        save_state(args.state, state, today)
    return 0 if fresh else 1


if __name__ == "__main__":
    sys.exit(main())

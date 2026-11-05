#!/usr/bin/env python3
"""News monitor for trader-news (NSE). Standard library only.

Watches headlines and NSE corporate announcements for the stocks you watch or
hold and prints only what is new since the last check, newest first, with how
far each stock has moved today and a flag on anything that touches your symbols.

    python3 news.py                                  # watchlist + plan + portfolio symbols
    python3 news.py --symbols TATACONSUM RELIANCE
    python3 news.py --symbols RELIANCE --all --hours 72  # everything in the last 3 days, seen or not
    python3 news.py --no-nse                         # headlines only
    python3 news.py --no-moves                       # skip the price line per symbol

Your symbols, when --symbols is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json.

Sources, one request each per symbol:
  - Yahoo Finance headline RSS for the .NS symbol:
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<SYMBOL>.NS&region=IN&lang=en-IN
    It answered an empty feed for every NSE symbol tried on 2026-11-05; it is
    tried first and kept for when it carries news again.
  - Google News RSS for the company's name (the working headline source):
    https://news.google.com/rss/search?q="<company name>"&hl=en-IN&gl=IN&ceid=IN:en
    The name comes from the watchlist, the INDstocks master (--master, or the
    download) or, failing that, the symbol itself.
  - NSE corporate announcements, the exchange's own filings feed (results,
    board meetings, management changes, press releases, with the PDF link):
    https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYMBOL>
    BSE's announcement API refuses automated clients (HTTP 403), so it is not read.

  - The move: INDstocks' full quote through ../trader_data.py, the last price
    against the previous close.

Times print in IST. State: trader/state/news.json remembers what you have
seen (30 days), so an item shows once. Offline: --offline DIR reads
yahoo-<SYMBOL>.xml, gnews-<SYMBOL>.xml, nse-<SYMBOL>.json and
quote-<SYMBOL>.json from DIR. The tests use it.

Headlines are leads, not facts: open the story before acting, and treat
nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trader_data  # noqa: E402

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s.NS&region=IN&lang=en-IN"
GNEWS = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
NSE_ANN = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
UA_NSE = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
BROWSER_UA = UA_NSE
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from the previous close is flagged as moving
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

# NSE announcement categories ("desc") that matter to a day trade
IMPORTANT_DESC = ("Outcome of Board Meeting", "Financial Results", "Change in Management", "Acquisition",
                  "Resignation", "Credit Rating", "Action(s) initiated or orders passed", "Press Release",
                  "Buyback", "Rights Issue", "Bonus", "Stock Split", "Dividend", "Fund Raising",
                  "Delisting", "Suspension")
QUIET_DESC = ("Copy of Newspaper Publication", "Loss of Share Certificates", "Analysts/Institutional Investor Meet",
              "ESOP/ESOS/ESPS", "Trading Window", "Shareholding", "Investor Presentation", "Record Date",
              "Certificate under SEBI", "Closure of Trading Window", "Compliance Certificate")
KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults?\b|\bEPS\b|\bguidance\b|\bprofit\b|\bQ[1-4]\b|\bquarter\b|\bmargin"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|target price|price target|\boutperform\b|\bunderperform\b|\bNomura\b|\bJefferies\b|\bMorgan Stanley\b|\bbrokerage"),
    ("legal", r"\blawsuit\b|\bsued\b|\bprobe\b|\binvestigation\b|\bSEBI order\b|\bCCI\b|\bNCLT\b|\bcourt\b|\btribunal\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bbuyout\b|\btakeover\b|\bdeal\b|\bstake\b|\bJV\b|\bjoint venture\b"),
    ("capital", r"\bdividend\b|\bbuyback\b|\bbonus\b|\bstock split\b|\bQIP\b|\brights issue\b|\bFPO\b|\bIPO\b|\bfund raising\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bMD\b|\bresign|\bsteps down\b|\bappoint|\bchairman\b|\bpromoter\b"),
    ("risk", r"\brecall\b|\bbankrupt|\bdefault\b|\bdelist|\bhack|\bbreach\b|\boutage\b|\bfraud\b|\bpledge"),
    ("halt", r"\bhalt(?:s|ed)?\b|\bcircuit\b|\btrading (?:pause|suspen)|\bASM\b|\bGSM\b"),
    ("regulatory", r"\bSEBI\b|\bRBI\b|\bDGCA\b|\bCDSCO\b|\bUSFDA\b|\bFDA\b|\bapproval\b|\bapproves\b|\bGST\b|\btariff"),
    ("macro", r"\bRBI\b|\brepo rate\b|\bCPI\b|\binflation\b|\bNifty\b|\bSensex\b|\bFII\b|\bDII\b|\bbudget\b|\bcrude\b|\brupee\b"),
    ("flows", r"\bblock deal\b|\bbulk deal\b|\bstake sale\b|\bOFS\b|\bpromoter (?:sells|buys|stake)"),
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

def to_ist(utc):
    """Aware or naive UTC -> naive IST wall-clock time."""
    if utc.tzinfo is not None:
        utc = utc.astimezone(_dt.timezone.utc)
    return utc.replace(tzinfo=None) + _dt.timedelta(hours=5, minutes=30)


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{symbol: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[sym.upper()] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(pl["symbol"].upper(), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("symbol") or p.get("ticker")
        if sym:
            out[sym.upper()] = p.get("name") or out.get(sym.upper())
    return out


def names_from_master(symbols, master=None, universe="trader/universe.txt"):
    """{symbol: company name} from trader/universe.txt, else a saved master, else nothing."""
    out = {}
    if universe and os.path.isfile(universe):
        with open(universe, encoding="utf-8") as fh:
            for ln in fh:
                if "|" in ln:
                    s, n = ln.rstrip("\n").split("|", 1)
                    if s.strip().upper() in symbols:
                        out[s.strip().upper()] = n.strip()
    missing = [s for s in symbols if s not in out]
    if missing and master and os.path.isfile(master):
        rows = trader_data.parse_instruments(trader_data.read_text(master))
        for r in rows:
            s = (r.get("TRADING_SYMBOL") or "").upper()
            if r.get("EXCH") == "NSE" and s in missing and (r.get("SERIES") or "") == "EQ":
                out[s] = r.get("SYMBOL_NAME") or ""
    return out


def search_name(name, sym):
    """The Google News query: the company name without the legal tail, else the symbol."""
    n = (name or "").strip()
    n = re.sub(r"\b(Limited|Ltd\.?|Ltd|Pvt\.?|Private|Corporation|Corp\.?|Co\.?)\b", "", n, flags=re.I)
    n = re.sub(r"[()\[\]]", " ", n)
    n = re.sub(r"\s+", " ", n).strip(" .,-")
    return n or sym


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.3):
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name, nse=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        headers = {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip", "Accept": "*/*"}
        if nse:
            headers.update({"Accept": "application/json, text/plain, */*", "Referer": "https://www.nseindia.com/",
                            "Accept-Language": "en-US,en;q=0.9"})
        req = urllib.request.Request(url, headers=headers)
        try:
            with trader_data.OPENER.open(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            exc.close()
            raise NewsError("HTTP %s for %s" % (exc.code, url.split("?")[0]))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url.split("/")[2], exc.reason))


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS), ISO 8601, or NSE's "04-Nov-2026 12:51:39" (IST) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            d = None
    if d is None:
        try:
            d = _dt.datetime.strptime(value, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_rss(data, symbol, source="Yahoo Finance"):
    """A headline RSS feed (Yahoo or Google News) -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (symbol, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        publisher = ""
        for c in it.iter():
            if _local(c.tag) == "source":
                publisher = (c.text or "").strip()
        if source == "Google News" and publisher and title.endswith(" - " + publisher):
            title = title[:-(len(publisher) + 3)].rstrip()
        desc = re.sub(r"<[^>]+>", " ", _text(it, "description"))
        desc = re.sub(r"\s+", " ", desc).strip()
        out.append({"id": "rss:" + guid, "source": publisher or source, "feed": source, "kind": "headline",
                    "symbol": symbol, "title": title, "summary": desc, "link": link,
                    "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_nse(data, symbol):
    """NSE corporate announcements JSON -> items."""
    try:
        rows = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ValueError as exc:
        raise NewsError("NSE announcements for %s are not JSON: %s" % (symbol, exc))
    if not isinstance(rows, list):
        raise NewsError("NSE announcements for %s are not a list" % symbol)
    out = []
    for r in rows:
        if str(r.get("symbol") or symbol).upper() != symbol.upper():
            continue
        desc = (r.get("desc") or "").strip()
        text = (r.get("attchmntText") or "").strip()
        company = (r.get("sm_name") or symbol).strip()
        title = "%s: %s" % (company.title() if company.isupper() else company, desc or "announcement")
        important = any(k.lower() in desc.lower() for k in IMPORTANT_DESC) and not any(
            k.lower() in desc.lower() for k in QUIET_DESC)
        out.append({"id": "nse:" + str(r.get("seq_id") or r.get("dt") or (symbol + desc)), "source": "NSE",
                    "feed": "NSE announcements", "kind": "filing", "symbol": symbol.upper(), "title": title,
                    "category": desc, "summary": text, "important": important,
                    "link": r.get("attchmntFile") or "", "time": parse_time(r.get("an_dt") or r.get("sort_date"))})
    return out


def parse_quote(data, symbol, when):
    """An INDstocks full-quote answer -> {"symbol", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        q = list(trader_data.parse_quotes(doc).values())[0]
    except (ValueError, IndexError, trader_data.DataError):
        raise NewsError("quote for %s is not usable" % symbol)
    price, prev = q.get("ltp"), q.get("prev_close")
    if not price or not prev:
        raise NewsError("quote for %s has no price" % symbol)
    return {"symbol": symbol, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2), "time": when}


def collect_moves(fetcher, watch, now, errors=None, backend=None):
    errors = [] if errors is None else errors
    out = []
    if fetcher.offline:
        for s in sorted(watch):
            try:
                out.append(parse_quote(fetcher.get("", "quote-%s.json" % s), s, now))
            except NewsError as exc:
                if "no file" not in str(exc):
                    errors.append("price for %s: %s" % (s, exc))
        return out
    try:
        b = backend or trader_data.backend()
        known = []
        for s in sorted(watch):
            try:
                b.resolve(s)
                known.append(s)
            except trader_data.DataError as exc:
                errors.append("price for %s: %s" % (s, exc))
        got = b.quote(known) if known else {}
    except trader_data.DataError as exc:
        errors.append("prices: %s" % exc)
        return out
    for s in sorted(watch):
        q = got.get(s)
        if not q or not q.get("ltp") or not q.get("prev_close"):
            continue
        out.append({"symbol": s, "price": round(float(q["ltp"]), 2), "prev_close": round(float(q["prev_close"]), 2),
                    "pct": round((float(q["ltp"]) / float(q["prev_close"]) - 1) * 100, 2), "time": now})
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        alts = [r"\(%s\)" % re.escape(t), r"\b(?:NSE|BSE)\s*:\s*%s\b" % re.escape(t), r"\b%s\b" % re.escape(t)]
        short = search_name(name, "") if name else ""
        if short and len(short) >= 4 and short.upper() != t:
            alts.append(r"\b%s\b" % re.escape(short).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts), re.I)
    return pats


def tag(items, watch):
    """Which of your symbols each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["symbol"]} if it["symbol"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if it["kind"] == "filing":
            cat = (it.get("category") or "").lower()
            if ("result" in cat or "board meeting" in cat) and "earnings" not in it["tags"]:
                it["tags"].insert(0, "earnings")
            if "management" in cat and "people" not in it["tags"]:
                it["tags"].insert(0, "people")
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, names, nse=True, errors=None):
    """Headlines (Yahoo, then Google News when Yahoo is empty) and NSE announcements for every symbol."""
    errors = [] if errors is None else errors
    items = []
    for s in sorted(watch):
        got = []
        try:
            got = parse_rss(fetcher.get(YAHOO % s, "yahoo-%s.xml" % s), s, "Yahoo Finance")
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("Yahoo headlines for %s: %s" % (s, exc))
        if not got:
            q = search_name(names.get(s) or watch.get(s), s)
            try:
                got = parse_rss(fetcher.get(GNEWS % urllib.parse.quote('"%s"' % q), "gnews-%s.xml" % s), s, "Google News")
            except NewsError as exc:
                errors.append("Google News for %s: %s" % (s, exc))
        items += got
        if nse:
            try:
                items += parse_nse(fetcher.get(NSE_ANN % s, "nse-%s.json" % s, nse=True), s)
            except NewsError as exc:
                if not (fetcher.offline and "no file" in str(exc)):
                    errors.append("NSE announcements for %s: %s" % (s, exc))
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = dict((k, v) for k, v in seen.items() if v >= cutoff)
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % to_ist(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append("%-12s %s %+.2f%% vs the previous close %s%s  %s" % (
            m["symbol"], "{:,.2f}".format(m["price"]), m["pct"], "{:,.2f}".format(m["prev_close"]), when, flag))
    return out


LINK_CHARS = 80


def short_link(url, n=LINK_CHARS):
    """A link that fits on a terminal line. Google News redirect links run to 400 characters of
    token; they are kept whole in --json and trader/state/news.json, cut here with a note."""
    url = url or ""
    if len(url) <= n:
        return url
    return url[:n] + "... (the full link is in --json)"


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["symbol"]
        out.append("%s %s  %-12s %-16s %s" % (mark, when, who, it["source"][:16], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        if it["link"]:
            extra.append(short_link(it["link"]))
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % to_ist(now).strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", "--tickers", dest="symbols", nargs="*", help="default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--universe", default="trader/universe.txt", help="company names for the Google News query")
    ap.add_argument("--master", help="a saved instruments CSV, for company names the universe lacks")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-nse", action="store_true", help="headlines only, no NSE announcements")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per symbol")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time, for reproducible runs (no offset = IST)")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:   # a Windows console in cp1252 cannot print a rupee sign or a curly quote from a feed
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.symbols:
        watch = dict((t.upper(), None) for t in args.symbols)
        known = watch_from_files(args.portfolio, args.watchlist, args.plan)
        watch.update(dict((t, known[t]) for t in watch if known.get(t)))
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if not watch:
        print("news: no symbols. Pass --symbols, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    if args.now:
        now = parse_time(args.now)
        if now is None:
            print("news: --now wants an ISO time like 2026-11-03T10:00:00+05:30", file=sys.stderr)
            return 2
        if "+" not in args.now and "Z" not in args.now.upper() and not re.search(r"-\d\d:\d\d$", args.now):
            now = now - _dt.timedelta(hours=5, minutes=30)   # a bare time is IST
    else:
        now = _dt.datetime.now(_dt.timezone.utc)
    names = dict((s, n) for s, n in watch.items() if n)
    names.update(names_from_master([s for s in watch if s not in names], args.master, args.universe))
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    items = collect(fetcher, watch, names, nse=not args.no_nse, errors=errors)
    items = dedupe(tag(items, dict((s, names.get(s)) for s in watch)))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, now, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())

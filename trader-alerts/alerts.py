#!/usr/bin/env python3
"""Price alerts and the NSE clock for trader-alerts. Standard library only.

    python3 alerts.py clock                          # is the NSE open? next open / close
    python3 alerts.py quote TATACONSUM --write       # quotes -> trader/prices.json
    python3 alerts.py quote --holdings --write       # every symbol in your portfolio and plan
    python3 alerts.py add --from-plan                # stop, target and flat-by from trader/plan.json
    python3 alerts.py add --symbol TATACONSUM --stop 1039.40
    python3 alerts.py add --symbol TATACONSUM --target 1061.00
    python3 alerts.py add --at 15:15 --label "flat by close"
    python3 alerts.py add --symbol RELIANCE --pct 3
    python3 alerts.py add --symbol RELIANCE --circuit
    python3 alerts.py add --opening-bell
    python3 alerts.py check                          # rings what hit, once; exit 1 if nothing rang
    python3 alerts.py check --now "2026-11-03 15:15" --offline tests/fixtures/trader-alerts
    python3 alerts.py list
    python3 alerts.py reset tataconsum-plan-stop

Rule types:
  above / target  the price is at or above "price"
  below / stop    the price is at or below "price"
  pct_move        the price moved "pct" percent or more from the previous close
                  (or from "ref" if you give one); "direction": "up" or "down"
  circuit         the price sits at the day's upper or lower circuit band (the
                  quote's own band): a sell may not fill at the upper band, a buy
                  not at the lower; rings once per day
  opening_bell    the NSE opened today (rings once per trading day)
  time            the IST clock passed "at" (HH:MM) on a trading day (rings once
                  per trading day)

A rule with "date" is only watched on that date (rules from a plan are for the
plan's day only). An above / below alert rings ONCE and then stays quiet until
you reset it, unless the rule has "rearm": true (it re-arms when the price
crosses back). A pct_move or circuit alert rings at most once per day.

Quotes: INDstocks full quotes through ../trader_data.py (INDSTOCKS_TOKEN), one
request for every symbol. They are exchange quotes with a few seconds of lag,
for alerts and sizing checks, never for timing an order. --offline DIR reads
DIR/indstocks-<SYMBOL>.json (the /market/quotes/full answer) instead.

NSE calendar: trader_data's 2026 holiday list (nseindia.com, read 2026-11-04).
Outside that year the clock says the holiday list is unknown.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # realpath: works through a junction
import trader_data  # noqa: E402

HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
SQUARE_OFF = "15:20"
NSE_HOLIDAYS = trader_data.NSE_HOLIDAYS
MUHURAT = trader_data.MUHURAT
CALENDAR_YEARS = trader_data.CALENDAR_YEARS
OPEN = trader_data.OPEN
CLOSE = trader_data.CLOSE
trading_day = trader_data.trading_day
session = trader_data.session


class AlertError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def now_ist():
    return trader_data.now_ist().replace(microsecond=0)


def parse_now(text):
    """-> naive IST. A time without an offset is IST; Z or an offset is converted."""
    if not text:
        return now_ist()
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise AlertError("--now wants a time like \"2026-11-03 10:00\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return dt
    return (dt - dt.utcoffset()).replace(tzinfo=None) + trader_data.IST


def iso(ist):
    return ist.replace(microsecond=0).isoformat() + "+05:30"


def market_status(now):
    d = now.date()
    ok, reason = trading_day(d)
    st = {"now_ist": now.strftime("%Y-%m-%d %H:%M"), "date": d.isoformat(),
          "weekday": now.strftime("%A"), "calendar_known": d.year in CALENDAR_YEARS,
          "open": False, "reason": reason, "muhurat": MUHURAT.get(d.isoformat())}
    s = session(d)
    if s:
        o, c = s
        st["opens"], st["closes"] = o.strftime("%H:%M"), c.strftime("%H:%M")
        if o <= now.time() < c:
            st["open"] = True
            st["reason"] = "regular session"
        elif now.time() < o:
            st["reason"] = "before the open"
        else:
            st["reason"] = "after the close"
    nd = d if (s and now.time() < s[0]) else d + datetime.timedelta(days=1)
    for _ in range(20):
        if session(nd):
            st["next_open"] = "%s %s" % (nd.isoformat(), OPEN.strftime("%H:%M"))
            break
        nd += datetime.timedelta(days=1)
    return st


# ---------------------------------------------------------------- quotes

def parse_quote_doc(text_or_doc, sym, when):
    """An INDstocks /market/quotes/full answer (any one code) -> our quote for sym."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    try:
        parsed = trader_data.parse_quotes(doc)
    except trader_data.DataError as e:
        raise AlertError("the quote for %s is not usable: %s" % (sym, e))
    if not parsed:
        raise AlertError("INDstocks has no quote for %s" % sym)
    q = list(parsed.values())[0]
    return quote_of(sym, q, when)


def quote_of(sym, q, when):
    if q.get("ltp") is None:
        raise AlertError("INDstocks has no last price for %s" % sym)
    return {"symbol": sym.upper(), "price": float(q["ltp"]), "open": q.get("open"), "high": q.get("high"),
            "low": q.get("low"), "prev_close": q.get("prev_close"), "upper_circuit": q.get("upper_circuit"),
            "lower_circuit": q.get("lower_circuit"), "volume": q.get("volume"), "time": iso(when),
            "currency": "INR", "source": "indstocks"}


def _offline_text(folder, sym):
    base = os.path.join(folder, "indstocks-%s.json" % sym)
    if os.path.exists(base):
        with open(base, encoding="utf-8") as fh:
            return fh.read()
    if os.path.exists(base + ".gz"):
        with gzip.open(base + ".gz", "rt", encoding="utf-8") as fh:
            return fh.read()
    raise AlertError("no offline quote file %s" % base)


def get_quotes(symbols, offline=None, backend=None, when=None):
    """{SYM: quote} and [errors]. One INDstocks request for every symbol (up to 1,000)."""
    when = when or now_ist()
    quotes, errors = {}, []
    symbols = [s.upper() for s in symbols]
    if offline:
        for sym in symbols:
            try:
                quotes[sym] = parse_quote_doc(_offline_text(offline, sym), sym, when)
            except AlertError as e:
                errors.append(str(e))
        return quotes, errors
    b = backend or trader_data.backend()
    known = []
    for sym in symbols:
        try:
            b.resolve(sym)
            known.append(sym)
        except trader_data.DataError as e:
            errors.append(str(e))
    if not known:
        return quotes, errors
    try:
        got = b.quote(known)
    except trader_data.DataError as e:
        errors.append("INDstocks quotes: %s" % e)
        return quotes, errors
    for sym in known:
        q = got.get(sym)
        if not q:
            errors.append("INDstocks sent no quote for %s" % sym)
            continue
        try:
            quotes[sym] = quote_of(sym, q, when)
        except AlertError as e:
            errors.append(str(e))
    return quotes, errors


def write_prices(path, quotes, now):
    old = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                old = json.load(fh)
        except (ValueError, OSError):
            old = {}
    prices = dict(old.get("prices", {}))
    detail = dict(old.get("detail", {}))
    stamp = iso(now)
    for s, q in quotes.items():
        prices[s] = q["price"]
        detail[s] = {"prev_close": q.get("prev_close"), "upper_circuit": q.get("upper_circuit"),
                     "lower_circuit": q.get("lower_circuit"), "time": q.get("time"), "source": q.get("source"),
                     "fetched": stamp}
    doc = {"as_of": stamp, "source": "indstocks", "currency": "INR",
           "note": ("Exchange quotes with a few seconds of lag, for alerts and sizing checks. The exchange's "
                    "price is the one that fills. detail.fetched is when each price was read."),
           "prices": prices, "detail": detail}
    _write(path, doc)
    return doc


def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read(path, default):
    if not path or not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise AlertError("%s is not valid JSON: %s" % (path, e))


# ---------------------------------------------------------------- rules

PRICE_TYPES = {"above", "below", "stop", "target", "pct_move", "circuit", "opening_bell", "time"}
NO_QUOTE = {"opening_bell", "time"}


def _hhmm(text):
    m = HHMM.match(str(text or "").strip())
    if not m:
        return None
    return datetime.time(int(m.group(1)), int(m.group(2)))


def validate(rule):
    t = rule.get("type")
    if t not in PRICE_TYPES:
        raise AlertError("rule %s: unknown type %r" % (rule.get("id"), t))
    if t not in NO_QUOTE and not rule.get("symbol"):
        raise AlertError("rule %s needs a symbol" % rule.get("id"))
    if t in ("above", "below", "stop", "target") and not isinstance(rule.get("price"), (int, float)):
        raise AlertError("rule %s needs a numeric price" % rule.get("id"))
    if t == "pct_move" and not isinstance(rule.get("pct"), (int, float)):
        raise AlertError("rule %s needs a numeric pct" % rule.get("id"))
    if t == "time" and _hhmm(rule.get("at")) is None:
        raise AlertError("rule %s needs \"at\" as an IST time like \"15:15\"" % rule.get("id"))
    if rule.get("date") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(rule["date"])):
        raise AlertError("rule %s: date must be YYYY-MM-DD" % rule.get("id"))


def active(rule, today):
    """A rule with a "date" is watched on that date only."""
    return not rule.get("date") or str(rule["date"]) == today


def _rang_today(stamp, today):
    return bool(stamp) and str(stamp)[:10] == today


def evaluate(rules, quotes, status, state, now):
    """Pure function. Returns (rings, new_state). Each ring: {id, symbol, message}."""
    state = json.loads(json.dumps(state or {}))
    rung = state.setdefault("rung", {})
    today = status["date"]
    stamp = iso(now)
    rings = []
    for r in rules:
        validate(r)
        rid = r.get("id") or "%s-%s" % (r.get("symbol", "market"), r["type"])
        if not active(r, today):
            continue
        typ = {"stop": "below", "target": "above"}.get(r["type"], r["type"])
        if typ == "opening_bell":
            if status["open"] and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": "", "message": "Opening bell: the NSE is open (%s IST)."
                              % status["now_ist"]})
                rung[rid] = stamp
            continue
        if typ == "time":
            at = _hhmm(r.get("at"))
            if status.get("opens") and now.time() >= at and not _rang_today(rung.get(rid), today):
                label = r.get("label") or "time alert"
                msg = "%s%s: it is %s IST (set for %s)" % (label[:1].upper(), label[1:], now.strftime("%H:%M"),
                                                           at.strftime("%H:%M"))
                msg += (", the market closes at %s and the broker squares off MIS from about %s." % (status["closes"], SQUARE_OFF)) \
                    if status["open"] else ", the market has closed."
                sym = str(r.get("symbol") or "").upper()
                if sym:
                    msg += (" If %s is still open, close it: trader-broker flatten --symbol %s builds the sell "
                            "ticket, then trader-risk and your yes." % (sym, sym))
                rings.append({"id": rid, "symbol": sym, "message": msg})
                rung[rid] = stamp
            continue
        sym = r["symbol"].upper()
        q = quotes.get(sym)
        if not q:
            continue
        p = q["price"]
        if typ in ("above", "below"):
            hit = p >= r["price"] if typ == "above" else p <= r["price"]
            if hit and rid not in rung:
                word = "at or above" if typ == "above" else "at or below"
                label = {"stop": "stop", "target": "target"}.get(r["type"], "alert level")
                rings.append({"id": rid, "symbol": sym, "message": "%s %s Rs %.2f: %s your %s Rs %s%s."
                              % (sym, "is" if status["open"] else "last", p, word, label, _n(r["price"]),
                                 (" (" + r["note"] + ")") if r.get("note") else "")})
                rung[rid] = stamp
            elif not hit and rid in rung and r.get("rearm"):
                del rung[rid]
        elif typ == "pct_move":
            ref = r.get("ref") or q.get("prev_close") or q.get("open")
            if not ref:
                continue
            move = 100.0 * (p / float(ref) - 1)
            want = r.get("direction")
            hit = abs(move) >= r["pct"] and (not want or (want == "up") == (move > 0))
            if hit and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message": "%s moved %+.2f%% to Rs %.2f (from Rs %.2f)."
                              % (sym, move, p, float(ref))})
                rung[rid] = stamp
        elif typ == "circuit":
            up, lo = q.get("upper_circuit"), q.get("lower_circuit")
            side = None
            if up and p >= float(up) - 1e-9:
                side, band = "upper", up
            elif lo and p <= float(lo) + 1e-9:
                side, band = "lower", lo
            if side and not _rang_today(rung.get(rid), today):
                rings.append({"id": rid, "symbol": sym, "message":
                              "%s is Rs %.2f, at its %s circuit band Rs %.2f: %s." % (
                                  sym, p, side, float(band),
                                  "a sell may not fill until it comes off the band" if side == "upper"
                                  else "a buy may not fill, and a stop below it cannot")})
                rung[rid] = stamp
    state["last_check"] = stamp
    return rings, state


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def rules_from_plan(plan, cfg_flat_by=None):
    """trader/plan.json -> a stop alert, a target alert and a flat-by time alert, for the plan's day."""
    if not isinstance(plan, dict) or not plan.get("symbol"):
        raise AlertError("the plan has no symbol (run trader-exit first)")
    sym = str(plan["symbol"]).upper()
    out = []
    for key, typ, word in (("stop", "stop", "plan stop"), ("target", "target", "plan target")):
        v = plan.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            raise AlertError("the plan has no %s (run trader-exit)" % key)
        label = plan.get("%s_label" % key)
        out.append({"id": "%s-plan-%s" % (sym.lower(), key), "symbol": sym, "type": typ, "price": float(v),
                    "note": word + (": " + label if label else "")})
    flat = plan.get("flat_by") or cfg_flat_by or "15:15"
    if _hhmm(flat) is None:
        raise AlertError("the plan's flat_by %r is not a time like 15:15" % flat)
    out.append({"id": "%s-flat-by" % sym.lower(), "symbol": sym, "type": "time", "at": flat, "label": "flat by close"})
    if plan.get("date"):
        for r in out:
            r["date"] = str(plan["date"])
    return out


# ---------------------------------------------------------------- commands

def holdings(a):
    syms = []
    pf = _read(a.portfolio, {})
    for p in pf.get("positions", []) if isinstance(pf, dict) else []:
        syms.append(str(p.get("symbol") or p.get("ticker") or "").upper())
    plan = _read(a.plan, {})
    if isinstance(plan, dict) and plan.get("symbol"):
        syms.append(str(plan["symbol"]).upper())
    return [s for s in syms if s]


def _dedupe(items):
    out = []
    for s in items:
        if s and s not in out:
            out.append(s)
    return out


def cmd_clock(a):
    st = market_status(parse_now(a.now))
    if a.json:
        print(json.dumps(st, indent=2))
    else:
        print("IST %s (%s): NSE %s, %s." % (st["now_ist"], st["weekday"], "OPEN" if st["open"] else "closed",
                                            st["reason"]))
        if st.get("opens"):
            print("Today's session: %s to %s; brokers square off MIS from about %s." % (st["opens"], st["closes"], SQUARE_OFF))
        if st.get("muhurat"):
            print("Muhurat trading today: %s. This pack does not trade it." % st["muhurat"])
        if not st["open"] and st.get("next_open"):
            print("Next open: %s IST." % st["next_open"])
        if not st["calendar_known"]:
            print("Holiday list covers %s only: check nseindia.com for this date." % " and ".join(map(str, CALENDAR_YEARS)))
    return 0 if st["open"] else 1


def cmd_quote(a):
    syms = _dedupe([s.upper() for s in a.symbols] + (holdings(a) if a.holdings else []))
    if not syms:
        raise AlertError("which symbols? alerts.py quote TATACONSUM RELIANCE, or --holdings")
    now = parse_now(a.now)
    quotes, errors = get_quotes(syms, a.offline, when=now)
    if a.write and quotes:
        write_prices(a.write, quotes, now)
    if a.json:
        print(json.dumps({"quotes": quotes, "errors": errors}, indent=2))
    else:
        for s in syms:
            q = quotes.get(s)
            if q:
                chg = ""
                if q.get("prev_close"):
                    chg = " (%+.2f%% vs prev close Rs %.2f)" % (100.0 * (q["price"] / q["prev_close"] - 1), q["prev_close"])
                band = ""
                if q.get("lower_circuit") and q.get("upper_circuit"):
                    band = "  band %.2f to %.2f" % (q["lower_circuit"], q["upper_circuit"])
                print("%-12s Rs %.2f%s%s  %s  [%s]" % (s, q["price"], chg, band, q.get("time", ""), q["source"]))
        for e in errors:
            print("error: " + e)
        if a.write and quotes:
            print("Wrote %d price(s) to %s." % (len(quotes), a.write))
        print("Quotes lag the exchange by a few seconds: for alerts only. The exchange's price is the one that fills.")
    return 0 if quotes else 1


def cmd_check(a):
    doc = _read(a.rules, None)
    if not doc or not doc.get("rules"):
        raise AlertError("no rules in %s: add some with alerts.py add --from-plan" % a.rules)
    rules = doc["rules"]
    now = parse_now(a.now)
    status = market_status(now)
    watched = [r for r in rules if active(r, status["date"])]
    syms = _dedupe([str(r.get("symbol", "")).upper() for r in watched if r.get("type") not in NO_QUOTE])
    quotes, errors = (get_quotes(syms, a.offline, when=now) if syms else ({}, []))
    state = _read(a.state, {})
    rings, state = evaluate(rules, quotes, status, state, now)
    if not a.no_write:
        _write(a.state, state)
        if quotes:
            write_prices(a.prices_out, quotes, now)
    if a.json:
        print(json.dumps({"market": status, "rings": rings, "errors": errors,
                          "quotes": dict((s, q["price"]) for s, q in quotes.items())}, indent=2))
    else:
        print("NSE %s (%s IST, %s)." % ("OPEN" if status["open"] else "closed", status["now_ist"], status["reason"]))
        for r in rings:
            print("ALERT " + r["message"])
        if not rings:
            print("Nothing hit. %d rule(s) watched today%s." % (
                len(watched), (", " + ", ".join("%s Rs %.2f" % (s, quotes[s]["price"]) for s in syms if s in quotes))
                if quotes else ""))
        for e in errors:
            print("error: " + e)
        if rings:
            print("An alert is information, not an order. A smart order's stop-loss and target legs already sit at "
                  "the broker (or on the paper ledger): check the order book and record any fill (ticket.py fill, "
                  "or ticket.py paper tick). To close a position yourself: ticket.py flatten, then trader-risk and "
                  "your yes.")
    return 0 if rings else 1


def cmd_add(a):
    doc = _read(a.rules, {"rules": []})
    doc.setdefault("rules", [])
    if a.from_plan:
        plan = _read(a.plan, None)
        if plan is None:
            raise AlertError("no plan at %s (run trader-exit first)" % a.plan)
        cfg = _read(a.config, {}) if a.config else {}
        new = rules_from_plan(plan, (cfg or {}).get("flat_by"))
        ids = set(r["id"] for r in new)
        doc["rules"] = [r for r in doc["rules"] if r.get("id") not in ids] + new
        state = _read(a.state, {})
        if state.get("rung"):
            for i in ids:
                state["rung"].pop(i, None)
            _write(a.state, state)
        _write(a.rules, doc)
        for r in new:
            print("Added %s: %s" % (r["id"], json.dumps(r)))
        print("Run alerts.py check every few minutes while the trade is open (a scheduled task or /loop). "
              "The broker's stop-loss and target legs are what actually get you out; these alerts tell you when.")
        return 0
    if a.opening_bell:
        rule = {"id": a.id or "opening-bell", "type": "opening_bell"}
    elif a.at:
        if _hhmm(a.at) is None:
            raise AlertError("--at wants an IST time like 15:15")
        rule = {"id": a.id or ("time-%s" % a.at.replace(":", "")), "type": "time", "at": a.at,
                "label": a.label or "time alert"}
        if a.symbol:
            rule["symbol"] = a.symbol.upper()
    else:
        if not a.symbol:
            raise AlertError("--symbol is required (or --from-plan, --at, --opening-bell)")
        sym = a.symbol.upper()
        if a.above is not None:
            rule = {"type": "above", "price": a.above}
        elif a.below is not None:
            rule = {"type": "below", "price": a.below}
        elif a.stop is not None:
            rule = {"type": "stop", "price": a.stop}
        elif a.target is not None:
            rule = {"type": "target", "price": a.target}
        elif a.pct is not None:
            rule = {"type": "pct_move", "pct": a.pct}
            if a.direction:
                rule["direction"] = a.direction
        elif a.circuit:
            rule = {"type": "circuit"}
        else:
            raise AlertError("give one of --above, --below, --stop, --target, --pct or --circuit")
        val = rule.get("price", rule.get("pct"))
        rid = a.id or ("%s-%s" % (sym.lower(), rule["type"]) + ("-%s" % _n(val) if val is not None else ""))
        rule = dict({"id": rid}, symbol=sym, **rule)
        if a.rearm:
            rule["rearm"] = True
        if a.note:
            rule["note"] = a.note
    if a.date:
        rule["date"] = a.date
    validate(rule)
    if any(r.get("id") == rule["id"] for r in doc["rules"]):
        raise AlertError("a rule with id %s already exists" % rule["id"])
    doc["rules"].append(rule)
    _write(a.rules, doc)
    print("Added %s: %s" % (rule["id"], json.dumps(rule)))
    return 0


def cmd_list(a):
    doc = _read(a.rules, {"rules": []})
    state = _read(a.state, {})
    if not doc.get("rules"):
        print("No rules yet.")
        return 1
    for r in doc["rules"]:
        rid = r.get("id")
        rung = state.get("rung", {}).get(rid)
        print("%-28s %s%s" % (rid, json.dumps(dict((k, v) for k, v in r.items() if k != "id")),
                              ("  (rang %s)" % rung) if rung else ""))
    return 0


def cmd_reset(a):
    state = _read(a.state, {})
    if a.id not in state.get("rung", {}):
        print("%s has not rung (nothing to reset)." % a.id)
        return 1
    del state["rung"][a.id]
    _write(a.state, state)
    print("Re-armed %s." % a.id)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Price alerts and the NSE clock (trader-alerts).")
    sub = ap.add_subparsers(dest="cmd")

    def net(p):
        p.add_argument("--offline", metavar="DIR", help="read indstocks-<SYMBOL>.json files, no network")
        p.add_argument("--now", help="pretend it is this time (no offset = IST)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("clock", help="is the NSE open?")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quote", help="latest quotes")
    net(p)
    p.add_argument("symbols", nargs="*")
    p.add_argument("--holdings", action="store_true", help="add symbols from your portfolio and plan")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="PATH",
                   help="save to prices.json (default trader/prices.json)")
    p = sub.add_parser("check", help="check every rule, ring what hit")
    net(p)
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--prices-out", default="trader/prices.json")
    p.add_argument("--no-write", action="store_true")
    p = sub.add_parser("add", help="add a rule")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p.add_argument("--symbol")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--above", type=float)
    g.add_argument("--below", type=float)
    g.add_argument("--stop", type=float)
    g.add_argument("--target", type=float)
    g.add_argument("--pct", type=float)
    g.add_argument("--circuit", action="store_true", help="ring when the price sits at the day's circuit band")
    g.add_argument("--opening-bell", action="store_true")
    g.add_argument("--at", metavar="HH:MM", help="a time alert, IST")
    g.add_argument("--from-plan", action="store_true", help="stop, target and flat-by from the plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json", help="flat_by when the plan has none")
    p.add_argument("--label", help="what a time alert says, e.g. \"flat by close\"")
    p.add_argument("--date", help="watch this rule on this date only (YYYY-MM-DD)")
    p.add_argument("--direction", choices=["up", "down"])
    p.add_argument("--rearm", action="store_true")
    p.add_argument("--note")
    p.add_argument("--id")
    p = sub.add_parser("list", help="list the rules")
    p.add_argument("--rules", default="trader/alerts.json")
    p.add_argument("--state", default="trader/state/alerts.json")
    p = sub.add_parser("reset", help="re-arm a rule that rang")
    p.add_argument("id")
    p.add_argument("--state", default="trader/state/alerts.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"clock": cmd_clock, "quote": cmd_quote, "check": cmd_check, "add": cmd_add,
                "list": cmd_list, "reset": cmd_reset}[a.cmd](a)
    except AlertError as e:
        print("alerts.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

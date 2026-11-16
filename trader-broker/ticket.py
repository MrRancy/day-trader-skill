#!/usr/bin/env python3
"""Order tickets, the decision log and the paper ledger for trader-broker (NSE, INDstocks).
Standard library only.

Nothing here places an order on its own. It does its work around the one
moment that matters, your yes:

  show      renders each proposed order as a plain-English ticket plus the
            exact JSON body for INDstocks' POST /smart/order (or /order). A
            smart order shows its stop-loss leg, its target leg and the max
            loss at the stop, charges included. An order is READY only if
            trader-risk passed THIS exact order today (matched by fingerprint)
            and nobody has decided on it yet.
  approve   records your explicit yes for ONE order, with your reply word for
            word. It refuses "yes to all", "sure", "ok", a yes that names a
            different stock or amount, and any order that is not READY.
  decline   records a no.
  place     sends an APPROVED order. Paper (the default): the local ledger
            trader/paper.json fills it at the quoted last price and keeps the
            stop-loss and target legs pending. Live ("paper": false AND --live):
            POST /smart/order at INDstocks, which also needs your static IP
            whitelisted there.
  result    records what happened when you placed an order yourself:
            placed (with the broker's order id), denied, or failed.
  fill      records a fill in trader/portfolio.json (cash and shares, charges off).
  flatten   builds a sell ticket that closes a symbol's open position by
            flat_by. It still goes through trader-risk and your yes.
  paper     the ledger: `paper tick` moves pending legs and limit orders
            against a price (a flag, trader/prices.json, or a live quote) and
            squares off INTRADAY positions after 15:20, like the broker does;
            `paper show` prints cash, positions, open legs and charges paid.
  sync      live only: reads INDstocks' order book and positions.
  log       prints today's decisions.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py place --index 1                     # paper: fills at the live quote
    python3 ticket.py place --index 1 --price 1050.50     # paper: fills at this price
    python3 ticket.py paper tick                          # legs vs the live quote; square-off after 15:20
    python3 ticket.py paper tick --price TATACONSUM=1061.20
    python3 ticket.py paper show
    python3 ticket.py fill --symbol TATACONSUM --side sell --qty 71 --price 1061.00
    python3 ticket.py flatten --symbol TATACONSUM --qty 71
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only). Dates are IST;
--now "2026-11-03 10:01" pins the clock.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # realpath: works through a junction
import trader_data  # noqa: E402

# the fields INDstocks reads; the fingerprint (shared with trader-risk/check.py) covers these only
INDSTOCKS_FIELDS = ("security_id", "exchange", "segment", "product", "txn_type", "order_type", "validity",
                    "qty", "limit_price", "trigger_price", "trigger_limit_price", "sl_trigger_price",
                    "sl_limit_price", "tgt_trigger_price", "tgt_limit_price", "is_amo", "symbol")
# the request body: everything INDstocks reads except our symbol, plus the tags it accepts
API_FIELDS = ("txn_type", "exchange", "segment", "product", "order_type", "validity", "security_id", "qty",
              "limit_price", "trigger_price", "trigger_limit_price", "sl_trigger_price", "sl_limit_price",
              "tgt_trigger_price", "tgt_limit_price", "is_amo", "algo_id", "remarks")
PRICE_FIELDS = ("limit_price", "trigger_price", "trigger_limit_price", "sl_trigger_price", "sl_limit_price",
                "tgt_trigger_price", "tgt_limit_price")
SMART_FIELDS = ("trigger_price", "trigger_limit_price", "sl_trigger_price", "tgt_trigger_price")
VALIDITY_WORDS = {"DAY": "good for today"}
PRODUCT_WORDS = {"INTRADAY": "INTRADAY (MIS, squared off from about 15:20)", "CNC": "CNC (delivery)",
                 "MARGIN": "MARGIN"}
YES_WORDS = ("yes", "y", "haan", "ha")
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each", "sab", "sabhi"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if", "nahi", "ruko"}
SQUARE_OFF = datetime.time(15, 20)
CLOSE = trader_data.CLOSE
FRESH_MINUTES = 15
charges_leg = trader_data.charges_leg


class TicketError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def now_ist():
    return trader_data.now_ist().replace(microsecond=0)


def parse_now(text):
    """-> naive IST. A time without an offset is IST."""
    if not text:
        return now_ist()
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TicketError("--now wants a time like \"2026-11-03 10:01\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return dt
    return (dt - dt.utcoffset()).replace(tzinfo=None) + trader_data.IST


def parse_ts(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None) + trader_data.IST
    return dt


def ist_iso(now):
    """naive IST -> "2026-11-03T10:01:00+05:30"."""
    return now.replace(microsecond=0).isoformat() + "+05:30"


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def rec_day(rec):
    """The trading day a decision belongs to: its "date" (the day it was made under), else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rates_of(cfg):
    r = dict(trader_data.CHARGE_RATES)
    r.update((cfg or {}).get("charges") or {})
    return r


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function as trader-risk/check.py: identifies the exact order."""
    core = dict((k, order[k]) for k in INDSTOCKS_FIELDS if k in order and order[k] is not None)
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _num(v):
    f = float(v)
    return str(int(f)) if f == int(f) else repr(f)


def _money(v):
    f = float(v)
    s = "{:,.2f}".format(f)
    return "Rs " + (s[:-3] if s.endswith(".00") else s)


def _p(v):
    return "%.2f" % float(v)


def side_of(order):
    return str(order.get("txn_type") or order.get("side") or "").strip().upper()


def is_smart(order):
    return any(order.get(k) is not None for k in SMART_FIELDS)


def api_body(order):
    """The exact JSON body for POST /smart/order (a smart order) or POST /order: INDstocks' field
    names, qty an integer, prices numbers. "symbol", "why" and "plan" are ours and are never sent."""
    out = {}
    for k in API_FIELDS:
        if k in order and order[k] is not None:
            v = order[k]
            if k == "qty":
                v = int(round(float(v)))
            elif k in PRICE_FIELDS:
                v = round(float(v), 2)
            out[k] = v
    out.setdefault("validity", "DAY")
    return out


def api_path(order):
    return "/smart/order" if is_smart(order) else "/order"


def max_loss(order, rates=None):
    """(rupees lost if the stop fills exactly at the stop, charges included, stop) or (None, stop)."""
    plan = order.get("plan") or {}
    stop = _f(order.get("sl_trigger_price")) or _f(plan.get("stop"))
    q = _f(order.get("qty"))
    if not stop or not q:
        return None, stop
    if str(order.get("order_type", "MARKET")).upper() == "LIMIT" and _f(order.get("limit_price")):
        entry = _f(order["limit_price"])
    elif _f(plan.get("max_loss_net")) is not None:
        return round(_f(plan["max_loss_net"]), 2), stop
    else:
        entry = _f(plan.get("entry"))
    if not entry:
        return None, stop
    risk = abs(entry - stop)
    price_loss = round(q * int(round(risk * 100)) / 100.0, 2)
    long = side_of(order) == "BUY"
    chg = trader_data.charges(int(q), entry if long else stop, stop if long else entry, rates)["total"]
    return round(price_loss + chg, 2), stop


def plain_parts(order, rates=None):
    """The ticket in plain words, one part per line."""
    side = side_of(order)
    sym = order.get("symbol", "?")
    typ = str(order.get("order_type") or "MARKET").upper()
    product = str(order.get("product") or "INTRADAY").upper()
    validity = VALIDITY_WORDS.get(str(order.get("validity") or "DAY").upper(), order.get("validity"))
    q = _num(order.get("qty") or 0)
    if typ == "LIMIT" and order.get("limit_price") is not None:
        how = "limit %s" % _p(order["limit_price"])
    elif typ == "TRIGGER":
        how = "trigger %s" % _p(order.get("trigger_price") or 0)
        if order.get("trigger_limit_price") is not None:
            how += " (limit %s)" % _p(order["trigger_limit_price"])
    else:
        how = "market"
    parts = ["%s %s %s, %s, %s, %s" % (side, q, sym, PRODUCT_WORDS.get(product, product), how, validity)]
    if order.get("sl_trigger_price") is not None:
        leg = "stop-loss trigger %s" % _p(order["sl_trigger_price"])
        if order.get("sl_limit_price") is not None:
            leg += " (limit %s)" % _p(order["sl_limit_price"])
        parts.append(leg)
    if order.get("tgt_trigger_price") is not None:
        leg = "target trigger %s" % _p(order["tgt_trigger_price"])
        if order.get("tgt_limit_price") is not None:
            leg += " (limit %s)" % _p(order["tgt_limit_price"])
        parts.append(leg)
    loss, stop = max_loss(order, rates)
    if loss is not None:
        parts.append("max loss about %s, charges included, if the stop fills at %s (it can fill worse in a fast market)"
                     % (_money(loss), _p(stop)))
    return parts


def plain(order, rates=None):
    """BUY 71 TATACONSUM, INTRADAY (MIS, ...), market, good for today / stop-loss trigger 1039.40 (limit 1036.80) / ..."""
    return " / ".join(plain_parts(order, rates))


def amount_numbers(order, rates=None):
    nums = []
    for k in ("qty", "limit_price", "trigger_price", "sl_trigger_price", "sl_limit_price", "tgt_trigger_price",
              "tgt_limit_price"):
        if order.get(k) is not None:
            v = _f(order[k])
            if v is not None:
                nums.append(v)
    loss, _ = max_loss(order, rates)
    if loss is not None:
        nums.append(loss)
    plan = order.get("plan") or {}
    for k in ("max_loss", "entry"):
        if _f(plan.get(k)) is not None:
            nums.append(_f(plan[k]))
    return nums


def parse_yes(reply, order, batch_symbols=(), rates=None):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("symbol", "")).upper()
    tokens = set(re.findall(r"[A-Z][A-Z0-9&\-]{0,19}", raw.upper()))
    others = set(s.upper() for s in batch_symbols) - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", raw)]
    if nums:
        want = amount_numbers(order, rates)
        if not any(abs(n - w) < 0.005 for n in nums for w in want):
            return False, "the reply says %s, but this order is %s" % (
                ", ".join(_num(n) for n in nums), plain(order, rates))
    return True, "explicit yes for %s" % plain(order, rates)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "trader-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by trader-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for ticket.py place" % rec.get("at"), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: trader-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    """entry or exit, as trader-risk saw it (older checks: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if side_of(order) == "BUY" else "exit"


# ---------------------------------------------------------------- rendering

def render(order, idx, total, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    rates = rates_of(cfg)
    broker = str(cfg.get("broker", "indstocks")).lower()
    paper = cfg.get("paper", True)
    acct = "PAPER ledger (trader/paper.json)" if paper else "LIVE ACCOUNT, REAL MONEY"
    lines = ["ORDER %d of %d, %s, %s" % (idx, total, acct, "Zerodha Kite" if broker == "kite" else "INDstocks")]
    for part in plain_parts(order, rates):
        lines.append("  " + part)
    if order.get("why"):
        lines.append("  why: " + order["why"])
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    if broker == "kite":
        lines.append("  The kite backend is not built yet: this ticket can be recorded with ticket.py result only.")
    else:
        lines.append("  %s POST %s with exactly:" % ("The paper ledger will simulate" if paper else "ticket.py place --live will send",
                                                     api_path(order)))
        for l in json.dumps(api_body(order), indent=2).splitlines():
            lines.append("    " + l)
        if is_smart(order):
            lines.append("  INDstocks places the entry, then the stop-loss and the target as one GTT child order")
            lines.append("  (OCO): when one leg fills, the other is cancelled.")
    if ready:
        lines.append("  Reply \"yes\" to place this one order, or \"no\" to skip it.")
        lines.append("  Then Claude runs ticket.py place --index %d, which you approve on its own permission prompt." % idx)
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    loss, stop = max_loss(order, rates_of(cfg))
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order, rates_of(cfg)),
            "fingerprint": fingerprint(order), "paper": bool(cfg.get("paper", True)),
            "broker": cfg.get("broker", "indstocks"), "endpoint": "POST " + api_path(order),
            "body": api_body(order), "why": order.get("why", ""),
            "smart": is_smart(order), "max_loss_net": loss, "stop": stop}


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date, charges=None):
    """Record a filled order in trader/portfolio.json: cash (charges off) and shares, with a
    cost-basis lot per buy (oldest lots are relieved first on a sell)."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "currency": "INR", "positions": []}
    symbol = symbol.upper()
    side = str(side).lower()
    qty, price = float(qty), float(price)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    if charges is None:
        charges = charges_leg(int(qty), price, side, rates_of(cfg))["total"]
    cost = round(qty * price, 2)
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("symbol") or p.get("ticker") or "").upper() == symbol:
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"symbol": symbol, "shares": 0.0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": cost})
        pos["shares"] = round(float(pos.get("shares", 0)) + qty, 6)
        pf["cash"] = round(float(pf.get("cash", 0)) - cost - float(charges), 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (_num(qty), symbol, pos.get("shares", 0) if pos else 0))
        left = qty
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = round(float(lot["shares"]) - take, 6)
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        if pos["shares"] <= 1e-9:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + cost - float(charges), 2)
    pf["charges_paid"] = round(float(pf.get("charges_paid", 0)) + float(charges), 2)
    write_json(portfolio_path, pf)
    return pf


def log_fill(state_path, now, today, symbol, side, qty, price, charges=0.0, kind=None, note=None, broker_id=None):
    """A fill in the log, so trader-risk can see what happened today."""
    state = load_state(state_path)
    rec = {"at": ist_iso(now), "date": today, "status": "filled", "symbol": symbol.upper(),
           "txn_type": str(side).upper(), "qty": int(round(float(qty))), "price": float(price),
           "charges": round(float(charges), 2),
           "plain": "%s %s %s at %s" % (str(side).upper(), _num(float(qty)), symbol.upper(), _p(price))}
    if kind:
        rec["kind"] = kind
    if note:
        rec["note"] = note
    if broker_id:
        rec["broker_order_id"] = broker_id
    state.setdefault("orders", []).append(rec)
    write_json(state_path, state)
    return rec


# ---------------------------------------------------------------- the paper ledger

class Paper(object):
    """A simulated INDstocks account in trader/paper.json. Market orders fill at the quoted last
    price; a limit buy fills when the price is at or under the limit; a stop-loss leg fills at its
    trigger when the price reaches it, a target leg at its trigger; INTRADAY positions are squared
    off at the price given after 15:20; pending orders expire at the close. One price per symbol
    per tick, so a tick is a snapshot, not a replay of every trade in between."""

    def __init__(self, path, cfg):
        self.path = path
        self.cfg = cfg or {}
        self.rates = rates_of(cfg)
        # slippage, percent a side, on every market fill (a MARKET entry, a stop-loss leg, the square-off);
        # limit fills (a LIMIT entry, the target leg) fill at their price
        self.slip = float(_f(self.cfg.get("slippage_pct")) or 0.0) / 100.0
        self.doc = read_json(path, "paper ledger", required=False)
        if self.doc is None:
            budget = _f(self.cfg.get("budget"))
            if not budget:
                raise TicketError("no %s yet and no budget in the config to start it from" % path)
            self.doc = {"created_by": "trader-broker", "currency": "INR", "started": None, "cash": float(budget),
                        "starting_cash": float(budget), "positions": {}, "orders": [], "charges_paid": 0.0,
                        "realized": 0.0, "seq": 0}

    def save(self):
        write_json(self.path, self.doc)

    def _id(self, prefix):
        self.doc["seq"] = int(self.doc.get("seq", 0)) + 1
        return "PAPER-%s-%d" % (prefix, self.doc["seq"])

    def slipped(self, price, side):
        """A market fill moved against you by the slippage, onto the 0.05 tick (up for a buy, down for a sell)."""
        if not self.slip:
            return round(price, 2)
        x = price * (1 + self.slip) if side == "BUY" else price * (1 - self.slip)
        n = math.ceil(round(x / 0.05, 6)) if side == "BUY" else math.floor(round(x / 0.05, 6))
        return round(n * 0.05, 2)

    def position(self, sym):
        return self.doc["positions"].get(sym.upper())

    def _fill(self, rec, qty, price, now, what):
        """Settle a fill: cash, position, charges. Returns the event line."""
        sym = rec["symbol"]
        side = rec["txn_type"]
        chg = charges_leg(qty, price, side, self.rates)["total"]
        cost = round(qty * price, 2)
        pos = self.doc["positions"].get(sym)
        if side == "BUY":
            if pos is None:
                pos = {"qty": 0, "avg_price": 0.0, "product": rec.get("product", "INTRADAY"),
                       "security_id": rec.get("security_id")}
                self.doc["positions"][sym] = pos
            total = pos["avg_price"] * pos["qty"] + cost
            pos["qty"] += qty
            pos["avg_price"] = round(total / pos["qty"], 4)
            self.doc["cash"] = round(self.doc["cash"] - cost - chg, 2)
        else:
            held = pos["qty"] if pos else 0
            if held < qty:
                raise TicketError("the paper ledger holds %d %s, not %d: nothing sold" % (held, sym, qty))
            pnl = round((price - pos["avg_price"]) * qty, 2)
            self.doc["realized"] = round(self.doc["realized"] + pnl, 2)
            rec["pnl"] = pnl                       # this sell's result before charges, for the day's journal
            rec["avg_cost"] = pos["avg_price"]
            pos["qty"] -= qty
            if pos["qty"] <= 0:
                del self.doc["positions"][sym]
            self.doc["cash"] = round(self.doc["cash"] + cost - chg, 2)
        self.doc["charges_paid"] = round(self.doc["charges_paid"] + chg, 2)
        rec.update({"status": "SUCCESS", "traded_qty": qty, "traded_price": price, "charges": chg,
                    "updated_at": ist_iso(now), "what": what})
        return {"id": rec["id"], "symbol": sym, "txn_type": side, "qty": qty, "price": price, "charges": chg,
                "what": what, "cash": self.doc["cash"]}

    def _cancel_legs(self, sym, now, why):
        out = []
        for rec in self.doc["orders"]:
            if rec.get("order_type") == "OCO" and rec["symbol"] == sym and rec["status"] == "PENDING":
                rec.update({"status": "CANCELLED", "updated_at": ist_iso(now), "what": why})
                out.append({"id": rec["id"], "symbol": sym, "what": why})
        return out

    def place(self, order, ltp, now):
        """A proposed order -> (parent record, child record or None, events). A MARKET order fills now
        at ltp; a LIMIT buy fills now if ltp <= limit (at ltp), else waits; a SELL that closes a position
        cancels that position's open legs first."""
        sym = str(order.get("symbol", "")).upper()
        side = side_of(order)
        qty = int(round(float(order.get("qty") or 0)))
        if qty <= 0:
            raise TicketError("the paper ledger needs a whole-share qty")
        typ = str(order.get("order_type") or "MARKET").upper()
        if typ == "TRIGGER":
            raise TicketError("the paper ledger does not simulate TRIGGER entries yet: use MARKET or LIMIT")
        if not self.doc.get("started"):
            self.doc["started"] = ist_iso(now)
        events = []
        if side == "SELL":
            events.extend(self._cancel_legs(sym, now, "cancelled: the position is being sold"))
        parent = {"id": self._id("EQ"), "created_at": ist_iso(now), "symbol": sym, "security_id": order.get("security_id"),
                  "txn_type": side, "exchange": order.get("exchange", "NSE"), "segment": order.get("segment", "EQUITY"),
                  "product": str(order.get("product") or "INTRADAY").upper(), "order_type": typ, "validity": "DAY",
                  "requested_qty": qty, "requested_price": _f(order.get("limit_price")), "status": "PENDING",
                  "remarks": order.get("remarks")}
        self.doc["orders"].append(parent)
        child = None
        if is_smart(order):
            child = {"id": self._id("GTT"), "created_at": ist_iso(now), "parent_order_id": parent["id"], "symbol": sym,
                     "txn_type": "SELL" if side == "BUY" else "BUY", "product": parent["product"], "order_type": "OCO",
                     "requested_qty": qty, "sl_trigger_price": _f(order.get("sl_trigger_price")),
                     "sl_limit_price": _f(order.get("sl_limit_price")), "tgt_trigger_price": _f(order.get("tgt_trigger_price")),
                     "tgt_limit_price": _f(order.get("tgt_limit_price")), "status": "PENDING"}
            parent["child_order_id"] = child["id"]
            self.doc["orders"].append(child)
        if ltp is None:
            raise TicketError("the paper ledger needs a price to fill at: --price, trader/prices.json or a live quote")
        ltp = float(ltp)
        limit = _f(order.get("limit_price"))
        fill_at = None
        if typ == "MARKET":
            fill_at = self.slipped(ltp, side)
        elif side == "BUY" and ltp <= limit + 1e-9:
            fill_at = ltp
        elif side == "SELL" and ltp >= limit - 1e-9:
            fill_at = ltp
        if fill_at is not None:
            events.append(self._fill(parent, qty, round(fill_at, 2), now, "filled at the quoted price" if fill_at == ltp
                                     else "filled at the quoted price %s plus slippage" % _p(ltp)))
        else:
            events.append({"id": parent["id"], "symbol": sym, "txn_type": side, "qty": qty, "price": None,
                           "what": "pending: the last price %s is %s the limit %s" % (
                               _p(ltp), "above" if side == "BUY" else "below", _p(limit))})
        self.save()
        return parent, child, events

    def tick(self, prices, now):
        """Move pending orders and legs against {SYM: price}. After 15:20 square off INTRADAY positions;
        at the close expire what is still pending. Returns the events."""
        prices = dict((str(k).upper(), float(v)) for k, v in (prices or {}).items())
        events = []
        t = now.time()
        for rec in list(self.doc["orders"]):
            if rec["status"] != "PENDING":
                continue
            sym = rec["symbol"]
            p = prices.get(sym)
            if rec["order_type"] == "OCO":
                parent = next((o for o in self.doc["orders"] if o["id"] == rec.get("parent_order_id")), None)
                if not parent or parent["status"] != "SUCCESS":
                    continue
                if p is None:
                    continue
                pos = self.doc["positions"].get(sym)
                qty = min(rec["requested_qty"], pos["qty"] if pos else 0)
                if qty <= 0:
                    rec.update({"status": "CANCELLED", "updated_at": ist_iso(now), "what": "cancelled: nothing left to close"})
                    continue
                long = rec["txn_type"] == "SELL"
                sl, tg = rec.get("sl_trigger_price"), rec.get("tgt_trigger_price")
                hit = None
                if sl and ((long and p <= sl + 1e-9) or (not long and p >= sl - 1e-9)):
                    # a stop becomes a market order: a price already through it fills there, not at the trigger
                    hit, at = "stop-loss", self.slipped(min(p, sl) if long else max(p, sl), rec["txn_type"])
                elif tg and ((long and p >= tg - 1e-9) or (not long and p <= tg + 1e-9)):
                    hit, at = "target", tg
                if hit:
                    rec["leg"] = hit
                    events.append(self._fill(rec, qty, round(at, 2), now, (
                        "%s leg filled at its trigger (last %s)" % (hit, _p(p))) if at in (sl, tg) else
                        "%s leg filled through its trigger %s (last %s)" % (hit, _p(sl), _p(p))))
            elif p is not None:
                limit = rec.get("requested_price")
                side = rec["txn_type"]
                if limit and ((side == "BUY" and p <= limit + 1e-9) or (side == "SELL" and p >= limit - 1e-9)):
                    events.append(self._fill(rec, rec["requested_qty"], round(p, 2), now, "limit order filled at the quoted price"))
        if t >= SQUARE_OFF:
            for sym, pos in list(self.doc["positions"].items()):
                if pos.get("product", "INTRADAY") != "INTRADAY" or pos["qty"] <= 0:
                    continue
                p = prices.get(sym)
                if p is None:
                    events.append({"id": None, "symbol": sym, "what": "square-off due but no price for %s: give --price %s=P" % (sym, sym)})
                    continue
                events.extend(self._cancel_legs(sym, now, "cancelled: MIS square-off"))
                rec = {"id": self._id("SQ"), "created_at": ist_iso(now), "symbol": sym, "security_id": pos.get("security_id"),
                       "txn_type": "SELL", "product": "INTRADAY", "order_type": "MARKET", "validity": "DAY",
                       "requested_qty": pos["qty"], "requested_price": None, "status": "PENDING", "remarks": "MIS square-off"}
                self.doc["orders"].append(rec)
                events.append(self._fill(rec, pos["qty"], self.slipped(p, "SELL"), now, "MIS square-off by the broker at the quoted price"))
        if t >= CLOSE:
            for rec in self.doc["orders"]:
                if rec["status"] == "PENDING":
                    rec.update({"status": "EXPIRED", "updated_at": ist_iso(now), "what": "expired at the close"})
                    events.append({"id": rec["id"], "symbol": rec["symbol"], "what": "expired at the close"})
        self.save()
        return events

    def open_legs(self):
        return [o for o in self.doc["orders"] if o["order_type"] == "OCO" and o["status"] == "PENDING"]

    def summary(self):
        d = self.doc
        lines = ["Paper ledger %s: cash %s (started with %s), charges paid %s, realized %s before charges."
                 % (self.path, _money(d["cash"]), _money(d["starting_cash"]), _money(d["charges_paid"]),
                    ("%+.2f" % d["realized"]).replace("+", "+Rs ").replace("-", "-Rs "))]
        if d["positions"]:
            for sym, p in sorted(d["positions"].items()):
                lines.append("  position %s: %d shares at %s average, %s" % (sym, p["qty"], _p(p["avg_price"]), p.get("product")))
        else:
            lines.append("  no open positions")
        legs = self.open_legs()
        for o in legs:
            lines.append("  open leg %s for %s: stop-loss %s, target %s (%d shares)" % (
                o["id"], o["symbol"], _p(o.get("sl_trigger_price") or 0), _p(o.get("tgt_trigger_price") or 0), o["requested_qty"]))
        pend = [o for o in d["orders"] if o["status"] == "PENDING" and o["order_type"] != "OCO"]
        for o in pend:
            lines.append("  pending %s: %s %d %s limit %s" % (o["id"], o["txn_type"], o["requested_qty"], o["symbol"],
                                                              _p(o.get("requested_price") or 0)))
        lines.append("  %d orders in the book. A paper fill at the last price is kinder than a real one." % len(d["orders"]))
        return "\n".join(lines)


# ---------------------------------------------------------------- the live broker (never called in tests)

class INDstocksBroker(object):
    """POST /smart/order, POST /order, the cancels, the order book and the positions, through
    trader_data's token and transport. Order placement needs the account's static IP whitelisted."""

    def __init__(self, api=None):
        self.api = api or trader_data.INDstocks()

    def place(self, order):
        body = api_body(order)
        if not body.get("security_id"):
            body["security_id"] = self.api.resolve(order["symbol"]).split("_", 1)[1]
        doc = self.api.post_json(api_path(order), body)
        data = doc.get("data") or {}
        if doc.get("status") != "success":
            raise TicketError("INDstocks did not accept the order: %s" % (doc.get("message") or doc))
        if "order_data" in data:
            first = (data.get("order_data") or [{}])[0]
            return {"order_id": first.get("order_id"), "order_status": first.get("order_status"),
                    "child_order_id": (first.get("child_order_details") or {}).get("order_id")}
        return {"order_id": data.get("order_id"), "order_status": data.get("order_status"), "child_order_id": None}

    def cancel(self, order_id, smart=True, segment="EQUITY"):
        return self.api.post_json("/smart/order/cancel" if smart else "/order/cancel",
                                  {"order_id": order_id, "segment": segment})

    def order_book(self):
        return (self.api.get_json("/order-book") or {}).get("data") or []

    def positions(self):
        return (self.api.get_json("/portfolio/positions", {"segment": "equity"}) or {}).get("data") or []

    def funds(self):
        return (self.api.get_json("/funds") or {}).get("data") or {}


# ---------------------------------------------------------------- prices

def price_for(sym, a, now, cfg):
    """(price, where from): --price, else a fresh trader/prices.json entry, else a live quote."""
    if getattr(a, "price", None) is not None:
        p = _f(a.price)
        if p is None or p <= 0:
            raise TicketError("--price wants a price above 0")
        return p, "--price"
    ppath = getattr(a, "prices", None) or "trader/prices.json"
    pdoc = read_json(ppath, "prices", required=False) or {}
    p = _f((pdoc.get("prices") or {}).get(sym))
    if p:
        det = ((pdoc.get("detail") or {}).get(sym) or {})
        t = parse_ts(det.get("time")) or parse_ts(det.get("fetched")) or parse_ts(pdoc.get("as_of"))
        if t and -60 <= (now - t).total_seconds() <= FRESH_MINUTES * 60:
            return p, ppath
    if getattr(a, "no_quote", False):
        raise TicketError("no fresh price for %s: pass --price P" % sym)
    try:
        q = trader_data.backend().quote([sym]).get(sym) or {}
    except trader_data.DataError as e:
        raise TicketError("no price for %s: %s Pass --price P." % (sym, str(e).rstrip(".") + "."))
    if not q.get("ltp"):
        raise TicketError("INDstocks sent no last price for %s: pass --price P" % sym)
    return float(q["ltp"]), "INDstocks quote"


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or now.date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-11-03")
    return now, today


def _load(a):
    doc = read_json(a.orders, "orders file (run trader-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    rates = rates_of(cfg)
    ok, why = parse_yes(a.reply, order, [o.get("symbol", "") for o in orders], rates)
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    loss, stop = max_loss(order, rates)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "txn_type": side_of(order),
           "qty": order.get("qty"), "kind": kind_of(order, rv), "max_loss_net": loss, "stop": stop,
           "plain": plain(order, rates), "status": "approved", "reply": a.reply,
           "paper": bool(cfg.get("paper", True)), "broker": cfg.get("broker", "indstocks")}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order, rates)))
    if cfg.get("paper", True):
        print("Now: ticket.py place --index %d (the paper ledger fills it at the quoted last price)." % a.index)
    else:
        print("Now: ticket.py place --index %d --live, which sends POST %s with exactly:" % (a.index, api_path(order)))
        print(json.dumps(api_body(order), indent=2))
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "txn_type": side_of(order),
           "plain": plain(order, rates_of(cfg)), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order, rates_of(cfg))))
    return 0


def _record(state_path, rec, now, today, status, **extra):
    new = {"at": ist_iso(now), "date": today, "index": rec.get("index"), "fingerprint": rec.get("fingerprint"),
           "symbol": rec["symbol"], "txn_type": rec.get("txn_type"), "plain": rec.get("plain"), "status": status}
    for k in ("qty", "kind", "max_loss_net", "stop", "paper"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    new.update(dict((k, v) for k, v in extra.items() if v is not None))
    state = load_state(state_path)
    state.setdefault("orders", []).append(new)
    write_json(state_path, state)
    return new


def cmd_place(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today (ticket.py approve first); nothing placed" % a.index)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None or rv.get("verdict") != "PASS":
        raise TicketError("order %d is not a PASS from today's risk check: %s" % (a.index, why or "blocked"))
    sym = rec["symbol"]
    paper = bool(cfg.get("paper", True))
    if not paper:
        if not a.live:
            raise TicketError("the config says \"paper\": false. This would place a REAL order. Nothing placed: "
                              "run it again with --live if that is what the user asked for, word for word")
        if str(cfg.get("broker", "indstocks")).lower() != "indstocks":
            raise TicketError("only the indstocks backend places live orders so far (broker %r)" % cfg.get("broker"))
        broker = INDstocksBroker()
        try:
            res = broker.place(order)
        except trader_data.DataError as e:
            _record(a.state, rec, now, today, "failed", note=str(e))
            raise TicketError("INDstocks refused the order (recorded as failed): %s" % e)
        new = _record(a.state, rec, now, today, "placed", broker_order_id=res.get("order_id"),
                      child_order_id=res.get("child_order_id"), broker_status=res.get("order_status"))
        print("Placed LIVE at INDstocks: order %s (%s)%s." % (res.get("order_id"), res.get("order_status"),
                                                              (", GTT child %s" % res["child_order_id"]) if res.get("child_order_id") else ""))
        print("Record the fill when you see it: ticket.py sync, or ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (sym, side_of(order).lower()))
        return 0
    ltp, src = price_for(sym, a, now, cfg)
    ledger = Paper(a.paper, cfg)
    parent, child, events = ledger.place(order, ltp, now)
    new = _record(a.state, rec, now, today, "placed", broker_order_id=parent["id"], child_order_id=child["id"] if child else None,
                  price_source=src)
    print("Placed on the paper ledger: %s%s, last price %s from %s." % (
        parent["id"], (" with GTT child %s" % child["id"]) if child else "", _p(ltp), src))
    for ev in events:
        if ev.get("price") is not None:
            apply_fill(a.portfolio, cfg, ev["symbol"], ev["txn_type"], ev["qty"], ev["price"], today, ev["charges"])
            log_fill(a.state, now, today, ev["symbol"], ev["txn_type"], ev["qty"], ev["price"], ev["charges"],
                     kind=rec.get("kind"), note=ev["what"], broker_id=ev["id"])
            print("  %s: %s %d %s at %s, charges %s. Ledger cash %s." % (
                ev["id"], ev["txn_type"], ev["qty"], ev["symbol"], _p(ev["price"]), _money(ev["charges"]), _money(ev["cash"])))
        else:
            print("  %s: %s" % (ev["id"], ev["what"]))
    if child:
        print("  The stop-loss and target legs wait in the ledger: ticket.py paper tick moves them against a price.")
    print("Added the fill to %s. Nothing real was sent." % a.portfolio)
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    new = _record(a.state, rec, now, today, a.status, broker_order_id=a.broker_id, note=a.note)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and a.filled_qty and a.filled_price:
        chg = charges_leg(int(float(a.filled_qty)), float(a.filled_price), rec.get("txn_type", "BUY"), rates_of(cfg))["total"]
        apply_fill(a.portfolio, cfg, rec["symbol"], rec.get("txn_type", "BUY"), a.filled_qty, a.filled_price, today, chg)
        log_fill(a.state, now, today, rec["symbol"], rec.get("txn_type", "BUY"), a.filled_qty, a.filled_price, chg,
                 kind=rec.get("kind"), broker_id=a.broker_id)
        print("Added the fill to %s (charges %s)." % (a.portfolio, _money(chg)))
    elif a.status == "placed":
        print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (rec["symbol"], str(rec.get("txn_type", "buy")).lower()))
        if rec.get("kind") == "entry" and is_smart(order):
            print("When the stop-loss or the target leg fills later, record that sell the same way.")
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now = parse_now(a.now)
    date = a.date or now.date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-11-03")
    chg = charges_leg(int(float(a.qty)), float(a.price), a.side, rates_of(cfg))["total"] if _f(a.qty) and float(a.qty) == int(float(a.qty)) else 0.0
    pf = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date, chg)
    log_fill(a.state, now, date, a.symbol, a.side, a.qty, a.price, chg, broker_id=a.broker_id)
    print("Recorded %s %s %s at %s on %s in %s (charges %s). Cash now %s." % (
        a.side, a.qty, a.symbol.upper(), _money(a.price), date, a.portfolio, _money(chg), _money(pf["cash"])))
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    sym = a.symbol.strip().upper()
    now, today = _clock(a)
    source = "the broker (ticket.py sync, or the INDstocks app)"
    security_id = None
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0:
            raise TicketError("--qty wants the number of shares you hold, like --qty 71")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if str(p.get("symbol") or p.get("ticker") or "").upper() == sym)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check the broker (ticket.py sync, or the "
                              "paper ledger) and pass --qty if you do hold some" % (sym, a.portfolio))
    if os.path.exists(a.paper):
        ledger = Paper(a.paper, cfg)
        pos = ledger.position(sym)
        if pos:
            security_id = pos.get("security_id")
    plan = read_json(a.plan, "plan", required=False) or {}
    flat_by = (plan.get("flat_by") if str(plan.get("symbol", "")).upper() == sym else None) \
        or cfg.get("flat_by") or "15:15"
    order = {"symbol": sym, "security_id": security_id, "exchange": "NSE", "segment": "EQUITY",
             "product": str(cfg.get("product") or "INTRADAY").upper(), "txn_type": "SELL", "order_type": "MARKET",
             "validity": "DAY", "qty": int(round(qty)),
             "why": "flat by %s IST: closes the %s day trade before the broker's 15:20 square-off (qty from %s)"
             % (flat_by, sym, source)}
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "trader-broker", "created_at": ist_iso(now), "status": "proposed",
           "paper": bool(cfg.get("paper", True)), "broker": cfg.get("broker", "indstocks"), "currency": "INR",
           "note": "Flatten ticket. Nothing is placed until trader-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    print("Flatten %s: %s (flat by %s IST)." % (sym, plain(order, rates_of(cfg)), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against the broker before you say yes." % source)
    print("Next:")
    print("  1. trader-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order, then ticket.py place.")
    if cfg.get("paper", True):
        print("  3. The paper ledger cancels the open stop-loss and target legs for %s when the sell fills." % sym)
    else:
        print("  3. The GTT child (stop-loss and target) still holds these shares at INDstocks. Cancel it first:")
        print("     ticket.py sync shows its GTT- id; POST /smart/order/cancel with that id. Then place the ticket.")
    return 0


def cmd_paper(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now, today = _clock(a)
    if a.action == "show":
        if not os.path.exists(a.paper):
            print("No paper ledger at %s yet: the first ticket.py place starts it from the config budget." % a.paper)
            return 1
        print(Paper(a.paper, cfg).summary())
        return 0
    ledger = Paper(a.paper, cfg)
    need = set(o["symbol"] for o in ledger.doc["orders"] if o["status"] == "PENDING") | set(ledger.doc["positions"])
    prices = {}
    for item in a.price or []:
        if "=" in item:
            s, p = item.split("=", 1)
        elif len(need) == 1:
            s, p = next(iter(need)), item          # one symbol open: a bare price is unambiguous
        elif not need:
            raise TicketError("nothing is open on the paper ledger, so there is nothing to tick")
        else:
            raise TicketError("--price wants SYMBOL=PRICE while more than one symbol is open (%s), got %r"
                              % (", ".join(sorted(need)), item))
        if _f(p) is None or float(p) <= 0:
            raise TicketError("--price %s wants a price above 0" % s.strip().upper())
        prices[s.strip().upper()] = float(p)
    missing = sorted(s for s in need if s not in prices)
    if missing and not a.no_quote:
        pdoc = read_json(a.prices, "prices", required=False) or {}
        for s in list(missing):
            p = _f((pdoc.get("prices") or {}).get(s))
            det = ((pdoc.get("detail") or {}).get(s) or {})
            t = parse_ts(det.get("time")) or parse_ts(det.get("fetched")) or parse_ts(pdoc.get("as_of"))
            if p and t and -60 <= (now - t).total_seconds() <= FRESH_MINUTES * 60:
                prices[s] = p
                missing.remove(s)
        if missing:
            try:
                q = trader_data.backend().quote(missing)
                for s in missing:
                    if (q.get(s) or {}).get("ltp"):
                        prices[s] = float(q[s]["ltp"])
            except trader_data.DataError as e:
                print("no live quote (%s): pass --price SYMBOL=P" % e)
    events = ledger.tick(prices, now)
    if not events:
        print("Paper tick at %s IST: nothing moved%s." % (now.strftime("%H:%M"), (" (prices: %s)" % ", ".join(
            "%s %s" % (s, _p(p)) for s, p in sorted(prices.items()))) if prices else ""))
    for ev in events:
        if ev.get("price") is not None:
            apply_fill(a.portfolio, cfg, ev["symbol"], ev["txn_type"], ev["qty"], ev["price"], today, ev["charges"])
            log_fill(a.state, now, today, ev["symbol"], ev["txn_type"], ev["qty"], ev["price"], ev["charges"],
                     kind="exit" if ev["txn_type"] == "SELL" else "entry", note=ev["what"], broker_id=ev["id"])
            print("%s: %s %d %s at %s (%s), charges %s. Ledger cash %s." % (
                ev["id"], ev["txn_type"], ev["qty"], ev["symbol"], _p(ev["price"]), ev["what"], _money(ev["charges"]),
                _money(ev["cash"])))
        else:
            print("%s: %s" % (ev.get("id") or ev["symbol"], ev["what"]))
    print(ledger.summary())
    return 0


def cmd_sync(a):
    cfg = read_json(a.config, "config", required=False) or {}
    if cfg.get("paper", True):
        print("The config is paper: the ledger is trader/paper.json (ticket.py paper show). Nothing to sync.")
        return 1
    broker = INDstocksBroker()
    try:
        book = broker.order_book()
        pos = broker.positions()
    except trader_data.DataError as e:
        raise TicketError(str(e))
    print("INDstocks order book today: %d orders." % len(book))
    for o in book:
        print("  %s %s %s %s %s qty %s/%s price %s status %s%s" % (
            o.get("id"), o.get("txn_type"), o.get("name") or o.get("security_id"), o.get("product"), o.get("order_type"),
            o.get("traded_qty"), o.get("requested_qty"), o.get("traded_price") or o.get("requested_price") or "-",
            o.get("status"), (" (" + o["extra_info"] + ")") if o.get("extra_info") else ""))
    print("Open equity positions: %d." % len(pos))
    for p in pos:
        print("  %s %s net %s at %s" % (p.get("symbol"), p.get("product"), p.get("net_qty"), p.get("avg_price")))
    print("Record fills with ticket.py fill; this command changes nothing.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % _money(r["max_loss_net"])) if r.get("max_loss_net") is not None and r["status"] == "placed" else ""
        chg = (" charges %s" % _money(r["charges"])) if r.get("charges") and r["status"] == "filled" else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-9s %s%s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, chg, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets, your yes, and the paper ledger (trader-broker, NSE).")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-03 10:01\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("place", help="send an approved order: the paper ledger, or INDstocks with --live")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--price", help="paper: fill at this price instead of a quote")
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--no-quote", action="store_true", help="paper: never ask INDstocks for a quote")
    p.add_argument("--live", action="store_true", help="REAL MONEY: only with \"paper\": false in the config")
    p.add_argument("--paper", default="trader/paper.json", help="the paper ledger")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("result", help="record what happened when you placed an order yourself")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--broker-id")
    p.add_argument("--date", help="YYYY-MM-DD (default: today in IST)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="a sell ticket that closes a symbol's position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold (default: trader/portfolio.json)")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--paper", default="trader/paper.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    clock(p)
    p = sub.add_parser("paper", help="the paper ledger: tick (move legs against a price) or show")
    p.add_argument("action", choices=["tick", "show"])
    p.add_argument("--price", action="append", metavar="[SYMBOL=]PRICE",
                   help="a price to tick against (repeatable; the symbol may be left out when only one is open)")
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--no-quote", action="store_true", help="never ask INDstocks for a quote")
    p.add_argument("--paper", default="trader/paper.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p = sub.add_parser("sync", help="live only: INDstocks' order book and positions (read only)")
    p.add_argument("--config", default="trader/config.json")
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "place": cmd_place,
                "result": cmd_result, "fill": cmd_fill, "flatten": cmd_flatten, "paper": cmd_paper,
                "sync": cmd_sync, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Pre-trade risk check for trader-risk (NSE). Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-11-03 10:00"        # pin the clock (IST)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it.

What blocks an order (every order):

  format        missing symbol or txn_type, no whole-share qty (INDstocks takes
                no notional orders), unknown order_type / product / validity /
                segment / exchange, a LIMIT order without a limit_price, a
                stop-loss or target leg without its limit price or on the wrong
                side of its trigger, a price off the stock's tick
  asset         F&O contracts (option and futures symbols, segment DERIVATIVE)
                unless "allow_fno": true; anything that is not an NSE/BSE equity
  short         selling more than you hold, unless "allow_short": true

What blocks an entry (on top):

  blocked       a symbol on your "blocked" list
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need margin (fully
                funded: MIS leverage is not used while "allow_margin" is false)
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (a smart order's
                sl_trigger_price or the order's plan stop); a stop on the wrong
                side of the entry
  loss          qty x (entry - stop) PLUS the round-trip charges over
                max_loss_per_trade_inr. The entry is the limit price for a limit
                order, else the HIGHER of the plan's entry and a fresh price
                (under 15 minutes old), so a stale plan cannot understate the risk
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  daily loss    the max losses (charges included) at the stops of today's
                entries plus this one over max_daily_loss_inr
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (IST), or
                the market is closed (weekend, NSE holiday, outside 09:15 to
                15:30, the Muhurat session)

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so trader-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # realpath: works through a junction
import trader_data  # noqa: E402

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 75000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 1000, "max_daily_loss_inr": 3000, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "15:00", "flat_by": "15:15",
    "product": "INTRADAY", "paper": True, "blocked": [], "allow_fno": False, "allow_margin": False,
    "allow_short": False, "charges": dict(trader_data.CHARGE_RATES),
    # the paper-month rules (off unless the config sets them): the 10:00 breadth gate and one loss ends the day
    "breadth_gate_pct": None, "gate_time": "10:00", "stop_after_loss": False,
}
# the fields INDstocks reads (POST /order and POST /smart/order); the fingerprint covers these only
INDSTOCKS_FIELDS = ("security_id", "exchange", "segment", "product", "txn_type", "order_type", "validity",
                    "qty", "limit_price", "trigger_price", "trigger_limit_price", "sl_trigger_price",
                    "sl_limit_price", "tgt_trigger_price", "tgt_limit_price", "is_amo", "symbol")
PRICE_FIELDS = ("limit_price", "trigger_price", "trigger_limit_price", "sl_trigger_price", "sl_limit_price",
                "tgt_trigger_price", "tgt_limit_price")
TYPES = {"MARKET", "LIMIT", "TRIGGER"}
PRODUCTS = {"INTRADAY", "CNC", "MARGIN"}
VALIDITIES = {"DAY"}
SEGMENTS = {"EQUITY", "DERIVATIVE", "COMMODITY"}
EXCHANGES = {"NSE", "BSE", "MCX"}
FNO = re.compile(r"^[A-Z&\-]+\d{2}[A-Z]{3}(\d+(\.\d+)?(CE|PE)|FUT)$")   # NIFTY26NOV24000CE, RELIANCE26NOVFUT
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PAISA = 0.005
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "trigger": "the trigger price", "stale price": "an old price"}

OPEN = trader_data.OPEN
CLOSE = trader_data.CLOSE
CALENDAR_YEARS = trader_data.CALENDAR_YEARS
trading_day = trader_data.trading_day
charges = trader_data.charges


class RiskError(Exception):
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
        raise RiskError("--now wants a time like \"2026-11-03 10:00\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return dt
    return (dt - dt.utcoffset()).replace(tzinfo=None) + trader_data.IST


def parse_ts(text):
    """An ISO time with an offset (or Z) -> naive IST; a bare time is IST; None when unreadable."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None) + trader_data.IST
    return dt


def hhmm(value, key):
    """"09:30" -> time(9, 30); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an IST time like \"09:30\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def window_check(now, c):
    """Reasons an entry cannot be placed now (IST). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    if before is None and after is None:
        return [], []
    warns = []
    d, t = now.date(), now.time().replace(second=0, microsecond=0)
    if d.year not in CALENDAR_YEARS:
        warns.append("the holiday list covers %s only: check nseindia.com for %s"
                     % (" and ".join(map(str, CALENDAR_YEARS)), d.isoformat()))
    ok, why = trading_day(d)
    stamp = now.strftime("%a %Y-%m-%d %H:%M")
    if d.isoformat() in trader_data.MUHURAT:
        return ["window: %s is the Muhurat session (%s); this pack does not trade it"
                % (d.isoformat(), trader_data.MUHURAT[d.isoformat()])], warns
    if not ok:
        return ["window: the market is closed today (%s, %s IST)" % (why, stamp)], warns
    if t < OPEN or now.time() >= CLOSE:
        return ["window: the market is closed (%s IST; the session is %s to %s)"
                % (stamp, OPEN.strftime("%H:%M"), CLOSE.strftime("%H:%M"))], warns
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                     t.strftime("%H:%M"))], warns
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)" % (after.strftime("%H:%M"),
                                                                    t.strftime("%H:%M"))], warns
    return [], warns


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in trader-broker/ticket.py: the exact order that was checked."""
    core = dict((k, order[k]) for k in INDSTOCKS_FIELDS if k in order and order[k] is not None)
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    return "Rs " + "{:,.2f}".format(float(x)).replace(".00", "")


def _paisa(x):
    """A price to the paisa, half up (1050.235 -> 1050.24)."""
    return int(float(x) * 100 + 0.5 + 1e-9) / 100.0


def _p(x):
    return "%.2f" % float(x)


def on_tick(x, tick):
    u = float(x) / float(tick)
    return abs(u - round(u)) < 1e-6


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("symbol") or p.get("ticker") or "").upper()
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_side(rec):
    return str(rec.get("txn_type") or rec.get("side") or "").lower()


def rec_is_entry(rec):
    """trader-broker logs the kind trader-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return rec_side(rec) == "buy"


def placed_today(state, today):
    """Orders recorded as placed today by trader-broker."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and rec_day(rec) == today:
            out.append(rec)
    return out


# ---------------------------------------------------------------- the check

def losses_today(ledger, today):
    """Trades closed today at or under zero before charges, from the paper ledger's sell records."""
    out = []
    for rec in (ledger or {}).get("orders", []):
        if rec.get("txn_type") != "SELL" or rec.get("status") != "SUCCESS" or "pnl" not in rec:
            continue
        when = str(rec.get("updated_at") or "")
        if when[:10] != today:
            continue
        if float(rec["pnl"]) <= 0:
            out.append({"symbol": rec.get("symbol"), "pnl": float(rec["pnl"]), "time": when[11:16]})
    return out


def gate_reasons(c, breadth, today):
    """Why the breadth gate blocks an entry (empty when it does not apply or is open)."""
    gate = _f(c.get("breadth_gate_pct"))
    if not gate:
        return []
    gate_time = str(c.get("gate_time") or DEFAULTS["gate_time"])
    if not breadth or str(breadth.get("date")) != today:
        return ["gate: no breadth reading for %s; run scan.py breadth at %s first (breadth_gate_pct %s)"
                % (today, gate_time, _num(gate))]
    if not breadth.get("open"):
        return ["gate: %s" % (breadth.get("reason") or "closed")]
    return []


def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          breadth=None, ledger=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive IST (default: the real clock); its date is "today"
    price_times  {SYM: naive IST time of that price}; None = every price counts as fresh
    breadth      trader/state/breadth.json (scan.py breadth), for the gate
    ledger       trader/paper.json, for stop_after_loss"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    rates = dict(DEFAULTS["charges"])
    rates.update((cfg or {}).get("charges") or {})
    prices = dict((str(k).upper(), float(v)) for k, v in (prices or {}).items() if _f(v))
    now = now or now_ist()
    today = today or now.date().isoformat()
    acct, top_warn = account(c, portfolio, prices)
    blocked = set(str(b).upper() for b in c.get("blocked") or [])
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss_net"))
        if ml is None:
            ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (rec_side(r), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = set((str(r.get("symbol", "")).upper(), rec_side(r)) for r in done_today)
    win_reasons, win_warns = window_check(now, c)
    gate_block = gate_reasons(c, breadth, today)
    lost = losses_today(ledger, today) if c.get("stop_after_loss") else []
    if c.get("stop_after_loss") and not c.get("paper", True) and ledger is None:
        top_warn.append("stop_after_loss reads the paper ledger; live fills are not read yet, so it cannot stop a "
                        "live day on its own")
    gate_info = None
    if _f(c.get("breadth_gate_pct")) and breadth and str(breadth.get("date")) == today:
        gate_info = "gate %s: %s%% of charts above their open at %s (gate %s%%)" % (
            "open" if breadth.get("open") else "closed", breadth.get("pct"), breadth.get("time"),
            _num(_f(c.get("breadth_gate_pct"))))
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    top_warn.extend(win_warns)

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        sym = str(o.get("symbol", "")).strip().upper()
        side = str(o.get("txn_type") or o.get("side") or "").strip().lower()
        typ = str(o.get("order_type") or o.get("type") or "MARKET").strip().upper()
        validity = str(o.get("validity") or "DAY").strip().upper()
        product = str(o.get("product") or c.get("product") or "INTRADAY").strip().upper()
        segment = str(o.get("segment") or "EQUITY").strip().upper()
        exchange = str(o.get("exchange") or "NSE").strip().upper()
        qty = o.get("qty")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        tick = _f(plan.get("tick")) or _f(o.get("tick"))
        legs = o.get("sl_trigger_price") is not None or o.get("tgt_trigger_price") is not None
        value = None

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: txn_type must be BUY or SELL, got %r" % (o.get("txn_type") or o.get("side")))
        if o.get("notional") is not None:
            reasons.append("format: INDstocks takes no notional (rupee) orders; give a whole-share qty")
        q = _f(qty)
        if q is None or q <= 0:
            reasons.append("format: qty must be a positive whole number, got %r" % (qty,))
        elif abs(q - round(q)) > 1e-9:
            reasons.append("format: qty must be whole shares, got %r" % (qty,))
        elif not isinstance(qty, int) or isinstance(qty, bool):
            warns.append("send qty as an integer (%d), the way POST /order types it" % int(round(q)))
        for k in PRICE_FIELDS:
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                elif isinstance(o.get(k), str):
                    warns.append("send %s as a number (%s), not a string" % (k, o.get(k)))
                elif tick and not on_tick(v, tick):
                    reasons.append("format: %s %s is not on %s's %s tick" % (k, _p(v), sym, ("%g" % tick)))
        if typ not in TYPES:
            reasons.append("format: unknown order_type %r (MARKET, LIMIT or TRIGGER)" % typ)
        if validity not in VALIDITIES:
            reasons.append("format: validity must be DAY, got %r" % validity)
        if product not in PRODUCTS:
            reasons.append("format: unknown product %r (INTRADAY or CNC)" % product)
        if segment not in SEGMENTS:
            reasons.append("format: unknown segment %r" % segment)
        if exchange not in EXCHANGES:
            reasons.append("format: unknown exchange %r" % exchange)
        if typ == "LIMIT" and o.get("limit_price") is None:
            reasons.append("format: a LIMIT order needs a limit_price")
        if typ == "TRIGGER" and o.get("trigger_price") is None:
            reasons.append("format: a TRIGGER order needs a trigger_price")
        if typ == "TRIGGER" and o.get("limit_price") is not None:
            reasons.append("format: a TRIGGER order takes trigger_limit_price, not limit_price")
        if typ == "MARKET" and o.get("limit_price") is not None:
            warns.append("limit_price on a MARKET order is ignored by INDstocks")
        if o.get("sl_trigger_price") is not None and o.get("sl_limit_price") is None:
            reasons.append("format: a stop-loss leg needs sl_limit_price as well as sl_trigger_price")
        if o.get("tgt_trigger_price") is not None and o.get("tgt_limit_price") is None:
            reasons.append("format: a target leg needs tgt_limit_price as well as tgt_trigger_price")
        if o.get("sl_trigger_price") is not None and o.get("sl_limit_price") is not None:
            slt, sll = _f(o["sl_trigger_price"]), _f(o["sl_limit_price"])
            if slt and sll and ((side == "buy" and sll > slt + PAISA) or (side == "sell" and sll < slt - PAISA)):
                reasons.append("format: the stop-loss limit %s must sit %s its trigger %s on a %s"
                               % (_p(sll), "at or below" if side == "buy" else "at or above", _p(slt), side.upper()))
        if o.get("tgt_trigger_price") is not None and o.get("tgt_limit_price") is not None:
            tgt, tgl = _f(o["tgt_trigger_price"]), _f(o["tgt_limit_price"])
            if tgt and tgl and ((side == "buy" and tgl < tgt - PAISA) or (side == "sell" and tgl > tgt + PAISA)):
                reasons.append("format: the target limit %s must sit %s its trigger %s on a %s"
                               % (_p(tgl), "at or above" if side == "buy" else "at or below", _p(tgt), side.upper()))
        if o.get("is_amo"):
            warns.append("an after-market order (is_amo): it goes to the exchange at the next open")

        # asset
        if segment == "COMMODITY" or exchange == "MCX":
            reasons.append("asset: %s is a commodity order; this pack trades NSE and BSE equity shares only" % sym)
        elif sym and (FNO.match(sym) or segment == "DERIVATIVE"):
            if not c.get("allow_fno"):
                reasons.append("asset: %s is an F&O contract; futures and options are off (\"allow_fno\": false)" % sym)
            else:
                warns.append("F&O order: an option can lose 100%% of what you pay, a future more than that")
        elif sym and not SYMBOL.match(sym):
            reasons.append("asset: %s does not look like an NSE symbol; this pack trades equity shares only" % sym)
        if product == "MARGIN" and segment == "EQUITY":
            reasons.append("format: product MARGIN is for derivatives; equity takes INTRADAY or CNC")

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        if typ == "LIMIT":
            ref, ref_from = _f(o.get("limit_price")), "limit"
        elif typ == "TRIGGER":
            ref, ref_from = _f(o.get("trigger_limit_price")) or _f(o.get("trigger_price")), "trigger"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                ref, ref_from = max(cands, key=lambda x: x[0])
                ref = _paisa(ref)
                if live and ref_from == "price" and plan_entry and live > plan_entry + PAISA:
                    info.append("%s last %s is above the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), _p(plan_entry), _p(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and typ == "MARKET":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        if q and q > 0 and ref:
            value = round(q * ref, 2)

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9 and q is not None:
            exiting = q <= held + 1e-9
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %s-share order; add it to "
                         "trader/prices.json (trader-alerts: alerts.py quote %s --write)" % (sym, qty, sym))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None:
            if value < float(c.get("min_order_inr") or 0) - PAISA:
                later.append("size: %s is under your %s minimum order"
                             % (money(value), money(c["min_order_inr"])))
            if c.get("max_order_inr") and value > float(c["max_order_inr"]) + PAISA:
                later.append("size: %s is over your %s max order"
                             % (money(value), money(c["max_order_inr"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        if value is not None and side == "buy" and kind == "entry":
            after_cash = cash - value
            if after_cash < -PAISA and not c.get("allow_margin"):
                later.append("cash: needs %s but only %s cash; that would be margin (MIS leverage is off while "
                             "\"allow_margin\" is false)" % (money(value), money(cash)))
            elif after_cash < buffer_inr - PAISA:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (money(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), money(buffer_inr)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + PAISA:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            over = q > held + 1e-9 if q is not None else held <= 0
            if over and not c.get("allow_short"):
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held)))

        # day-trade checks: stop, loss, reward:risk, daily loss, trades, window
        max_loss = max_loss_net = chg = risk_ps = rr = stop = target = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            leg_stop, plan_stop = _f(o.get("sl_trigger_price")), _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = _f(o.get("tgt_trigger_price")) or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > PAISA:
                warns.append("the stop-loss leg %s differs from the plan's stop %s: the leg is the one "
                             "the broker uses" % (_p(leg_stop), _p(plan_stop)))
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not at the broker: set a stop alert "
                             "(trader-alerts: alerts.py add --from-plan)" % _p(stop))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): a smart order's "
                                 "sl_trigger_price, from trader-exit and trader-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - PAISA) or (not long and stop <= ref + PAISA):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side.upper()))
                else:
                    risk_c = int(round(abs(ref - stop) * 100))
                    risk_ps = risk_c / 100.0
                    shares = q or 0
                    max_loss = round(shares * risk_ps, 2)
                    chg = charges(shares, ref, stop, rates)["total"] if long else charges(shares, stop, ref, rates)["total"]
                    max_loss_net = round(max_loss + chg, 2)
                    cap = _f(c.get("max_loss_per_trade_inr"))
                    if cap and max_loss_net > cap + PAISA:
                        later.append("loss: %s shares x %s risk a share = %s at the stop, plus %s charges = %s, "
                                     "over your %s max loss per trade"
                                     % (_num(shares), _p(risk_ps), money(max_loss), money(chg), money(max_loss_net),
                                        money(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + PAISA) or (not long and target >= ref - PAISA):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = round(int(round(abs(target - ref) * 100)) / float(risk_c), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            daily = _f(c.get("max_daily_loss_inr"))
            if daily and max_loss_net is not None:
                used = loss_today + batch_loss
                if used + max_loss_net > daily + PAISA:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(max_loss_net), money(used + max_loss_net),
                                                       money(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(gate_block)
            for l in lost:
                later.append("stopped for the day: %s closed at %s before charges at %s, and stop_after_loss is on"
                             % (l["symbol"], money(l["pnl"]), l["time"] or "?"))
            later.extend(win_reasons)
            if gate_info:
                info.append(gate_info)
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s + %s charges = %s%s, reward:risk %s%s"
                            % (money(max_loss), money(chg), money(max_loss_net),
                               (" (limit %s)" % money(c["max_loss_per_trade_inr"]))
                               if _f(c.get("max_loss_per_trade_inr")) else "",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss), money(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side.upper(), sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side.upper(), sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss_net or 0.0
            if side == "buy":
                if value is not None:
                    acct["cash"] -= value
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                if q is not None:
                    acct["shares"][sym] = held + q
            elif side == "sell":
                if value is not None:
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                if q is not None:
                    acct["shares"][sym] = held - q
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind, "product": product,
            "amount": "%s shares" % qty,
            "value_inr": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": max_loss,
            "charges": chg, "max_loss_net": max_loss_net,
            "reward_risk": rr, "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": now.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2)},
    }


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s, %s" % (r["verdict"], r["index"], r["side"].upper(), r["symbol"],
                                                    r["amount"], r["product"], r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_ist"], money(res["cash_after"]),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. trader-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive IST or None}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = parse_ts(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        s = str(s).upper()
        out[s] = float(p)
        d = detail.get(s) or {}
        times[s] = parse_ts(d.get("time")) or parse_ts(d.get("fetched")) or as_of
    return out, times


# ---------------------------------------------------------------- the journal and the weekly review

JOURNAL_DIR = "trader/journal"
REPLAYS_DIR = "trader/replays"


def _day_of(doc, *keys):
    for k in keys:
        v = (doc or {}).get(k)
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    return None


def paper_day(start, today):
    """Trading days from the paper month's first day to `today`, both counted (start itself is day 1).
    None before the start, without a start, or on a day NSE does not trade."""
    if not start:
        return None
    d0, d1 = datetime.date.fromisoformat(start), datetime.date.fromisoformat(today)
    if d1 < d0 or not trader_data.trading_day(d1)[0]:
        return None
    n, d = 0, d0
    while d <= d1:
        n += 1 if trader_data.trading_day(d)[0] else 0
        d += datetime.timedelta(days=1)
    return n


def _scan_day(w):
    """The day a watchlist belongs to: when it was made. A daily scan's as_of is the previous close, so the
    morning scan of the 13th says "2026-11-12 close" and would otherwise land in the 12th's journal."""
    return (w.get("created_at") or w.get("as_of") or "")[:10]


def journal_entry(today, files, notes=(), existing=None, paper_start=None):
    """One day's record from the state files that match `today`. Pure: files is {name: doc or None}."""
    j = dict(existing or {})
    j.update({"created_by": "trader-risk", "date": today, "paper_day": paper_day(paper_start, today)})
    b = files.get("breadth")
    j["breadth"] = ({"time": b.get("time"), "pct": b.get("pct"), "open": b.get("open"), "reason": b.get("reason")}
                    if b and _day_of(b, "date") == today else None)
    w = files.get("watchlist")
    j["scan"] = ({"mode": w.get("mode"), "as_of": w.get("as_of"), "hits": len(w.get("hits") or []),
                  "scanned": w.get("scanned")} if w and _scan_day(w) == today else None)
    e = files.get("entry")
    j["entry"] = ({"symbol": e.get("symbol"), "status": e.get("status"), "time": e.get("time"),
                   "price": e.get("price")} if e and _day_of(e, "date") == today else None)
    p = files.get("plan")
    j["plan"] = ({"symbol": p.get("symbol"), "entry": p.get("entry"), "stop": p.get("stop"), "target": p.get("target"),
                  "stop_rule": p.get("stop_rule"), "target_r": p.get("target_r"), "risk_per_share": p.get("risk_per_share")}
                 if p and _day_of(p, "date") == today else None)
    t = files.get("timemachine")
    if t and _day_of(t.get("plan") or {}, "date") == today:
        mc, h = t.get("mc") or {}, t.get("history") or {}
        j["timemachine"] = {"p_target": mc.get("p_target"), "p_stop": mc.get("p_stop"), "p_neither": mc.get("p_neither"),
                            "verdict": t.get("verdict"), "history": "%s fired: %s target, %s stop, %s neither"
                            % (h.get("triggered"), h.get("target"), h.get("stop"), h.get("close"))
                            if h.get("triggered") is not None else None}
    else:
        j["timemachine"] = None
    r = files.get("risk")
    j["risk"] = ({"pass": r.get("pass"), "block": r.get("block"),
                  "blocked_on": sorted(set(x for res in r.get("results") or [] for x in
                                           (reason.split(":")[0] for reason in res.get("reasons") or [])))}
                 if r and (r.get("checked_at") or "")[:10] == today else None)
    led = files.get("ledger") or {}
    fills = []
    for rec in led.get("orders") or []:
        if rec.get("status") != "SUCCESS" or (rec.get("updated_at") or "")[:10] != today:
            continue
        fills.append({"id": rec.get("id"), "symbol": rec.get("symbol"), "side": rec.get("txn_type"),
                      "qty": rec.get("traded_qty"), "price": rec.get("traded_price"), "charges": rec.get("charges"),
                      "pnl": rec.get("pnl"), "time": (rec.get("updated_at") or "")[11:16], "what": rec.get("what")})
    j["fills"] = fills
    sells = [f for f in fills if f["side"] == "SELL" and f.get("pnl") is not None]
    buys_chg = sum(f["charges"] or 0 for f in fills if f["side"] == "BUY")
    j["trades"] = {"closed": len(sells), "won": sum(1 for f in sells if f["pnl"] > 0),
                   "lost": sum(1 for f in sells if f["pnl"] <= 0),
                   "gross": round(sum(f["pnl"] for f in sells), 2),
                   "charges": round(buys_chg + sum(f["charges"] or 0 for f in sells), 2)}
    j["trades"]["net"] = round(j["trades"]["gross"] - j["trades"]["charges"], 2)
    rp = files.get("replay")
    j["replay"] = ({"rule": rp.get("summary"), "baseline": rp.get("baseline"), "others": rp.get("others")} if rp else None)
    old = list((existing or {}).get("notes") or [])
    for n in notes:
        old.append({"at": now_ist().strftime("%H:%M"), "note": n})
    j["notes"] = old
    return j


def render_journal(j):
    out = ["Journal %s%s" % (j["date"], ", paper day %d" % j["paper_day"] if j.get("paper_day") else "")]
    b = j.get("breadth")
    out.append("  gate      %s" % ("%s at %s: %s%%, %s" % ("open" if b["open"] else "closed", b["time"], b["pct"], b["reason"])
                                  if b else "no breadth reading"))
    s = j.get("scan")
    out.append("  scan      %s" % ("%s: %s hits of %s scanned (%s)" % (s["mode"], s["hits"], s["scanned"], s["as_of"]) if s
                                  else "none today"))
    e, p = j.get("entry"), j.get("plan")
    out.append("  entry     %s" % (" ".join(str(x) for x in (e["symbol"], e["status"], e.get("time"),
                                                          ("at %s" % e["price"]) if e.get("price") else None) if x)
                                  if e else "none"))
    out.append("  plan      %s" % ("%s entry %s stop %s (%s) target %s (%sR), risk %s" % (
        p["symbol"], p["entry"], p["stop"], p["stop_rule"], p["target"], p["target_r"], p["risk_per_share"]) if p else "none"))
    t = j.get("timemachine")
    out.append("  machine   %s" % ("%s; history %s" % (t["verdict"], t["history"]) if t else "not run"))
    r = j.get("risk")
    out.append("  risk      %s" % ("%s passed, %s blocked%s" % (r["pass"], r["block"], (" on " + ", ".join(r["blocked_on"])) if r["blocked_on"] else "")
                                  if r else "not run"))
    for f in j.get("fills") or []:
        out.append("  fill      %s %s %s %s at %s%s" % (f["time"], f["side"], f["qty"], f["symbol"], f["price"],
                                                       (", %s before charges" % money(f["pnl"])) if f.get("pnl") is not None else ""))
    tr = j["trades"]
    out.append("  trades    %d closed, %d won, %d lost: gross %s, charges %s, net %s"
               % (tr["closed"], tr["won"], tr["lost"], money(tr["gross"]), money(tr["charges"]), money(tr["net"])))
    rp = j.get("replay")
    if rp and rp.get("rule"):
        s = rp["rule"]
        out.append("  replay    the rule on every chart: %s breakouts, %s target, %s stop, %s flat, avg R %s, net %s%s"
                   % (s.get("breakouts"), s.get("target"), s.get("stop"), s.get("flat"), s.get("avg_R"),
                      money(s.get("net") or 0), "" if s.get("gate_open") else " (gate closed: none taken)"))
        if rp.get("baseline"):
            bl = rp["baseline"]
            out.append("            baseline 1R no gate: %s breakouts, avg R %s, net %s"
                       % (bl.get("breakouts"), bl.get("avg_R"), money(bl.get("net") or 0)))
        for o in rp.get("others") or []:
            out.append("            %s: %s" % (o.get("strategy"), "gate closed" if not o.get("gate_open") else
                                              "%s signals, avg R %s, net %s" % (o.get("breakouts"), o.get("avg_R"),
                                                                                 money(o.get("net") or 0))))
    else:
        out.append("  replay    none (timemachine.py universe --write after the close)")
    for n in j.get("notes") or []:
        out.append("  note %s  %s" % (n["at"], n["note"]))
    return "\n".join(out) + "\n"


def review(journals):
    """Roll-up over journal records: totals, by symbol, the gate, the replay."""
    days = sorted(journals, key=lambda j: j["date"])
    tot = {"days": len(days), "days_traded": 0, "closed": 0, "won": 0, "lost": 0, "gross": 0.0, "charges": 0.0,
           "net": 0.0, "gate_open_days": 0, "gate_read_days": 0, "replay_rule_net": 0.0, "replay_base_net": 0.0,
           "replay_days": 0}
    by_sym = {}
    for j in days:
        tr = j.get("trades") or {}
        if tr.get("closed"):
            tot["days_traded"] += 1
        for k in ("closed", "won", "lost"):
            tot[k] += tr.get(k, 0) or 0
        for k in ("gross", "charges", "net"):
            tot[k] = round(tot[k] + (tr.get(k, 0) or 0), 2)
        if j.get("breadth"):
            tot["gate_read_days"] += 1
            tot["gate_open_days"] += 1 if j["breadth"].get("open") else 0
        rp = j.get("replay") or {}
        if rp.get("rule"):
            tot["replay_days"] += 1
            tot["replay_rule_net"] = round(tot["replay_rule_net"] + (rp["rule"].get("net") or 0), 2)
            if rp.get("baseline"):
                tot["replay_base_net"] = round(tot["replay_base_net"] + (rp["baseline"].get("net") or 0), 2)
        for f in j.get("fills") or []:
            if f["side"] == "SELL" and f.get("pnl") is not None:
                s = by_sym.setdefault(f["symbol"], {"trades": 0, "won": 0, "gross": 0.0})
                s["trades"] += 1
                s["won"] += 1 if f["pnl"] > 0 else 0
                s["gross"] = round(s["gross"] + f["pnl"], 2)
    return {"from": days[0]["date"] if days else None, "to": days[-1]["date"] if days else None, "totals": tot,
            "by_symbol": dict(sorted(by_sym.items(), key=lambda kv: -kv[1]["gross"]))}


def render_review(r):
    t = r["totals"]
    out = ["Review %s to %s: %d journal day%s, %d with a closed trade." % (
        r["from"], r["to"], t["days"], "" if t["days"] == 1 else "s", t["days_traded"])]
    out.append("  trades    %d closed, %d won, %d lost: gross %s, charges %s, net %s"
               % (t["closed"], t["won"], t["lost"], money(t["gross"]), money(t["charges"]), money(t["net"])))
    out.append("  gate      open on %d of %d days read" % (t["gate_open_days"], t["gate_read_days"]))
    if t["replay_days"]:
        out.append("  replay    %d day%s: the rule on every chart net %s, the baseline net %s"
                   % (t["replay_days"], "" if t["replay_days"] == 1 else "s", money(t["replay_rule_net"]),
                      money(t["replay_base_net"])))
    for sym, s in r["by_symbol"].items():
        out.append("  %-12s %d trade%s, %d won, gross %s" % (sym, s["trades"], "" if s["trades"] == 1 else "s", s["won"],
                                                            money(s["gross"])))
    out.append("Counts, not a verdict. A month of these is the smallest sample worth a decision.")
    return "\n".join(out) + "\n"


def cmd_journal(a, now):
    today = a.today or now.date().isoformat()
    path = "%s/%s.json" % (a.journal_dir.rstrip("/\\"), today)
    existing = read_json(path, "journal", required=False)
    files = {"breadth": read_json(a.breadth, "breadth", required=False),
             "watchlist": read_json(a.watchlist, "watchlist", required=False),
             "entry": read_json(a.entry, "entry", required=False),
             "plan": read_json(a.plan, "plan", required=False),
             "timemachine": read_json(a.timemachine, "timemachine", required=False),
             "risk": read_json(a.out, "risk", required=False),
             "ledger": read_json(a.paper_ledger, "paper ledger", required=False),
             "replay": read_json(os.path.join(a.replays_dir, "%s.json" % today), "replay", required=False)}
    cfg = read_json(a.config, "config", required=False) or {}
    j = journal_entry(today, files, a.note or [], existing, cfg.get("paper_start"))
    if not a.no_write:
        os.makedirs(a.journal_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(j, fh, indent=2)
            fh.write("\n")
    print(json.dumps(j, indent=2) if a.json else render_journal(j) + ("Wrote %s.\n" % path if not a.no_write else ""))
    return 0


def cmd_review(a, now):
    today = a.today or now.date().isoformat()
    start = (datetime.date.fromisoformat(today) - datetime.timedelta(days=a.days - 1)).isoformat()
    journals = []
    if os.path.isdir(a.journal_dir):
        for name in sorted(os.listdir(a.journal_dir)):
            if name.endswith(".json") and start <= name[:10] <= today:
                j = read_json(os.path.join(a.journal_dir, name), "journal", required=False)
                if j:
                    journals.append(j)
    if not journals:
        print("No journal days between %s and %s in %s. Run check.py journal at the end of each day." % (start, today, a.journal_dir))
        return 1
    r = review(journals)
    print(json.dumps(r, indent=2) if a.json else render_review(r))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (trader-risk, NSE).")
    ap.add_argument("cmd", nargs="?", default="check", choices=["check", "journal", "review"],
                    help="check (default) the orders; journal: write today's record; review: roll the journals up")
    ap.add_argument("--note", action="append", help="journal: a note to append (repeatable)")
    ap.add_argument("--days", type=int, default=7, help="review: how many days back")
    ap.add_argument("--journal-dir", default=JOURNAL_DIR)
    ap.add_argument("--replays-dir", default=REPLAYS_DIR, help="where timemachine.py universe --write saves")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--entry", default="trader/entry.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--timemachine", default="trader/timemachine.json")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what trader-broker placed today")
    ap.add_argument("--breadth", default="trader/state/breadth.json", help="scan.py breadth's reading, for the gate")
    ap.add_argument("--paper-ledger", default="trader/paper.json", help="the paper ledger, for stop_after_loss")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-11-03 10:00\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-11-03")
        if a.cmd == "journal":
            return cmd_journal(a, now)
        if a.cmd == "review":
            return cmd_review(a, now)
        doc = read_json(a.orders, "orders file (run trader-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        breadth = read_json(a.breadth, "breadth", required=False)
        ledger = read_json(a.paper_ledger, "paper ledger", required=False)
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, breadth, ledger)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = now_ist().strftime("%Y-%m-%dT%H:%M:%S+05:30")
            rec["orders_file"] = a.orders
            tmp = a.out + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, a.out)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())

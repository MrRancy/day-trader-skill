#!/usr/bin/env python3
"""Exit Planner for trader-exit (NSE): sets the stop and the target BEFORE you
enter, so you know when to get out. Standard library only. Long side only.
It never places an order.

    python3 exit.py plan                                    # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write  # TATACONSUM Nov 3: stop 1039.40, target 1061.00
    python3 exit.py plan --symbol TATACONSUM --entry 1050.20 --or-high 1048.80 --or-low 1030.00
    python3 exit.py plan --stop atr:1.5 --offline TATACONSUM.json   # ATR needs the session's bars
    python3 exit.py plan --symbol NVDA --entry 190.25 --setup level --stop pct:1
    python3 exit.py show                                    # the current trader/plan.json

Stops (a long's stop rounds DOWN to the tick, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or INDstocks). With fewer than 14
             bars by then it averages all of them and says so.
  supertrend the Supertrend(10, 3.0) line under the entry on the same bars:
             the lower band that only rises while the price stays above it.
             With fewer than 10 bars the ATR averages what there is, and it
             says so. Refused when the line sits above the entry (downtrend).
  A level setup has no opening-range default: give a stop.

Targets (round half up to the tick):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

The tick: NSE prices move in ticks that depend on the price band (0.01 under
Rs 250, 0.05 to 1,000, 0.10 to 5,000, 0.50, 1.00, 5.00 above 20,000). The
tick comes from trader/entry.json (trader-entry reads it from the INDstocks
master), from --tick, from --master FILE, or from the price band.

Refused, with the reason: a stop at or above the entry, a target at or below
it, a risk under 0.05% of the price or under one tick (too tight to survive
the spread), an entry.json that has not triggered (unless you give --entry),
a short (v2 plans longs only).

flat_by (default 15:15, or "flat_by" in trader/config.json): out by then, win
or lose. Brokers square off MIS intraday positions from about 15:20, so a
later flat-by hands the exit to the broker. "min_reward_risk" in the config
(default 1.0) only warns here; trader-risk enforces it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # realpath: works through a junction
import trader_data  # noqa: E402

INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"flat_by": "15:15", "min_reward_risk": 1.0}
SQUARE_OFF = datetime.time(15, 20)     # brokers start squaring off MIS positions about here
ATR_BARS = 14
SUPERTREND_PERIOD, SUPERTREND_MULT = 10, 3.0
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05% of the price is inside the spread's noise
PAISA = Decimal("0.01")
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."

OPEN = trader_data.OPEN
CLOSE = trader_data.CLOSE
session = trader_data.session
ist_of = trader_data.ist_of
ts_of = trader_data.ts_of


class PlanError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def now_ist():
    return trader_data.now_ist().replace(microsecond=0)


def parse_now(text):
    """--now -> naive IST. A time without an offset is IST."""
    if not text:
        return now_ist()
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (now_ist().date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise PlanError('--now wants an IST time like "2026-11-03 10:00" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return dt
    return (dt - dt.utcoffset()).replace(tzinfo=None) + trader_data.IST


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise PlanError("%s wants an IST time like 15:15, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_clock(text, what):
    """10:00, 10:00 AM, 3:05 PM -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text))
    if not m:
        raise PlanError("%s wants a time like 10:00, not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise PlanError("%s wants a time like 10:00 AM, not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise PlanError("%s wants a time like 10:00, not %r" % (what, text))
    return datetime.time(h, mi)


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


# ---------------------------------------------------------------- money math (Decimal, to the tick)

def D(x, what="a number"):
    try:
        if isinstance(x, float):
            return Decimal(repr(x))
        return Decimal(str(x).strip().lstrip("Rs").lstrip("$").strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))


def paise(x):
    """Round half up to the paisa (for arithmetic, not for an order)."""
    return D(x).quantize(PAISA, rounding=ROUND_HALF_UP)


def to_tick(x, tick, rounding=ROUND_HALF_UP):
    """Round x onto the tick grid."""
    t = D(tick)
    return ((D(x) / t).quantize(Decimal(1), rounding=rounding) * t).quantize(PAISA)


def floor_tick(x, tick):
    """A long's stop: round down, away from the entry."""
    return to_tick(x, tick, ROUND_FLOOR)


def money(x):
    return "Rs %s" % format(D(x).quantize(PAISA), ",f")


def num(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    return format(D(x).normalize(), "f")


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    if t in ("supertrend", "st"):
        return "supertrend", None
    m = re.match(r"^(price|pct|atr):\s*(?:rs\s*)?([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X, atr:K or supertrend (got %r)" % text)
    kind, val = m.group(1), D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*(?:rs\s*)?([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        return "price", D(m.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None, tick="0.05", supertrend=None):
    """The stop, the target and the risk, on the tick grid. Raises PlanError with the reason."""
    tk = D(tick)
    if tk <= 0:
        raise PlanError("the tick must be above 0")
    e = paise(entry)
    if e <= 0:
        raise PlanError("the entry price must be above Rs 0")
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = paise(or_high), paise(or_low)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (money(oh), money(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = Rs %s" % (money(oh), money(ol), num(raw))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% under the entry" % num(stop_param)
        detail = "%s x (1 - %s%%) = Rs %s" % (money(e), num(stop_param), num(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        a = D(round(atr, 6))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_bars)
        detail = "ATR Rs %s over the %d bars to the entry%s" % (
            num(a.quantize(Decimal("0.0001"))), atr_bars, " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    elif stop_rule == "supertrend":
        if not supertrend:
            raise PlanError("a supertrend stop needs the session's bars")
        if supertrend["direction"] != 1:
            raise PlanError("Supertrend(%d, %s) is in a downtrend at the entry: its line %s sits above the price, "
                            "so it is not a long's stop. Use or-mid, atr:K or price:X."
                            % (supertrend["period"], num(D(str(supertrend["mult"]))), money(paise(supertrend["line"]))))
        raw = D(round(supertrend["line"], 6))
        label = "Supertrend(%d, %s) line" % (supertrend["period"], num(D(str(supertrend["mult"]))))
        detail = "line Rs %s on the %d bars to the entry%s" % (
            num(raw.quantize(PAISA)), supertrend["bars"],
            " (fewer than %d so far)" % supertrend["period"] if supertrend["bars"] < supertrend["period"] + 1 else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    stop = floor_tick(raw, tk)
    if stop != D(raw):
        detail = (detail + ", rounded down to the %s tick" % num(tk)) if detail else "Rs %s rounded down to the %s tick" % (num(raw), num(tk))
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (money(stop), label, money(e)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above Rs 0" % money(stop))
    risk = e - stop
    floor_risk = max(e * MIN_RISK_PCT / 100, tk)
    if risk < floor_risk:
        raise PlanError("the stop is %s under the entry, less than %s (0.05%% of the price, or one tick): too tight to "
                        "survive the spread, normal wiggle would stop you out. Widen it."
                        % (money(risk), money(floor_risk.quantize(PAISA, rounding=ROUND_HALF_UP))))
    if target_rule == "r":
        target = to_tick(e + target_param * risk, tk)
        t_label = r_label(target_param)
    else:
        target = to_tick(target_param, tk)
        t_label = None
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (money(target), money(e)))
    rr = ((target - e) / risk).quantize(PAISA, rounding=ROUND_HALF_UP)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    return {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr, "tick": tk}


# ---------------------------------------------------------------- bars for ATR (same reader as trader-entry)

def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_chart(sym, interval, day, offline=None, backend=None):
    """{bars: [{t,h,l,c}]} for one session: a saved bundle, or INDstocks through trader_data."""
    sym = sym.upper()
    if offline:
        path = offline
        if os.path.isdir(offline):
            path = os.path.join(offline, sym + ".json")
            if not os.path.exists(path):
                path += ".gz"
        if not os.path.exists(path):
            raise PlanError("no offline file %s" % path)
        try:
            doc = json.loads(read_text(path))
        except (OSError, ValueError) as e:
            raise PlanError("could not read %s: %s" % (path, e))
        if not isinstance(doc, dict) or not isinstance(doc.get("bars"), dict):
            raise PlanError("%s is not a trader bundle (no \"bars\")" % path)
        held = dict((k.upper(), v) for k, v in doc["bars"].items())
        if sym not in held:
            raise PlanError("the chart file holds %s, not %s" % (", ".join(sorted(held)) or "nothing", sym))
        raw = held[sym]
    else:
        b = backend or trader_data.backend()
        try:
            raw = b.candles(sym, interval, day, day + datetime.timedelta(days=1))
        except trader_data.DataError as e:
            raise PlanError(str(e))
    bars = []
    for x in raw or []:
        try:
            c = float(x["c"])
            bars.append({"t": int(x["ts"]), "h": float(x.get("h", c)), "l": float(x.get("l", c)), "c": c})
        except (KeyError, TypeError, ValueError):
            continue
    bars.sort(key=lambda x: x["t"])
    return {"bars": bars}


def session_bars(chart, day, interval, upto_slot=None, now=None):
    """Closed, on-grid bars of one NSE session, oldest first."""
    step = INTERVALS[interval]
    s = session(day)
    if not s:
        return []
    open_dt = datetime.datetime.combine(day, s[0])
    close_dt = datetime.datetime.combine(day, s[1])
    open_ts = ts_of(open_dt)
    out = {}
    for b in chart["bars"]:
        if (b["t"] - open_ts) % step:
            continue
        et = ist_of(b["t"])
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        if now and et + datetime.timedelta(seconds=step) > now:
            continue
        slot = int((et - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


def supertrend_of(bars, period=SUPERTREND_PERIOD, mult=SUPERTREND_MULT):
    """The Supertrend line and direction at the last bar: {"line", "direction", "bars", "period", "mult"}."""
    line, direction = trader_data.supertrend([b["h"] for b in bars], [b["l"] for b in bars],
                                             [b["c"] for b in bars], period, mult)
    if not line or line[-1] is None:
        raise PlanError("a supertrend stop needs at least one closed bar by the entry")
    return {"line": round(line[-1], 4), "direction": direction[-1], "bars": len(bars), "period": period, "mult": mult}


# ---------------------------------------------------------------- plan

def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        for k in DEFAULTS:
            if doc.get(k) is not None:
                cfg[k] = doc[k]
    return cfg


def resolve_flat_by(day, explicit, cfg, entry_time):
    """(HH:MM, note). Must be before the 15:30 close; a note past the brokers' square-off."""
    t = parse_hhmm(explicit or cfg.get("flat_by") or DEFAULTS["flat_by"], "--flat-by" if explicit else "flat_by")
    note = ""
    s = session(day) if day else None
    close_t = s[1] if s else CLOSE
    if t >= close_t:
        raise PlanError("%s %s is at or after the %s close%s" % (
            "--flat-by" if explicit else "flat_by in the config", hm(t), hm(close_t), " on %s" % day if day else ""))
    if t > SQUARE_OFF:
        note = "later than %s, when brokers start squaring off MIS positions: the broker may close it for you" % hm(SQUARE_OFF)
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (hm(entry_time), hm(t)))
    return hm(t), note


def gather(a):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("v2 plans longs only: no stops above a short entry. Plan a buy instead.")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": a.symbol.upper(), "entry": a.entry, "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "tick": None, "source": "flags"}
    else:
        path = a.from_path or "trader/entry.json"
        doc = read_json(path, "entry (run /trader-entry first, or give --symbol and --entry)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: v2 plans longs only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                            % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        info = {"symbol": str(doc.get("symbol") or "").upper(), "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "tick": doc.get("tick"), "source": path}
        if not info["symbol"] or info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def resolve_tick(a, info):
    """(tick, where it came from): --tick, entry.json, --master, the downloaded master, the price band."""
    if a.tick is not None:
        if a.tick <= 0:
            raise PlanError("--tick must be above 0")
        return D(a.tick), "--tick"
    if info.get("tick"):
        return D(info["tick"]), info["source"]
    rows = None
    if a.master:
        rows = trader_data.parse_instruments(read_text(a.master))
    elif not a.offline:
        try:
            rows = trader_data.backend().instruments("equity")
        except trader_data.DataError:
            rows = None
    tick, src = trader_data.tick_size(info["symbol"], info["entry"], rows)
    return D(repr(tick)), src


def build_plan(a, now):
    info = gather(a)
    cfg = read_config(a.config)
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X, atr:K "
                            "or supertrend"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    day = None
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = now.date()
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    tick, tick_src = resolve_tick(a, info)
    atr = atr_n = None
    st = None
    if stop_rule in ("atr", "supertrend"):
        chart = load_chart(info["symbol"], info["interval"], day, a.offline)
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], parse_now(a.now) if a.now else None)
        if len(bars) < 3:
            raise PlanError("a%s stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                            % ("n atr" if stop_rule == "atr" else " supertrend", info["interval"], info["symbol"],
                               day, len(bars)))
        if stop_rule == "atr":
            atr, atr_n = atr_of(bars)
        else:
            st = supertrend_of(bars)
    c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                atr, atr_n, tick, st)
    flat_by, flat_note = resolve_flat_by(day, a.flat_by, cfg, entry_time)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(paise(info["or_high"])), "low": float(paise(info["or_low"])),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "trader-exit", "symbol": info["symbol"], "side": "buy", "date": day.isoformat(),
            "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": hm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "flat_by": flat_by,
            "currency": "INR", "tick": float(c["tick"]), "tick_source": tick_src,
            "stop_detail": c["stop_detail"], "created_at": now.strftime("%Y-%m-%dT%H:%M:%S+05:30"), "note": NOTE}
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    if st is not None:
        plan["supertrend"] = st
    warnings = []
    if flat_note:
        warnings.append("Flat-by %s is %s." % (flat_by, flat_note))
    if tick_src == "price band":
        warnings.append("Tick %s taken from the NSE price band, not the instrument master: check it before a limit order."
                        % num(c["tick"]))
    mrr = cfg.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: trader-risk checks that."
                            % (ratio(c["reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    today = now.date()
    if day != today:
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), today.isoformat()))
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan["setup"], plan["setup"])
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(PAISA, rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = " at %s" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s long, %s, %s on %s bars" % (
        plan["symbol"], day.strftime("%a %Y-%m-%d"), setup, "5-minute" if plan["interval"] == "5m" else "1-minute")]
    out.append("  Entry         %s%s" % (money(plan["entry"]), when))
    out.append("  Stop          %s  %s%s" % (money(plan["stop"]), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (money(plan["target"]), plan["target_label"]))
    out.append("  Risk a share  %s (%s%% of the entry; before charges, trader-budget adds them)" % (money(plan["risk_per_share"]), pct))
    out.append("  Reward:risk   %s" % ratio(plan["reward_risk"]))
    out.append("  Flat by       %s IST, win or lose: no overnight hold (MIS is squared off from about 15:20 anyway)" % plan["flat_by"])
    if plan.get("tick"):
        out.append("  Tick          %s" % num(plan["tick"]))
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def cmd_plan(a):
    now = parse_now(a.now)
    plan, warnings = build_plan(a, now)
    if a.write:
        d = os.path.dirname(a.write)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /trader-timemachine replays it, then /trader-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    for line in describe(dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan)):
        print(line)
    today = parse_now(a.now).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exit Planner (trader-exit, NSE): the stop and target before the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default trader/entry.json)")
    p.add_argument("--symbol")
    p.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, e.g. 10:00")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="v2: buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K, supertrend")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--tick", type=float, help="the symbol's tick in INR (default: entry.json, the master, the price band)")
    p.add_argument("--master", metavar="FILE", help="a saved instruments CSV for the tick size")
    p.add_argument("--flat-by", help="HH:MM IST (default: config flat_by, 15:15)")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="PATH", help="a saved bundle for an atr stop")
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-03 10:00"')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

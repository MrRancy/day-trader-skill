#!/usr/bin/env python3
"""Entry Timer for trader-entry (NSE): watches a setup bar by bar and says the
moment to get in. Standard library only. Long side only. It never places an order.

    python3 entry.py check TATACONSUM                          # opening-range breakout, 5-minute bars
    python3 entry.py check TATACONSUM --now "2026-11-03 10:00" --offline TATACONSUM.json
    python3 entry.py check TATACONSUM --setup level --level 1055.00    # first close above a price
    python3 entry.py check TATACONSUM --interval 1m --min-volume-x 1.5 --write
    python3 entry.py watch TATACONSUM --every 30 --until 15:00  # one line per closed bar
    python3 entry.py watchlist                                  # every hit in trader/watchlist.json

Setups (v2 plans longs only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 09:15, 09:20 and 09:25 five-minute bars).
         It triggers on the first COMPLETED bar after the range that CLOSES
         above the range high. A wick above the high is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from trader-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars (the 09:15 bar carries the pre-open
         auction's first prints, so 1.0 is already a high bar).

A trigger's time is the trigger bar's CLOSE time (the 09:55 bar closes at
10:00) and its price is that close, to the paisa. Only bars on the interval
grid count (anchored at 09:15) and a bar counts only once it has closed:
now >= its start + the interval. The 15:30 print is after the close.

Statuses: triggered; waiting (not open yet, range still forming, or no close
above the level yet); expired (no trigger by no_entries_after, default 15:00:
brokers square off MIS intraday positions from about 15:20); no-data (the
market is closed today or INDstocks sent no bars).

trader/config.json (optional): "no_entries_before" (09:30) and
"no_entries_after" (15:00), IST.

watch wakes just after each bar closes (bars only change then), re-polls every
--every seconds (default 30) while INDstocks has not posted the new bar, prints
one line per closed bar and stops on a trigger (exit 0, writes trader/entry.json),
on expiry, at the close or at --until (exit 1). With --now or --offline it runs
on a simulated clock and replays the day without sleeping.

Data: INDstocks 1-minute or 5-minute candles through ../trader_data.py (free,
INDSTOCKS_TOKEN, can lag a few seconds; bars, not single trades).
  --offline PATH reads a saved bundle (a trader-scanner --save file, or
  PATH/<SYMBOL>.json when PATH is a folder) instead. Without --now, an offline
  file is read as of its last bar's close.

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # realpath: works through a junction
import trader_data  # noqa: E402

PAUSE = 0.25     # seconds between requests (INDstocks allows 5 a second)
GRACE = 5        # seconds after a bar closes before watch asks for it
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": "09:30", "no_entries_after": "15:00"}

NSE_HOLIDAYS = trader_data.NSE_HOLIDAYS
CALENDAR_YEARS = trader_data.CALENDAR_YEARS
OPEN = trader_data.OPEN
CLOSE = trader_data.CLOSE
trading_day = trader_data.trading_day
session = trader_data.session
next_open = trader_data.next_open
ist_of = trader_data.ist_of
ts_of = trader_data.ts_of


class EntryError(Exception):
    pass


# ---------------------------------------------------------------- time (IST, naive, no DST)

def now_ist():
    return trader_data.now_ist().replace(microsecond=0)


def iso(ist):
    return ist.strftime("%Y-%m-%dT%H:%M:%S+05:30")


def parse_now(text):
    """--now -> naive IST. "2026-11-03 10:00" (or ...T10:00, or just 10:00 for today) is IST;
    an offset or Z is converted."""
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
        raise EntryError('--now wants an IST time like "2026-11-03 10:00" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return dt
    return (dt - dt.utcoffset()).replace(tzinfo=None) + trader_data.IST


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise EntryError("%s wants an IST time like 15:00, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    """10:00, 09:30: the contract's wall-clock form (24-hour, as NSE writes it)."""
    return "%02d:%02d" % (t.hour, t.minute)


short = hm


# ---------------------------------------------------------------- bars

def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, sym):
    if os.path.isdir(path):
        for name in (sym + ".json", sym + ".json.gz"):
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise EntryError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def parse_bundle(text, sym, interval=None):
    """A saved bundle -> {symbol, bars, as_of, interval}. Bars with a missing price are dropped."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise EntryError("the offline file for %s is not valid JSON" % sym)
    if not isinstance(doc, dict) or not isinstance(doc.get("bars"), dict):
        raise EntryError("the offline file for %s is not a trader bundle (no \"bars\")" % sym)
    held = dict((k.upper(), v) for k, v in doc["bars"].items())
    if sym.upper() not in held:
        raise EntryError("the offline file holds %s, not %s" % (", ".join(sorted(held)) or "nothing", sym))
    bars = clean_bars(held[sym.upper()])
    iv = doc.get("interval")
    short_iv = {"1minute": "1m", "5minute": "5m"}.get(iv, iv)
    return {"symbol": sym.upper(), "bars": bars, "as_of": None, "interval": short_iv}


def clean_bars(raw):
    bars = []
    for b in raw or []:
        try:
            c = float(b["c"])
            bars.append({"t": int(b["ts"]), "o": float(b.get("o", c)), "h": float(b.get("h", c)),
                         "l": float(b.get("l", c)), "c": c, "v": int(b.get("v") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    bars.sort(key=lambda b: b["t"])
    return bars


def get_chart(sym, interval, day=None, offline=None, backend=None):
    """Bars for one symbol: INDstocks for `day` (today by default), or a saved bundle."""
    sym = sym.upper()
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    if offline:
        f = offline_file(offline, sym)
        try:
            text = read_text(f)
        except (OSError, ValueError) as e:
            raise EntryError("could not read %s: %s" % (f, e))
        ch = parse_bundle(text, sym)
        if ch["interval"] and ch["interval"] != interval:
            raise EntryError("%s has %s bars, not %s: use --interval %s" % (sym, ch["interval"], interval, ch["interval"]))
        return ch
    day = day or now_ist().date()
    b = backend or trader_data.backend()
    try:
        bars = b.candles(sym, interval, day, day + datetime.timedelta(days=1))
    except trader_data.DataError as e:
        raise EntryError(str(e))
    return {"symbol": sym, "bars": clean_bars(bars), "as_of": None, "interval": interval}


# ---------------------------------------------------------------- the rule

def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise EntryError("%s is not valid JSON: %s" % (path, e))
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
    for k in DEFAULTS:
        parse_hhmm(cfg[k], k)
    return cfg


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def rs(x):
    return "Rs %.2f" % x


def evaluate(chart, now, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None):
    """Pure function: the setup's status at `now` (naive IST) from one session's bars."""
    cfg = cfg or dict(DEFAULTS)
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    n_or = or_minutes * 60 // step
    local = now
    day = local.date()
    res = {"symbol": chart["symbol"], "setup": setup, "side": "buy", "date": day.isoformat(),
           "interval": interval, "or_minutes": or_minutes, "now": local.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting",
           "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
           "last": None, "forming": None, "min_volume_x": min_volume_x,
           "prev_close": chart.get("prev_close"), "calendar_known": day.year in CALENDAR_YEARS, "_bars": []}
    ok, why = trading_day(day)
    if not ok:
        res.update(status="no-data", phase="closed", reason="the NSE is closed today (%s)" % why,
                   next_open=next_open(day))
        return res
    o, c = session(day)
    open_dt = datetime.datetime.combine(day, o)
    close_dt = datetime.datetime.combine(day, c)
    before = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_before"], "no_entries_before"))
    cutoff = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_after"], "no_entries_after"))
    if cutoff > close_dt:
        cutoff = close_dt
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res.update(opens=hm(o), closes=hm(c), no_entries_before=hm(before), no_entries_after=hm(cutoff))

    open_ts = ts_of(open_dt)
    by_slot = {}
    for b in chart["bars"]:
        if (b["t"] - open_ts) % step:
            continue                       # off the grid (the grid is anchored at 09:15)
        et = ist_of(b["t"])
        if et.date() != day or not (open_dt <= et < close_dt):
            continue                       # another day, pre-open, or the 15:30 closing print
        slot = int((et - open_dt).total_seconds()) // step
        by_slot[slot] = dict(b, et=et, end=et + datetime.timedelta(seconds=step), slot=slot)
    bars = []
    for slot in sorted(by_slot):
        b = by_slot[slot]
        if b["end"] <= local:
            bars.append(b)
        elif b["et"] <= local:
            res["forming"] = {"bar_start": hm(b["et"]), "closes": hm(b["end"]), "price": r2(b["c"])}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    if local < open_dt:
        res.update(phase="pre-open", reason="the NSE opens at %s%s" % (hm(o), (
            " and the %d-minute range is set at %s" % (or_minutes, hm(or_end)) if setup == "orb" else "")))
        return res

    or_bars = [b for b in bars if b["slot"] < n_or]
    avg_vol = (sum(b["v"] for b in or_bars) / float(len(or_bars))) if or_bars else None
    if or_bars:
        res["opening_range"] = {"high": r2(max(b["h"] for b in or_bars)), "low": r2(min(b["l"] for b in or_bars)),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": local >= or_end, "avg_volume": int(round(avg_vol))}
    if setup == "orb":
        if local < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, hm(or_end)))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="INDstocks sent no bars for the opening range")
            return res
        lvl = max(b["h"] for b in or_bars)
    else:
        lvl = float(level)
    res["level"] = r2(lvl)

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in bars:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if b["c"] > lvl:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": r2(last["c"]),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if local < close_dt else "after-close",
                   reason="the %s bar closed above %s" % (hm(trig["et"]), "the range high" if setup == "orb" else "the level"))
        res["trigger"] = {"time": hm(trig["end"]), "close_time": hm(trig["end"]), "bar_start": hm(trig["et"]),
                          "bar_index": trig["slot"], "price": r2(trig["c"]), "close": trig["c"],
                          "volume": trig["v"], "volume_x": r2(vx), "pct_over": r2(100.0 * (trig["c"] / lvl - 1))}
        res["minutes_since"] = int((local - trig["end"]).total_seconds() // 60)
        return res
    if not bars:
        res.update(status="no-data", phase="open", reason="INDstocks sent no bars for %s yet" % day.isoformat())
        return res
    what = "close above the range high %s" % rs(lvl) if setup == "orb" else "close above %s" % rs(lvl)
    if local >= cutoff:
        res.update(status="expired", phase="after-close" if local >= close_dt else "after-cutoff",
                   reason="no %s by %s, the last time for a new entry (MIS positions are squared off from about 15:20)"
                          % (what, hm(cutoff)))
        return res
    res["phase"] = "open"
    if last["c"] > lvl and last in thin:
        res["reason"] = "the %s bar closed above at %s but on %.2fx the range's volume, under your %.2fx" % (
            hm(last["et"]), rs(last["c"]), last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif last["c"] > lvl and last["end"] < before:
        res["reason"] = "the %s bar closed above at %s, but no entries before %s (config)" % (
            hm(last["et"]), rs(last["c"]), hm(before))
    else:
        gap = lvl - last["c"]
        res["reason"] = "last close %s (%s bar), %s %s %s (%+.2f%%)" % (
            rs(last["c"]), hm(last["et"]), rs(abs(gap)), "under" if gap >= 0 else "over",
            "the range high %s" % rs(lvl) if setup == "orb" else "the level %s" % rs(lvl),
            100.0 * (last["c"] / lvl - 1))
    return res


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return dict((k, v) for k, v in res.items() if not k.startswith("_"))


def entry_doc(res, now, tick=None):
    """trader/entry.json, the contract shape (plus a plain-words reason)."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "trader-entry", "symbol": res["symbol"], "setup": res["setup"], "side": "buy",
            "date": res["date"], "status": res["status"], "time": t.get("time"), "bar_start": t.get("bar_start"),
            "bar_index": t.get("bar_index"), "price": t.get("price"), "level": res.get("level"),
            "interval": res["interval"], "currency": "INR", "tick": tick,
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "source": "indstocks", "checked_at": iso(now), "reason": res.get("reason")}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"pre-open": "NOT OPEN YET", "forming": "RANGE FORMING", "closed": "MARKET CLOSED"}
SHORT = {"pre-open": "not open", "forming": "forming", "closed": "closed"}


def setup_name(res):
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    return "close above %s" % rs(res["level"]) if res["level"] is not None else "close above a level"


def describe(res):
    """Plain lines for a person."""
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    head = "%s  %s %s IST  %s on %s bars (INDstocks, can lag)" % (
        res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), setup_name(res), bars)
    out = [head]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high %s, low %s (%d of %d bars%s)." % (
            hm(start), hm(end), rs(orr["high"]), rs(orr["low"]), orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        out.append("TRIGGERED %s at %s: the %s bar (bar %d) closed above %s %s (%+.2f%%%s)." % (
            t["time"], rs(t["price"]), t["bar_start"], t["bar_index"],
            "the range high" if res["setup"] == "orb" else "the level", rs(res["level"]), t["pct_over"],
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("That was %d minutes ago; the last close is %s (%s bar). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], rs(res["last"]["close"]), res["last"]["bar_start"]))
        if res.get("minutes_since", 0) > step:
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you buy: /trader-exit "
                   "(exit.py plan --from trader/entry.json).")
    else:
        out.append("%s  %s." % (label, res["reason"]))
        f = res.get("forming")
        if f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming (%s so far, closes %s); it does not count until it closes." % (
                f["bar_start"], rs(f["price"]), f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s IST." % res["next_open"])
    if not res.get("calendar_known"):
        out.append("Holiday list covers %s only: check nseindia.com for this date." % " and ".join(map(str, CALENDAR_YEARS)))
    return out


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    start = hm(b["et"])
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far %s to %s" % (rs(min(x["l"] for x in seen)), rs(max(x["h"] for x in seen)))
        if b["slot"] == n_or - 1:
            note = "opening range set: high %s, low %s" % (rs(orr["high"]), rs(orr["low"]))
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed above %s (%+.2f%%). Get in now if you take it: %s, %s" % (
            rs(lvl), t["pct_over"], t["time"], rs(t["price"]))
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "above %s" % rs(lvl)
        if res["setup"] == "level" or b["slot"] >= n_or:
            avg = orr.get("avg_volume")
            if res.get("min_volume_x") and avg:
                note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
            elif b["end"] < datetime.datetime.combine(b["et"].date(), parse_hhmm(res["no_entries_before"], "x")):
                note += ", but no entries before %s" % res["no_entries_before"]
    else:
        note = "%s under %s" % (rs(lvl - b["c"]), rs(lvl))
    return "%5s bar  closed %s  %s" % (start, rs(b["c"]), note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now) -> chart, clock() -> naive IST, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5m")]
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        local = now
        day = local.date()
        s = session(day)
        if not s:
            emit("%s: market closed today (%s). Nothing to watch." % (day.isoformat(), trading_day(day)[1]))
            return 1, None
        open_dt = datetime.datetime.combine(day, s[0])
        close_dt = datetime.datetime.combine(day, s[1])
        if local < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                emit("%s IST  %s: watching from the first bar's close at %s." % (
                    local.strftime("%H:%M"), "not open yet" if local < open_dt else "open, no bar closed yet",
                    hm(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = open_dt + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s IST  could not read bars: %s" % (local.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or local >= close_dt:
            emit("EXPIRED  %s." % (res["reason"] or "the market closed without a trigger"))
            return 1, res
        if until and local >= until:
            emit("Stopped at --until %s without a trigger. %s." % (hm(until), res["reason"]))
            return 1, res
        # sleep until just after the next bar closes; poll every `every` seconds if INDstocks is behind
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - local).total_seconds()
        cutoff = datetime.datetime.combine(day, parse_hhmm(res.get("no_entries_after") or DEFAULTS["no_entries_after"], "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > local:
                wait = min(wait, (stop_at - local).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _opts(a, cfg):
    if a.side in ("sell", "short"):
        raise EntryError("v2 plans longs only: no short entries. Watch for a buy (--side buy) instead.")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg}


def as_of(chart, interval):
    """An offline file is read as of its last bar's close."""
    if chart["bars"]:
        return ist_of(chart["bars"][-1]["t"]) + datetime.timedelta(seconds=INTERVALS[interval])
    return now_ist()


def _load(a, sym, now):
    """(chart, now). An offline file without --now is read as of its last bar's close."""
    if a.offline and now is None:
        ch = get_chart(sym, a.interval, offline=a.offline)
        return ch, as_of(ch, a.interval)
    now = now or now_ist()
    return get_chart(sym, a.interval, now.date(), a.offline), now


def tick_of(a, sym, price):
    """The symbol's tick: from --master, else the downloaded master when online, else the price band."""
    rows = None
    if getattr(a, "master", None):
        rows = trader_data.parse_instruments(read_text(a.master))
    elif not a.offline:
        try:
            rows = trader_data.backend().instruments("equity")
        except trader_data.DataError:
            rows = None
    return trader_data.tick_size(sym, price, rows)[0]


def cmd_check(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = a.symbol.upper()
    chart, now = _load(a, sym, parse_now(a.now) if a.now else None)
    res = evaluate(chart, now, **opts)
    if a.write:
        price = (res.get("trigger") or {}).get("price") or (res.get("last") or {}).get("close") or res.get("level")
        write_json(a.write, entry_doc(res, now, tick_of(a, sym, price) if price else None))
    if a.json:
        print(json.dumps(public(res), indent=2))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = a.symbol.upper()
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    start = parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, None)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = now_ist, time.sleep

    def provider(now):
        return get_chart(sym, a.interval, now.date(), a.offline)

    until = None
    if a.until:
        t = parse_hhmm(a.until, "--until")
        until = datetime.datetime.combine(now_fn().date(), t)

    def on_trigger(res, now):
        write_json(a.out, entry_doc(res, now, tick_of(a, sym, res["trigger"]["price"])))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until,
                         on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    if not os.path.exists(a.watchlist):
        raise EntryError("no watchlist at %s: run /trader-scanner first (it writes the hits there)" % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (a.watchlist, e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % a.watchlist)
    now = parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        sym = str(h["symbol"]).upper()
        o = dict(opts)
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        try:
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i and not a.offline:
                time.sleep(PAUSE)
            chart, when = _load(a, sym, now)
            rows.append((sym, evaluate(chart, when, **o), None))
        except EntryError as e:
            rows.append((sym, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows], indent=2))
    else:
        print("%-12s  %-9s  %-6s  %-10s  %-10s  %-10s  %s" % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST", "VS LEVEL"))
        for s, r, e in rows:
            if e:
                print("%-12s  error: %s" % (s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
            print("%-12s  %-9s  %-6s  %-10s  %-10s  %-10s  %s" % (
                s, status[:9], t.get("time") or "-", ("%.2f" % t["price"]) if t else "-",
                ("%.2f" % r["level"]) if r.get("level") is not None else "-",
                ("%.2f" % last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <SYMBOL> --write, and /trader-exit." % (n, len(rows)))
        print("INDstocks bars, bar by bar, can lag. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry Timer (trader-entry, NSE): watches a setup bar by bar.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="v2: buy only")
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-03 10:00"')
        p.add_argument("--offline", metavar="PATH", help="a saved bundle (.json/.json.gz) or a folder of <SYMBOL>.json")
        p.add_argument("--master", metavar="FILE", help="a saved instruments CSV for the tick size (offline)")
        p.add_argument("--config", default="trader/config.json")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while INDstocks is behind")
    p.add_argument("--until", help="stop at this IST time (HH:MM)")
    p.add_argument("--out", default="trader/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except EntryError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

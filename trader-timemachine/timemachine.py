#!/usr/bin/env python3
"""Time Machine for trader-timemachine (NSE). Standard library only.

Replays a planned day trade before you risk a rupee: 10,000 simulated runs of
the rest of the session from your entry, plus the same rule on every past
session in the data.

    python3 timemachine.py run                          # trader/plan.json, 10,000 replays, seed 19
    python3 timemachine.py run --write                  # also save trader/timemachine.json
    python3 timemachine.py run --until 15:15            # stop each replay at your flat-by time
    python3 timemachine.py run --seed 7 --replays 20000 --json
    python3 timemachine.py run --symbol TATACONSUM --date 2026-11-03 --entry 1050.20 \\
        --stop 1039.40 --target 1061.00 --entry-time 10:00
    python3 timemachine.py chart                        # ASCII fan chart of the replays
    python3 timemachine.py chart --from trader/timemachine.json

How the replays work (the US original's sim.py, same algorithm, same random draws):
  1. Fetch 100 calendar days of 5-minute bars (INDstocks through ../trader_data.py,
     7 days a request). Keep only FULL sessions before the plan's date: 75 bars,
     09:15 to 15:30 IST. Gaps, special sessions and today are left out.
  2. The pool: for every bar slot 1..74 of the day, each past session's bar as
     ratios to the previous bar's close: (open, high, low, close) / prev close.
  3. One replay starts at the entry price and walks the rest of the session.
     For each bar i it picks a slot j = i + randint(-window, window), kept in
     1..74, then one past bar from pool[j] (random.choice), and scales it to
     the current price. The stop is checked before the target in every bar,
     so a bar that touches both counts as the stop (the conservative call).
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at --until (default 15:30, the close; your
     flat-by is 15:15 and brokers square off MIS from about 15:20).
     "green" = target first, or above the entry at --until (below for a sell).
     R = the target's R at the target, -1 at the stop, otherwise
     (price at --until - entry) / risk. A gap through the stop still counts
     -1R here; a real fill can be worse.
  5. The price at --until: median and the 5th to 95th percentile of all
     replays (pct(a, q) = sorted a at index int(q * n)). The fan chart uses a
     second run of 2,000 paths with the same seed. The histogram has 24 bins
     between the 1st and 99th percentile.
  6. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R) re-run on every one of those past
     full sessions, first close beyond the range = entry, same stop-first rule.
     Stops round to the symbol's tick, away from the entry.

The verdict line looks at the replays that hit one side (target or stop) and
compares the target's share of them, target / (target + stop), with the share
that breaks even at the plan's target R: 1 / (1 + R), so 50 % at 1R and 33 % at
2R. The gap, in percentage points:
  less than 5 points    "Close to a coin flip" (1R) / "Close to break-even" (other R)
  5 to 15 points        "Leans toward the target" / "Leans toward the stop"
  15 points or more     "Strongly toward the target" / "Strongly toward the stop"
Below it: a note when more than 40 % of the replays hit neither (the price at
--until decides those), and the average R when its sign goes against the lean.

5-minute bars only. The run refuses with a plain message when there are fewer
than 20 full sessions or the entry bar is not in the data. Same plan, same data
and same --seed give the same numbers on any machine. A simulation built from
the stock's own recent bars is not a prediction: results, news and circuit
halts can do what the last 100 days never did.

--offline FILE reads a saved bundle (.json or .json.gz) instead.
Times are IST wall-clock; --now "2026-11-03 10:00" pins the clock.
"""

import argparse
import datetime
import gzip
import json
import math
import os
import random
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))  # realpath: works through a junction
import trader_data  # noqa: E402

INTERVAL = 300       # 5-minute bars, in seconds
SESSION_BARS = 75    # 09:15 to 15:30 in 5-minute bars
OPEN_MIN = 9 * 60 + 15
CLOSE_MIN = 15 * 60 + 30
LOOKBACK_DAYS = 100  # calendar days of bars to fetch: about 65 sessions
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05     # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ist_of = trader_data.ist_of


class TMError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def parse_now(text):
    """-> naive IST. A time without an offset is IST."""
    if not text:
        return trader_data.now_ist().replace(microsecond=0)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TMError("--now wants a time like \"2026-11-03 10:00\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return dt
    return (dt - dt.utcoffset()).replace(tzinfo=None) + trader_data.IST


def parse_clock(text, what):
    """'10:00', '10:00 AM', '3:15 PM' or '3:15' (a bare 1 to 8 o'clock means p.m.) -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s wants an IST time like 10:00 or 15:15, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 8:
        h += 12
    return h * 60 + mi


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


clock = clock24


# ---------------------------------------------------------------- data

def read_text(path):
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    except OSError as e:
        raise TMError("could not read %s: %s" % (path, e))


def read_offline(path, sym):
    """A saved bundle (.json or .json.gz), or a folder holding <SYM>-100d5m.json(.gz) or <SYM>.json(.gz)."""
    if os.path.isdir(path):
        for name in ("%s-100d5m.json" % sym, "%s-100d5m.json.gz" % sym, "%s.json" % sym, "%s.json.gz" % sym):
            if os.path.exists(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
        else:
            raise TMError("no %s-100d5m.json(.gz) in %s" % (sym, path))
    if not os.path.exists(path):
        raise TMError("no offline chart file %s" % path)
    return read_text(path)


def fetch_bars(sym, now, backend=None):
    """100 calendar days of 5-minute bars through trader_data, as a bundle document."""
    b = backend or trader_data.backend()
    start = now.date() - datetime.timedelta(days=LOOKBACK_DAYS)
    end = now.date() + datetime.timedelta(days=1)
    try:
        bars = b.candles(sym, "5m", start, end)
    except trader_data.DataError as e:
        raise TMError(str(e))
    return {"created_by": "trader-timemachine", "source": "indstocks", "interval": "5minute",
            "start": str(start), "end": str(end), "bars": {sym: bars}, "failed": {}}


def bundle_bars(text_or_doc, sym):
    """A bundle (text or dict) -> the raw bar list for sym."""
    doc = text_or_doc
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except ValueError:
            raise TMError("the data for %s is not JSON" % sym)
    if not isinstance(doc, dict) or not isinstance(doc.get("bars"), dict):
        raise TMError("the data is not a trader bundle (no \"bars\")")
    held = dict((k.upper(), v) for k, v in doc["bars"].items())
    if sym not in held:
        raise TMError("the data is for %s but the plan is for %s" % (", ".join(sorted(held)) or "nobody", sym))
    iv = doc.get("interval")
    if iv and iv not in ("5minute", "5m"):
        raise TMError("the data has %s bars; the time machine needs 5-minute bars" % iv)
    return held[sym] or []


def parse_bars(raw, now):
    """Raw bars -> (bars, dropped). Regular-session 5-min bars only, oldest first.
    Dropped: bars with a missing price, bars off the 5-minute grid, bars not complete
    yet (now < start + 5 min), and bars outside 09:15 to 15:30 IST."""
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "outside_hours": 0}
    bars = {}
    for b in raw:
        try:
            ts = int(b["ts"])
            o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
            v = int(b.get("v") or 0)
        except (KeyError, TypeError, ValueError):
            dropped["no_price"] += 1
            continue
        local = ist_of(ts)
        mins = local.hour * 60 + local.minute - OPEN_MIN
        if local.second or mins % 5:
            dropped["off_grid"] += 1
            continue
        if mins < 0 or mins >= SESSION_BARS * 5:
            dropped["outside_hours"] += 1
            continue
        if local + datetime.timedelta(seconds=INTERVAL) > now:
            dropped["not_complete"] += 1
            continue
        bars[ts] = {"date": local.date().isoformat(), "slot": mins // 5, "start": local.strftime("%H:%M"),
                    "o": o, "h": h, "l": l, "c": c, "v": v}
    return [bars[t] for t in sorted(bars)], dropped


def split_sessions(bars):
    """{date: {slot: bar}}."""
    days = {}
    for b in bars:
        days.setdefault(b["date"], {})[b["slot"]] = b
    return days


def full_sessions(days, before):
    """Full sessions (all 75 bars) strictly before the plan's date, oldest first,
    and the short ones that were left out (gaps, special sessions)."""
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        if len(s) == SESSION_BARS:
            full.append((d, [s[i] for i in range(SESSION_BARS)]))
        else:
            short.append({"date": d, "bars": len(s)})
    return full, short


# ---------------------------------------------------------------- the simulation (sim.py)

def build_pool(sessions):
    """pool[i] = every past session's bar i as (o, h, l, c) / bar i-1's close, in date order."""
    pool = [[] for _ in range(SESSION_BARS)]
    for _, D in sessions:
        for i in range(1, SESSION_BARS):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2, side="buy"):
    """Monte Carlo replays of bars k+1..last. Same draws and order as the US original's sim.py."""
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(SESSION_BARS - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, 2))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = dict((x, outcomes.count(x)) for x in ("target", "stop", "close"))
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(p)
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), 2) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
        "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins},
    }


# ---------------------------------------------------------------- stop rules and the history replay

def tick_away(x, long, tick=0.05):
    """Round a stop onto the tick grid, away from the entry: down for a buy, up for a sell."""
    units = round(x / tick, 6)
    n = math.floor(units) if long else math.ceil(units)
    return round(n * tick, 2)


def opening_range(D, n):
    """(high, low) of the first n bars of a session given as {slot: bar} or a list."""
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


SUPERTREND = (10, 3.0)


def rule_stop(rule, oh, ol, entry, long, bars=None, k=None):
    """The stop the rule gives on a session. None when the rule has no stop there (a Supertrend
    line on the wrong side of the entry)."""
    r = rule["stop_rule"]
    tick = rule.get("tick") or 0.05
    if r == "or_mid":
        return tick_away((oh + ol) / 2, long, tick)
    if r in ("or_low", "or_high"):
        return tick_away(ol if long else oh, long, tick)
    if r == "supertrend":
        if bars is None or k is None:
            return None
        upto = bars[:k + 1]
        line, direction = trader_data.supertrend([b["h"] for b in upto], [b["l"] for b in upto],
                                                 [b["c"] for b in upto], *SUPERTREND)
        if line[-1] is None or direction[-1] != (1 if long else -1):
            return None
        return tick_away(line[-1], long, tick)
    frac = rule["stop_pct"] / 100.0
    return tick_away(entry * (1 - frac) if long else entry * (1 + frac), long, tick)


def history_replay(sessions, rule, last, side="buy"):
    """The plan's rule on every past full session: entry = the first bar after the
    opening range to close beyond it (up to the --until bar), then stop first, then
    target, bar by bar, else the close at --until."""
    long = side == "buy"
    n = rule["or_minutes"] // 5
    trades = []
    skipped = 0
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, last + 1) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        e = round(D[kk]["c"], 2)
        s = rule_stop(rule, oh, ol, e, long, D, kk)
        if s is None:
            skipped += 1
            continue
        risk = (e - s) if long else (s - e)
        if risk <= 0:
            continue
        t = round(e + rule["target_r"] * risk, 2) if long else round(e - rule["target_r"] * risk, 2)
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        R = ((t - e) if long else (e - t)) / risk if oc == "target" else -1.0 if oc == "stop" else \
            ((cl - e) if long else (e - cl)) / risk
        trades.append({"date": d, "time": clock(OPEN_MIN + 5 * (kk + 1)), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "no_stop": skipped,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, now):
    """The plan from --plan (trader/plan.json) or from the flags, checked and normalized."""
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target", "entry_time") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": a.side, "date": a.date or now.date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": "5m", "tick": a.tick}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("no plan at %s: run /trader-exit first, or pass --symbol --date --entry --stop "
                          "--target --entry-time" % a.plan)
        try:
            with open(a.plan, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (a.plan, e))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % a.plan)
        if a.tick:
            raw = dict(raw, tick=a.tick)
        src = a.plan
    return normalize_plan(raw, src)


def normalize_plan(raw, src="flags"):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    sym = str(raw.get("symbol") or "").strip().upper()
    if not re.match(r"^[A-Z0-9][A-Z0-9.&\-]{0,19}$", sym):
        raise TMError("the plan needs an NSE symbol, got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side must be buy or sell, got %r" % raw.get("side"))
    interval = str(raw.get("interval") or "5m")
    if interval != "5m":
        raise TMError("5-minute bars only (the plan says %s): 1-minute bars make a pool of single trades, "
                      "not of sessions" % interval)
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("the plan needs a date like 2026-11-03, got %r" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)" % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("a sell needs target < entry < stop (got target %s, entry %s, stop %s)" % (target, entry, stop))
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if (close_min - OPEN_MIN) % 5:
            raise TMError("entry_time %s is not on the 5-minute grid (use the bar's close, e.g. 10:00)"
                          % raw["entry_time"])
        k = (close_min - OPEN_MIN) // 5 - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                          % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 10:00) or entry_bar")
    if not 0 <= k <= SESSION_BARS - 2:
        raise TMError("the entry must close between 09:20 and 15:25 to leave bars to replay "
                      "(got %s)" % clock(OPEN_MIN + 5 * (k + 1)))
    orng = raw.get("opening_range") or {}
    or_min = orng.get("minutes", 15) if isinstance(orng, dict) else 15
    try:
        or_min = int(or_min)
    except (TypeError, ValueError):
        raise TMError("opening_range.minutes must be a number")
    if or_min < 5 or or_min % 5 or or_min >= SESSION_BARS * 5:
        raise TMError("opening_range.minutes must be a multiple of 5 (got %s)" % or_min)
    tick = raw.get("tick")
    if tick is not None:
        try:
            tick = float(tick)
        except (TypeError, ValueError):
            raise TMError("tick must be a number, got %r" % raw.get("tick"))
        if tick <= 0:
            raise TMError("tick must be above 0")
    else:
        tick = trader_data.tick_size(price=entry)[0]
    risk = (entry - stop) if long else (stop - entry)
    return {
        "symbol": sym, "side": side, "date": date, "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": clock(OPEN_MIN + 5 * (k + 1)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_share": round(risk, 2),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "5m", "tick": tick,
        "flat_by": raw.get("flat_by"), "from": src,
    }


def resolve_rule(plan, entry_day):
    """The rule the history replay uses: orb, the plan's opening range, its stop rule
    (or_mid, or_low / or_high, or 'pct' = the plan's stop distance as a percent of
    the entry), and the plan's target R. 'auto' works the stop rule out from the
    plan day's own opening range. None when the setup is not orb."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    n = plan["opening_range"]["minutes"] // 5
    tick = plan["tick"]
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"], "tick": tick,
            "stop_pct": round(100.0 * plan["risk_per_share"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(tick_away((oh + ol) / 2, long, tick) - plan["stop"]) < 0.005:
                rule["stop_rule"] = "or_mid"
            elif abs(tick_away(ol if long else oh, long, tick) - plan["stop"]) < 0.005:
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far, "supertrend"):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the run

def _until(mc):
    return mc.get("until") or clock(CLOSE_MIN)


def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one side,
    minus the share that breaks even at the target's R. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds."""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (
            _until(mc), _until(mc))
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, _until(mc), _until(mc)))
    lv = lean(mc)
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    return notes


def time_machine(data, plan, now, replays=10000, seed=19, window=2, until=None, source="indstocks"):
    """A bundle (text or dict) + a normalized plan -> the timemachine.json document."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    sym = plan["symbol"]
    raw = bundle_bars(data, sym)
    bars, dropped = parse_bars(raw, now)
    if not bars:
        raise TMError("no complete 5-minute bars for %s in the data" % sym)
    k = plan["entry_bar"]
    until_min = CLOSE_MIN if until is None else parse_clock(until, "--until")
    if (until_min - OPEN_MIN) % 5 or until_min > CLOSE_MIN:
        raise TMError("--until must be on the 5-minute grid and no later than %s (the close)" % clock(CLOSE_MIN))
    last = (until_min - OPEN_MIN) // 5 - 1
    if last <= k:
        raise TMError("--until %s leaves nothing to replay after the %s entry" % (clock(until_min),
                                                                             plan["entry_time"]))
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        reason = ("no bars for that day" if not entry_day else
                  "the data stops at the %s bar" % entry_day[max(entry_day)]["start"])
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock(OPEN_MIN + 5 * k), plan["entry_time"], plan["date"], sym, reason))
    eb = entry_day[k]
    warn = []
    if not eb["l"] - 0.01 <= plan["entry"] <= eb["h"] + 0.01:
        warn.append("the plan's entry %.2f is outside that bar's range %.2f to %.2f"
                    % (plan["entry"], eb["l"], eb["h"]))
    sessions, short = full_sessions(days, plan["date"])
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d"
                      % (len(sessions), sym, plan["date"], MIN_SESSIONS))
    pool = build_pool(sessions)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window, plan["side"])
    mc = dict(mc, until=clock(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0],
              band_times=[clock(OPEN_MIN + 5 * (j + 1)) for j in range(k, last + 1)])
    rule = resolve_rule(plan, entry_day)
    plan_out = dict((kk, v) for kk, v in plan.items() if v is not None)
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last, plan["side"])
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    last_bar = bars[-1]
    return {
        "created_by": "trader-timemachine",
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
        "source": source,
        "data": {"symbol": sym, "interval": "5m", "lookback_days": LOOKBACK_DAYS,
                 "last_bar": "%s %s IST" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "mc": mc,
        "history": history,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d full sessions. It is not a prediction: results, "
                 "news and circuit halts can do things those sessions never did. Same seed, same numbers." % (sym, len(sessions))),
    }


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    long = p["side"] == "buy"
    until = m["until"]
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    r = p["target_r"]
    lines = [
        "%s %s, %s %s: entry %.2f at %s (bar %d), stop %.2f, target %.2f (%sR, risk %.2f a share)."
        % (p["symbol"], p["side"], wd, p["date"], p["entry"], p["entry_time"], p["entry_bar"], p["stop"],
           p["target"], ("%g" % r), p["risk_per_share"]),
        "%s replays of the rest of the session, %s to %s IST (%d five-minute bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], m["seed"]),
        "Each bar is drawn whole from %s's own 5-min bars at the same time of day (+-%d bars) over its last "
        "%d full sessions (%s to %s)." % (p["symbol"], m["window"], m["pool_sessions"], m["pool_from"], m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or %s the entry at %s" % (pc(m["p_green"]),
                                                                       "above" if long else "below", until),
        "",
        "  Price at %s: median %.2f, 90 %% of replays between %.2f and %.2f."
        % (until, m["median_close"], m["p5_close"], m["p95_close"]),
        "  Average R: %+.3f (R = the %.2f risk: %sR at the target, -1R at the stop)."
        % (m["avg_R"], p["risk_per_share"], ("+%g" % m["r_at_target"]), ),
        "",
    ]
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
                      "or_high": "opening-range high",
                      "supertrend": "Supertrend(%d, %g) line at the entry" % SUPERTREND}.get(
                          ru["stop_rule"], "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target) on each of "
                         "those %d real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f.%s"
                         % (ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"],
                            (" %d more breakouts had the Supertrend line above the entry: no stop, not taken."
                             % h["no_stop"]) if h.get("no_stop") else ""))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s) (gaps or special sessions): %s."
                     % (len(doc["data"]["short_sessions_left_out"]),
                        ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per 5-min bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))
    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9.2f |" % price) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        lab = times[j]
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")


def compute(a):
    now = parse_now(a.now)
    plan = load_plan(a, now)
    if a.offline:
        data, source = read_offline(a.offline, plan["symbol"]), "offline"
    else:
        data, source = fetch_bars(plan["symbol"], now), "indstocks"
    return time_machine(data, plan, now, a.replays, a.seed, a.window, a.until, source)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc=dict((k, v) for k, v in doc["mc"].items() if k != "sample_paths"))
        print(json.dumps(slim, indent=2))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: /trader-budget sizes it to your budget and your max loss per trade.")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("no %s: run timemachine.py run --write first" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


# ---------------------------------------------------------------- the evening replay: every chart, one rule

U_FLAT_MIN = 15 * 60 + 15        # flat at the close of the bar ending 15:15
U_DEFAULTS = {"target_r": 2.0, "gate_pct": 65.0, "gate_time": "10:00", "cutoff": "11:00", "budget": 10000.0,
              "max_trades": 2, "max_loss_pct": 3.0, "position_pct": 75.0, "min_price": 50.0, "or_minutes": 15,
              "first_entry": "09:35"}


def load_universe(path, day=None):
    """A scanner --save bundle -> (day, {SYM: [75 bars of that day]}), only full regular sessions."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    raw = doc.get("bars") if isinstance(doc, dict) else None
    if not isinstance(raw, dict):
        raise TMError("%s is not a scanner bundle (no \"bars\")" % path)
    far = datetime.datetime(2100, 1, 1)
    out = {}
    days = {}
    for sym, bars in raw.items():
        parsed, _ = parse_bars(bars or [], far)
        for d, s in split_sessions(parsed).items():
            days[d] = days.get(d, 0) + 1
            if len(s) == SESSION_BARS:
                out.setdefault(d, {})[sym] = [s[i] for i in range(SESSION_BARS)]
    if not days:
        raise TMError("no 5-minute bars in %s" % path)
    day = day or max(days, key=lambda d: (days[d], d))
    if day not in out:
        raise TMError("no full sessions on %s in %s" % (day, path))
    return day, out[day]


def breadth_at(sessions, at_min, min_price):
    """Share of charts whose bar ending at `at_min` closed above the 09:15 open."""
    k = (at_min - OPEN_MIN) // 5 - 1
    n = up = 0
    for bars in sessions.values():
        if bars[0]["o"] < min_price:
            continue
        n += 1
        up += bars[k]["c"] > bars[0]["o"]
    return (100.0 * up / n) if n else None, n


def u_shares(inr, price):
    return int(round(inr * 100)) // int(round(price * 100)) if price > 0 else 0


def u_shares_for_loss(max_loss, risk, fill, stop, rates):
    q = u_shares(max_loss, risk) if risk > 0 else 0
    while q > 0 and q * risk + trader_data.charges(q, fill, stop, rates)["total"] > max_loss + 0.005:
        q -= 1
    return q


STRATEGIES = {
    "orb": "the first 5-minute close above the 15-minute opening range; stop at the range midpoint; target R x risk",
    "vwap": "a 5-minute close at least vwap_pct (0.4 %) under the session VWAP; target the VWAP of that bar; stop the "
            "same distance under the entry (openalgo.in/python/intraday-strategies, buy-the-dip)",
    "ema": "EMA 9 crosses above EMA 21 on 5-minute closes (both seeded from the day's own bars, so the first cross "
           "cannot come before 11:00); stop one ATR(14) under the entry; target R x risk; exit on the crossunder",
    "supertrend_rsi": "5-minute Supertrend(10, 3) turns up with RSI(14) between 50 and 70; stop at the Supertrend "
                      "line; target R x risk",
}


def _target(entry, risk, r, tick):
    return round(math.ceil(round((entry + r * risk) / tick, 6) - 1e-9) * tick, 2)


def first_signal(strategy, bars, rule, tick, lo_min, hi_min, n_or):
    """The first entry of `strategy` on one session's 75 bars whose trigger bar closes between lo_min and
    hi_min (minutes after midnight). Returns {"k", "close_min", "entry", "stop", "target", "rule_exit"} or
    None. The entry is the trigger bar's close; the caller fills at the next bar's open."""
    closes = [b["c"] for b in bars]
    highs, lows = [b["h"] for b in bars], [b["l"] for b in bars]
    r = rule["target_r"]

    def window(k):
        m = OPEN_MIN + 5 * (k + 1)
        return lo_min <= m <= hi_min, m

    if strategy == "orb":
        oh, ol = opening_range(bars, n_or)
        for k in range(n_or, SESSION_BARS):
            if closes[k] > oh:
                ok, m = window(k)
                if not ok:
                    return None          # the first breakout is the only one; outside the window there is no trade
                entry = round(closes[k], 2)
                stop = tick_away((oh + ol) / 2, True, tick)
                risk = round(entry - stop, 2)
                if risk <= 0:
                    return None
                return {"k": k, "close_min": m, "entry": entry, "stop": stop, "target": _target(entry, risk, r, tick),
                        "rule_exit": None}
        return None

    if strategy == "vwap":
        pct = float(rule.get("vwap_pct", 0.4)) / 100.0
        pv = vol = 0.0
        for k in range(SESSION_BARS):
            b = bars[k]
            pv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
            vol += b["v"]
            if not vol:
                continue
            vwap = pv / vol
            ok, m = window(k)
            if m < lo_min:
                continue
            if m > hi_min:
                return None
            if closes[k] <= vwap * (1 - pct):
                entry = round(closes[k], 2)
                gap = vwap - entry
                stop = tick_away(entry - gap, True, tick)
                target = tick_away(vwap, False, tick)          # up to the tick: the VWAP of the signal bar
                if entry - stop <= 0 or target <= entry:
                    return None
                return {"k": k, "close_min": m, "entry": entry, "stop": stop, "target": target, "rule_exit": None}
        return None

    if strategy == "ema":
        fast, slow = trader_data.ema(closes, 9), trader_data.ema(closes, 21)
        atr = trader_data.atr_series(highs, lows, closes, 14)
        for k in range(1, SESSION_BARS):
            if None in (fast[k - 1], slow[k - 1], fast[k], slow[k]):
                continue
            if fast[k - 1] <= slow[k - 1] and fast[k] > slow[k]:
                ok, m = window(k)
                if m < lo_min:
                    continue
                if m > hi_min:
                    return None
                a = atr[k] if atr[k] is not None else (highs[k] - lows[k])
                entry = round(closes[k], 2)
                stop = tick_away(entry - a, True, tick)
                risk = round(entry - stop, 2)
                if risk <= 0:
                    return None
                exit_k = next((j for j in range(k + 1, SESSION_BARS)
                               if fast[j] is not None and slow[j] is not None and fast[j] < slow[j]), None)
                return {"k": k, "close_min": m, "entry": entry, "stop": stop, "target": _target(entry, risk, r, tick),
                        "rule_exit": exit_k}
        return None

    if strategy == "supertrend_rsi":
        line, direction = trader_data.supertrend(highs, lows, closes, 10, 3.0)
        rsi = trader_data.rsi_series(closes, 14)
        for k in range(1, SESSION_BARS):
            if direction[k] == 1 and direction[k - 1] == -1 and rsi[k] is not None and 50 <= rsi[k] <= 70:
                ok, m = window(k)
                if m < lo_min:
                    continue
                if m > hi_min:
                    return None
                entry = round(closes[k], 2)
                stop = tick_away(line[k], True, tick)
                risk = round(entry - stop, 2)
                if risk <= 0:
                    return None
                return {"k": k, "close_min": m, "entry": entry, "stop": stop, "target": _target(entry, risk, r, tick),
                        "rule_exit": None}
        return None
    raise TMError("unknown strategy %r: one of %s" % (strategy, ", ".join(STRATEGIES)))


def universe_replay(day, sessions, rule, rows=None, rates=None, scores=None):
    """One day, one rule, every chart. Returns the day's record.

    rule: target_r, gate_pct (0 = no gate), gate_time, cutoff ("" = none), budget, max_trades, max_loss_pct,
    position_pct, min_price, or_minutes, first_entry, stop_after_loss (bool), min_score (None = no filter).
    Entry = the first close above the opening range, filled at the next bar's open; stop = the range midpoint on
    the tick; target = entry + R x risk; stop first, then target, else flat at 15:15. EVERY breakout is scored at
    the budget each; the LEDGER takes them in trigger order (most traded first on the same bar), one position at
    a time, up to max_trades, and stops after the first loss when stop_after_loss is on."""
    rates = rates or trader_data.CHARGE_RATES
    n_or = rule["or_minutes"] // 5
    gate_min = parse_clock(rule["gate_time"], "gate_time") if rule.get("gate_pct") else None
    cut_min = parse_clock(rule["cutoff"], "cutoff") if rule.get("cutoff") else None
    first_min = parse_clock(rule["first_entry"], "first_entry")
    pct, counted = breadth_at(sessions, gate_min or 10 * 60, rule["min_price"])
    gate_open = (not rule.get("gate_pct")) or (pct is not None and pct >= rule["gate_pct"])
    rec = {"date": day, "charts": len(sessions), "breadth_pct": round(pct, 1) if pct is not None else None,
           "breadth_counted": counted, "gate_open": gate_open, "rule": rule, "every": [], "ledger": [],
           "budget": rule["budget"], "strategy": rule.get("strategy", "orb")}
    if not gate_open:
        rec["cash_end"] = rule["budget"]
        return rec
    # the signals, in trigger order; the most traded (rupees in the opening range) first on the same bar
    strategy = rule.get("strategy", "orb")
    lo_min = first_min if not gate_min else max(first_min, gate_min)
    hi_min = cut_min or 15 * 60
    hits = []
    for sym, bars in sessions.items():
        if bars[0]["o"] < rule["min_price"]:
            continue
        tick = trader_data.tick_size(sym, bars[0]["c"], rows)[0]
        sig = first_signal(strategy, bars, rule, tick, lo_min, hi_min, n_or)
        if sig is None:
            continue
        if rule.get("min_score") is not None:
            sc = (scores or {}).get(sym)
            if sc is None or sc < rule["min_score"]:
                continue
        traded = sum(b["c"] * b["v"] for b in bars[:n_or])
        hits.append((sig["close_min"], -traded, sym, sig))
    hits.sort(key=lambda h: h[:3])
    cash, trades, lost, open_until = float(rule["budget"]), 0, False, -1
    for close_min, _, sym, sig in hits:
        bars = sessions[sym]
        kk = sig["k"]
        if kk + 1 >= SESSION_BARS:
            continue
        fill_bar = bars[kk + 1]
        fill_close_min = OPEN_MIN + 5 * (kk + 2)
        if fill_close_min > U_FLAT_MIN:
            continue
        entry, stop, target = sig["entry"], sig["stop"], sig["target"]
        fill = fill_bar["o"]
        risk = round(entry - stop, 2)
        if risk <= 0 or fill - stop <= 0 or target <= fill:
            continue
        ex = why = None
        for i in range(kk + 1, SESSION_BARS):
            b = bars[i]
            end_min = OPEN_MIN + 5 * (i + 1)
            if b["l"] <= stop:
                ex, why, when = stop, "stop", end_min
                break
            if b["h"] >= target:
                ex, why, when = target, "target", end_min
                break
            if sig.get("rule_exit") is not None and i >= sig["rule_exit"] and i > kk + 1:
                ex, why, when = b["c"], "exit", end_min       # the strategy's own exit (an EMA crossunder)
                break
            if end_min >= U_FLAT_MIN:
                ex, why, when = b["c"], "flat", end_min
                break
        if ex is None:
            ex, why, when = bars[-1]["c"], "flat", CLOSE_MIN
        q_all = u_shares(rule["budget"], fill)
        chg_all = trader_data.charges(q_all, fill, ex, rates)["total"] if q_all else 0.0
        e = {"symbol": sym, "at": clock(close_min), "entry": entry, "fill": fill, "stop": stop, "target": target,
             "risk": risk, "exit": ex, "why": why, "when": clock(when), "R": round((ex - fill) / risk, 2),
             "qty": q_all, "gross": round(q_all * (ex - fill), 2), "charges": chg_all,
             "net": round(q_all * (ex - fill) - chg_all, 2)}
        rec["every"].append(e)
        if trades >= rule["max_trades"] or lost or close_min <= open_until:
            continue
        max_loss = rule["budget"] * rule["max_loss_pct"] / 100
        q = u_shares_for_loss(max_loss, fill - stop, fill, stop, rates)
        q = min(q, u_shares(rule["budget"] * rule["position_pct"] / 100, fill), u_shares(cash, fill))
        if q < 1:
            continue
        chg = trader_data.charges(q, fill, ex, rates)["total"]
        pnl = round(q * (ex - fill) - chg, 2)
        cash = round(cash + pnl, 2)
        trades += 1
        lost = rule.get("stop_after_loss") and pnl < 0
        open_until = when
        rec["ledger"].append(dict(e, qty=q, gross=round(q * (ex - fill), 2), charges=chg, net=pnl, cash_after=cash))
    rec["cash_end"] = cash
    return rec


def universe_summary(rec):
    ev = rec["every"]
    return {"date": rec["date"], "strategy": rec.get("strategy", "orb"), "charts": rec["charts"],
            "breadth_pct": rec["breadth_pct"], "gate_open": rec["gate_open"],
            "breakouts": len(ev), "target": sum(1 for e in ev if e["why"] == "target"),
            "stop": sum(1 for e in ev if e["why"] == "stop"),
            "flat": sum(1 for e in ev if e["why"] in ("flat", "exit")),
            "rule_exit": sum(1 for e in ev if e["why"] == "exit"),
            "flat_green": sum(1 for e in ev if e["why"] == "flat" and e["R"] > 0),
            "avg_R": round(sum(e["R"] for e in ev) / len(ev), 3) if ev else None,
            "gross": round(sum(e["gross"] for e in ev), 2), "charges": round(sum(e["charges"] for e in ev), 2),
            "net": round(sum(e["net"] for e in ev), 2), "ledger_trades": len(rec["ledger"]),
            "ledger_net": round(rec["cash_end"] - rec["budget"], 2)}


def rupees(x):
    return ("+Rs " if x >= 0 else "-Rs ") + "{:,.2f}".format(abs(x))


def render_universe(rec, label):
    s = universe_summary(rec)
    r = rec["rule"]
    out = ["%s, %s%s: %s charts with a full session; breadth at %s %s%% (gate %s): %s." % (
        rec["date"], ("%s, " % s["strategy"]) if s["strategy"] != "orb" else "", label, "{:,}".format(s["charts"]), r["gate_time"],
        s["breadth_pct"] if s["breadth_pct"] is not None else "n/a",
        ("%g%%" % r["gate_pct"]) if r.get("gate_pct") else "off",
        "OPEN" if s["gate_open"] else "CLOSED, no trades")]
    if not s["gate_open"]:
        return "\n".join(out) + "\n"
    out.append("  every %s at Rs %s each: %d taken, %d target, %d stop, %d flat at 15:15%s (%d of those above the "
               "entry); average R %+.2f; gross %s, charges %s, net %s."
               % ("signal" if s["strategy"] != "orb" else "breakout", "{:,.0f}".format(rec["budget"]), s["breakouts"],
                  s["target"], s["stop"], s["flat"], (" or on the rule's exit (%d)" % s["rule_exit"]) if s["rule_exit"] else "",
                  s["flat_green"], s["avg_R"] or 0.0, rupees(s["gross"]), "Rs {:,.2f}".format(s["charges"]), rupees(s["net"])))
    out.append("  the ledger, Rs %s for the day, %d trade%s max, %s%% max loss a trade%s:" % (
        "{:,.0f}".format(rec["budget"]), r["max_trades"], "" if r["max_trades"] == 1 else "s", "%g" % r["max_loss_pct"],
        ", over after a loss" if r.get("stop_after_loss") else ""))
    for t in rec["ledger"]:
        out.append("    %s %-12s %d at %.2f, stop %.2f, target %.2f -> %s %.2f at %s: %s (charges Rs %.2f)"
                   % (t["at"], t["symbol"], t["qty"], t["fill"], t["stop"], t["target"], t["why"], t["exit"], t["when"],
                      rupees(t["net"]), t["charges"]))
    if not rec["ledger"]:
        out.append("    no trade could be sized")
    out.append("  day: %s (cash Rs %s)." % (rupees(s["ledger_net"]), "{:,.2f}".format(rec["cash_end"])))
    return "\n".join(out) + "\n"


def cmd_universe(a):
    cfg = {}
    if a.config and os.path.exists(a.config):
        with open(a.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
    rule = dict(U_DEFAULTS)
    rule["gate_pct"] = float(cfg.get("breadth_gate_pct") or 0) or U_DEFAULTS["gate_pct"]
    rule["gate_time"] = cfg.get("gate_time") or U_DEFAULTS["gate_time"]
    rule["budget"] = float(cfg.get("budget") or U_DEFAULTS["budget"])
    rule["max_trades"] = int(cfg.get("max_trades_per_day") or U_DEFAULTS["max_trades"])
    rule["stop_after_loss"] = bool(cfg.get("stop_after_loss", True))
    if a.target:
        m = re.match(r"^\s*([0-9.]+)\s*r?\s*$", a.target, re.I)
        if not m or float(m.group(1)) <= 0:
            raise TMError("--target wants 1R, 1.5R, 2R ...")
        rule["target_r"] = float(m.group(1))
    if a.gate is not None:
        rule["gate_pct"] = float(a.gate)
    if a.cutoff is not None:
        rule["cutoff"] = "" if a.cutoff.lower() in ("none", "off", "") else a.cutoff
    if a.budget:
        rule["budget"] = float(a.budget)
    if a.max_trades:
        rule["max_trades"] = int(a.max_trades)
    if a.no_stop_after_loss:
        rule["stop_after_loss"] = False
    if a.min_score is not None:
        rule["min_score"] = int(a.min_score)
    rule["strategy"] = a.strategy
    if a.vwap_pct is not None:
        rule["vwap_pct"] = float(a.vwap_pct)
    rows = None
    if a.master:
        rows = trader_data.parse_instruments(trader_data.read_text(a.master))
    day, sessions = load_universe(a.bundle, a.date)
    scores = None
    if a.min_score is not None:
        if not a.daily:
            raise TMError("--min-score needs --daily, a saved daily bundle")
        scores = {}
        opener = gzip.open if a.daily.endswith(".gz") else open
        with opener(a.daily, "rt", encoding="utf-8") as fh:
            ddoc = json.load(fh)
        for sym, bars in (ddoc.get("bars") or {}).items():
            bs = sorted((b for b in bars if ist_of(b["ts"]).date().isoformat() < day), key=lambda b: b["ts"])
            if len(bs) >= 51:
                sc = trader_data.score_card([b["h"] for b in bs], [b["l"] for b in bs], [b["c"] for b in bs])
                scores[sym] = sc["score"] if sc else None
    rec = universe_replay(day, sessions, rule, rows, trader_data.CHARGE_RATES, scores)
    base = None
    if a.compare:
        brule = dict(rule, target_r=1.0, gate_pct=0.0, cutoff="", stop_after_loss=False, max_trades=3, strategy="orb")
        brule.pop("min_score", None)
        base = universe_replay(day, sessions, brule, rows, trader_data.CHARGE_RATES, None)
    others = []
    if a.others:
        for strat in sorted(STRATEGIES):
            if strat == rule["strategy"]:
                continue
            orule = dict(rule, strategy=strat, cutoff="" if strat == "ema" else rule["cutoff"])
            others.append(universe_summary(universe_replay(day, sessions, orule, rows, trader_data.CHARGE_RATES, scores)))
    doc = {"created_by": "trader-timemachine", "mode": "universe", "bundle": os.path.basename(a.bundle),
           "summary": universe_summary(rec), "rule": rule, "ledger": rec["ledger"],
           "every": rec["every"] if a.keep_every else None,
           "baseline": universe_summary(base) if base else None, "others": others or None,
           "note": "Every number is a count on this day's bars with fills at the trigger prices. Not a forecast."}
    if a.write:
        path = a.write if a.write is not True else os.path.join("trader", "replays", "%s.json" % day)
        _write(path, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    label = "%gR, gate %s, cutoff %s%s" % (rule["target_r"], ("%g%%" % rule["gate_pct"]) if rule["gate_pct"] else "off",
                                         rule["cutoff"] or "none",
                                         (", score >= %d" % rule["min_score"]) if rule.get("min_score") is not None else "")
    sys.stdout.write(render_universe(rec, label))
    if base:
        sys.stdout.write(render_universe(base, "baseline 1R, no gate, no cutoff, 3 trades"))
    for o in others:
        print("  %-15s %s" % (o["strategy"] + ":", "gate closed, none taken" if not o["gate_open"] else
                              "%d signals, %d target, %d stop, %d flat, avg R %+.2f, net %s, ledger %s"
                              % (o["breakouts"], o["target"], o["stop"], o["flat"], o["avg_R"] or 0.0,
                                 rupees(o["net"]), rupees(o["ledger_net"]))))
    if a.write:
        print("Wrote %s." % path)
    print(doc["note"])
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned day trade (trader-timemachine, NSE).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json", help="the plan from trader-exit")
        p.add_argument("--symbol", help="or give the plan as flags: NSE symbol")
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (default today, IST)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help='the entry bar\'s close, e.g. "10:00"')
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=15)
        p.add_argument("--stop-rule", default="auto",
                       choices=["auto", "or_mid", "or_low", "or_high", "pct", "supertrend"])
        p.add_argument("--tick", type=float, help="the symbol's tick in INR (default: the plan's, else the price band)")
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this time (default 15:30, the close)")
        p.add_argument("--offline", metavar="FILE", help="a saved bundle (.json or .json.gz), no network")
        p.add_argument("--now", help='pretend it is this time ("2026-11-03 10:00" IST, or ISO with Z)')

    p = sub.add_parser("run", help="replay the plan: 10,000 simulated sessions plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="save the result (default trader/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="draw a saved result instead of running again")
    p = sub.add_parser("universe", help="the evening replay: the rule on every chart of a saved day (scan.py orb --save)")
    p.add_argument("bundle", help="a scanner bundle of 5-minute bars for the day")
    p.add_argument("--date", help="the day inside the bundle (default: the day with the most charts)")
    p.add_argument("--config", default="trader/config.json", help="budget, max_trades_per_day, breadth_gate_pct, gate_time")
    p.add_argument("--strategy", default="orb", choices=sorted(STRATEGIES),
                   help="the entry rule to replay (default orb); see STRATEGIES in this file")
    p.add_argument("--vwap-pct", type=float, help="vwap: how far under VWAP a close must be, percent (default 0.4)")
    p.add_argument("--target", help="1R, 1.5R, 2R (default 2R)")
    p.add_argument("--gate", type=float, help="breadth gate percent at gate_time; 0 = no gate (default: config, 65)")
    p.add_argument("--cutoff", help="no entries after HH:MM (default 11:00; none = no cutoff)")
    p.add_argument("--budget", type=float, help="rupees per day (default: config budget, 10,000)")
    p.add_argument("--max-trades", type=int, help="ledger trades a day (default: config max_trades_per_day, 2)")
    p.add_argument("--no-stop-after-loss", action="store_true", help="keep trading after a losing trade")
    p.add_argument("--min-score", type=int, help="only charts whose daily score card is at least this (needs --daily)")
    p.add_argument("--daily", help="a saved daily bundle, for --min-score")
    p.add_argument("--master", help="a saved instruments CSV for tick sizes (default: the price band)")
    p.add_argument("--compare", action="store_true", help="also show the baseline: 1R, no gate, no cutoff")
    p.add_argument("--others", action="store_true", help="also score the other strategies with the same rule")
    p.add_argument("--keep-every", action="store_true", help="keep every breakout's row in the written file")
    p.add_argument("--write", nargs="?", const=True, metavar="PATH", help="save (default trader/replays/DATE.json)")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart, "universe": cmd_universe}[a.cmd](a)
    except TMError as e:
        print("timemachine.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

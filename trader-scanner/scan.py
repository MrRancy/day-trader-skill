#!/usr/bin/env python3
"""Chart Scanner for trader-scanner (NSE): scan thousands of charts for setups. Standard library only.

    python3 scan.py universe                              # INDstocks master + NSE ASM/GSM lists -> trader/universe.txt
    python3 scan.py daily                                 # before the open: daily setups -> trader/watchlist.json
    python3 scan.py daily --symbols RELIANCE,TCS,INFY     # or --watchlist FILE, or --limit 200
    python3 scan.py orb                                   # after 09:35 IST: opening-range breakouts so far
    python3 scan.py orb --at 10:00 --or-minutes 15        # cut the clock at 10:00 IST
    python3 scan.py orb --offline orb5m-2026-11-03.json.gz --now "2026-11-03 10:00"

universe  Reads the INDstocks equity instrument master and keeps an NSE cash line when, in order:
            1. it is an NSE row (the BSE line of the same share is out)
            2. its series is EQ: BE and BZ are trade-for-trade (no intraday), SM and ST are SME,
               the rest are debt, ETFs, mutual funds, REITs and such
            3. its ISIN is a share's (INE..., or IN9... for a DVR share): the EQ series also carries
               ETFs and fund units (INF...) and NSE's test symbols (DUMMY...)
            4. it is not on NSE's GSM list, nor on the ASM list (long or short term): surveillance
               names trade with 100 % margin, price bands and sometimes trade-for-trade
          Writes trader/universe.txt (SYMBOL|Name) and trader/surveillance.json (the lists, dated).

daily     Pre-market scan of daily bars (INDstocks, 5 symbols a request, 100 calendar days back).
          Today's bar is left out until 15:30. A chart whose last bar is locked at a circuit band
          (high == low, or the close at today's band) is counted and skipped: no fill is possible.
          Setups on closes, the same rules as the US original: breakout_20d, bull_flag,
          reclaim_ma20, oversold (RSI 14 under 30), oversold_bounce.

orb       Opening-range breakout on 5-minute bars. The range is the first --or-minutes (15) of the
          session: the 09:15, 09:20 and 09:25 bars. A hit is the first completed bar to CLOSE above
          the range high, at that bar's close time. INDstocks bars carry real highs and lows, so there
          is one stage, 5 charts a request. A bar that closed at or above the upper circuit band is
          not a hit: it is locked, and reported as such.

Times are IST (Asia/Kolkata, UTC+5:30, no DST). The session is 09:15 to 15:30. Data comes through
../trader_data.py (INDstocks, INDSTOCKS_TOKEN); the NSE holiday list is embedded (2026, read from
nseindia.com on 2026-11-04) and the ASM/GSM lists are read live from nseindia.com's JSON.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trader_data  # noqa: E402

UA_NSE = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
NSE_API = "https://www.nseindia.com/api/"
NSE_LISTS = (("asm", "reportASM"), ("gsm", "reportGSM"))
PER_CALL = trader_data.CANDLES_PER_CALL   # INDstocks: 5 scrip codes a historical request
SLEEP = time.sleep                        # tests swap this out
BAR_MIN = 5
MIN_DAILY_BARS = 30
DAILY_LOOKBACK_DAYS = 100                 # calendar days: about 65 sessions, enough for 30 bars and RSI
DEFAULT_UNIVERSE = "trader/universe.txt"
DEFAULT_CONFIG = "trader/config.json"
DEFAULT_UNTIL = "15:00"                   # no_entries_after: MIS positions are squared off from 15:20
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

# the NSE calendar lives in trader_data, one copy for every skill
NSE_HOLIDAYS = trader_data.NSE_HOLIDAYS
MUHURAT = trader_data.MUHURAT
CALENDAR_YEARS = trader_data.CALENDAR_YEARS
OPEN = trader_data.OPEN
CLOSE = trader_data.CLOSE
trading_day = trader_data.trading_day
session = trader_data.session

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
    ("ema_cross", "EMA 10 crossed over EMA 20"),
    ("supertrend_flip", "Supertrend turned up"),
    ("volume_spike", "volume over 2x its 20-day average"),
    ("bb_squeeze", "Bollinger squeeze (width at a 20-bar low)"),
    ("bb_release", "Bollinger squeeze released"),
]
EMA_CROSS_WITHIN = 3          # bars: the cross counts for three sessions, as the indicator-skills scanner does
SUPERTREND = (10, 3.0)
VOLUME_SPIKE_X = 2.0
BB = (20, 2.0)
BB_LOW_BARS = 20
SCORE_BARS = 51               # EMA50 needs 50 closes and one more to compare
LABEL = dict(SETUPS)
KEEP_SERIES = {"EQ"}
T2T_SERIES = {"BE", "BZ"}
SME_SERIES = {"SM", "ST"}


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- time

ist_of = trader_data.ist_of


def parse_now(text):
    """-> naive IST. "2026-11-03 10:00" is IST; a time with Z or an offset is converted."""
    if not text:
        return trader_data.now_ist().replace(microsecond=0)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ScanError('--now wants a time like "2026-11-03 10:00" (IST) or ...Z (UTC)')
    if dt.tzinfo is None:
        return dt
    return (dt - dt.utcoffset()).replace(tzinfo=None) + trader_data.IST


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants an IST time like 10:00" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def clock_label(t):
    """datetime or time -> '10:00' (24-hour, as NSE writes its times)."""
    return "%02d:%02d" % (t.hour, t.minute)


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


# ---------------------------------------------------------------- NSE lists

def nse_get(name, timeout=20):
    """One of nseindia.com's JSON reports. The site wants a browser's headers."""
    url = NSE_API + name
    req = urllib.request.Request(url, headers={"User-Agent": UA_NSE, "Accept": "application/json, text/plain, */*",
                                               "Accept-Language": "en-US,en;q=0.9",
                                               "Referer": "https://www.nseindia.com/"})
    try:
        with trader_data.OPENER.open(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        e.close()
        raise ScanError("nseindia.com answered HTTP %d for %s" % (e.code, name))
    except (urllib.error.URLError, OSError) as e:
        raise ScanError("could not reach nseindia.com for %s: %s" % (name, getattr(e, "reason", e)))
    try:
        return json.loads(text)
    except ValueError:
        raise ScanError("nseindia.com did not answer JSON for %s (a block page?)" % name)


def parse_surveillance(asm_doc, gsm_doc):
    """NSE's reportASM and reportGSM -> {"asm": {SYM: what}, "gsm": {SYM: what}}."""
    out = {"asm": {}, "gsm": {}}
    if isinstance(asm_doc, dict):
        for term in ("longterm", "shortterm"):
            for r in ((asm_doc.get(term) or {}).get("data") or []):
                sym = str(r.get("symbol") or "").strip().upper()
                if sym:
                    stage = str(r.get("asmSurvIndicator") or "").strip()
                    out["asm"][sym] = ("%s %s" % ("LT-ASM" if term == "longterm" else "ST-ASM", stage)).strip()
    elif asm_doc is not None:
        raise ScanError("NSE's ASM report is not the shape this tool knows")
    if isinstance(gsm_doc, list):
        for r in gsm_doc:
            sym = str(r.get("symbol") or "").strip().upper()
            if sym:
                out["gsm"][sym] = "GSM stage %s" % (r.get("gsmStage") or "?")
    elif gsm_doc is not None:
        raise ScanError("NSE's GSM report is not the shape this tool knows")
    return out


def holidays_from_report(doc):
    """nseindia.com's holiday-master (type=trading) -> {date: description} for the capital market."""
    out = {}
    for r in (doc or {}).get("CM") or []:
        d = datetime.datetime.strptime(r["tradingDate"], "%d-%b-%Y").date()
        out[d.isoformat()] = r["description"].replace("*", "").strip()
    return out


# ---------------------------------------------------------------- universe

def classify(row, surveillance=None):
    """One instrument-master row -> 'keep' or the rule that drops it."""
    if (row.get("EXCH") or "").upper() != "NSE":
        return "not NSE"
    if (row.get("INSTRUMENT_NAME") or "").upper() != "EQUITY":
        return "not equity"
    series = (row.get("SERIES") or "").upper()
    if series not in KEEP_SERIES:
        if series in T2T_SERIES:
            return "trade-for-trade (BE/BZ)"
        if series in SME_SERIES:
            return "SME (SM/ST)"
        return "debt, ETF or other series"
    isin = (row.get("ISIN") or "").strip().upper()
    if not isin.startswith("IN") or isin.startswith("INF"):
        return "not equity shares"   # INF is a fund unit (ETF, MF), DUMMY... an NSE test symbol
    sym = (row.get("TRADING_SYMBOL") or "").strip().upper()
    s = surveillance or {}
    if sym in s.get("gsm", {}):
        return "GSM"
    if sym in s.get("asm", {}):
        return "ASM"
    return "keep"


def build_universe(rows, surveillance=None):
    """master rows -> (kept [(SYM, name)], counts)."""
    kept, counts = {}, {"rows": len(rows)}
    for r in rows:
        why = classify(r, surveillance)
        counts[why] = counts.get(why, 0) + 1
        if why == "keep":
            sym = (r.get("TRADING_SYMBOL") or "").strip().upper()
            kept[sym] = " ".join((r.get("SYMBOL_NAME") or r.get("CUSTOM_SYMBOL") or "").split())
    return sorted(kept.items()), counts


def band_table(rows):
    """master rows -> {SYM: (lower, upper)}: today's circuit band of each NSE EQ line."""
    out = {}
    for r in rows:
        if (r.get("EXCH") or "").upper() != "NSE" or (r.get("SERIES") or "").upper() != "EQ":
            continue
        try:
            lo, hi = float(r.get("LOWER_LIMIT") or 0), float(r.get("UPPER_LIMIT") or 0)
        except ValueError:
            continue
        if 0 < lo < hi:
            out[(r.get("TRADING_SYMBOL") or "").strip().upper()] = (lo, hi)
    return out


def at_band(bar, band):
    """A bar pinned at a circuit limit: locked all bar (high == low), or closed at the band."""
    if bar["h"] == bar["l"]:
        return True
    if band and (bar["c"] >= band[1] or bar["c"] <= band[0]):
        return True
    return False


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes (the same math as the US original)."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_bars(bars, now):
    """COMPLETE daily bars, oldest first: today's bar dropped until the session closes, anything
    dated after the clock dropped, one bar a day."""
    today = now.date()
    s = session(today)
    today_done = s is None or now.time() >= s[1]
    out = {}
    for b in bars:
        d = ist_of(b["ts"]).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = b
    return [(d, out[d]) for d in sorted(out)]


def find_setups(closes, highs=None, lows=None, volumes=None):
    """closes (oldest first, >= 30) -> (setup keys, numbers). highs, lows and volumes unlock the
    indicator setups (ema_cross, supertrend_flip, volume_spike, bb_squeeze, bb_release) and the
    score card; without them only the price setups are checked."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    nums = {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
            "run_pct": run * 100, "range5_pct": rng5 * 100, "score": None}
    if highs is None or lows is None or volumes is None:
        return tags, nums
    tags += indicator_setups(highs, lows, c, volumes, nums)
    nums["score"] = trader_data.score_card(highs, lows, c) if len(c) >= SCORE_BARS else None
    return tags, nums


def indicator_setups(highs, lows, closes, volumes, nums=None):
    """The indicator rules on daily bars (oldest first): ema_cross, supertrend_flip, volume_spike,
    bb_squeeze, bb_release. Each needs enough bars for its own lookback, else it is simply not tagged."""
    nums = nums if nums is not None else {}
    tags = []
    e10, e20 = trader_data.ema(closes, 10), trader_data.ema(closes, 20)
    if trader_data.crossed_above(e10, e20, EMA_CROSS_WITHIN):
        tags.append("ema_cross")
    _, direction = trader_data.supertrend(highs, lows, closes, *SUPERTREND)
    if len(direction) >= 2 and direction[-1] == 1 and direction[-2] == -1:
        tags.append("supertrend_flip")
    if len(volumes) >= 21:
        avg = sum(volumes[-21:-1]) / 20.0
        nums["volume_x"] = volumes[-1] / avg if avg else None
        if avg and volumes[-1] > VOLUME_SPIKE_X * avg:
            tags.append("volume_spike")
    width = trader_data.bbands(closes, *BB)["width"]
    w = [x for x in width if x is not None]
    if len(w) >= BB_LOW_BARS + 1:
        nums["bb_width"] = w[-1]
        if w[-1] <= min(w[-BB_LOW_BARS:]):
            tags.append("bb_squeeze")
        elif w[-2] <= min(w[-BB_LOW_BARS - 1:-1]):
            tags.append("bb_release")
    nums["supertrend"] = direction[-1] if direction and direction[-1] is not None else None
    return tags


def scan_daily(data, symbols, now, min_price, min_volume, bands=None):
    """Pure. data = {SYM: [bars]}. Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "circuit": 0, "scanned": 0}
    today = now.date()
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_bars(s, now)
        if not bars:
            counts["short"] += 1
            continue
        d_last, last = bars[-1]
        price, vol = last["c"], last["v"]
        if price < min_price or (min_volume > 0 and vol < min_volume):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        if at_band(last, (bands or {}).get(sym) if d_last == today else None):
            counts["circuit"] += 1
            continue
        counts["scanned"] += 1
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [b["c"] for _, b in bars]
        tags, nums = find_setups(closes, [b["h"] for _, b in bars], [b["l"] for _, b in bars],
                                 [b["v"] for _, b in bars])
        if tags:
            sc = nums.get("score")
            hits.append({"symbol": sym, "setups": tags, "price": round(price, 2), "close_date": d_last.isoformat(),
                         "rsi": round(nums["rsi"], 1), "volume": int(vol),
                         "score": sc["score"] if sc else None, "score_parts": sc["parts"] if sc else None,
                         "ema20": sc["ema20"] if sc else None, "ema50": sc["ema50"] if sc else None})
    hits.sort(key=lambda h: (-(h["price"] * h["volume"]), h["symbol"]))  # most rupees traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k):
    return datetime.datetime.combine(day, OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(bars, day, cutoff):
    """5-minute bars of `day` that are complete at `cutoff` (naive IST), on the 09:15 grid.
    Sorted, one per slot."""
    s = session(day)
    close_t = s[1] if s else CLOSE
    out = {}
    for b in bars:
        t = ist_of(b["ts"])
        if t.date() != day or t.second or (t - slot(day, 0)).total_seconds() % (BAR_MIN * 60):
            continue  # another day, or off the grid
        if t.time() < OPEN or t.time() >= close_t:
            continue  # pre-open, post-close
        if t + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        out[t] = {"start": t, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range):
    """Every slot from 09:15 to the cutoff is there, and at least the range."""
    want = int((cutoff - slot(day, 0)).total_seconds() // (BAR_MIN * 60))
    s = session(day)
    if s:
        want = min(want, int((datetime.datetime.combine(day, s[1]) - slot(day, 0)).total_seconds()
                             // (BAR_MIN * 60)))
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k) for k, b in enumerate(bars))


def orb_hit(bars, n_range):
    """Real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": clock_label(closes_at),
                    "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at, "_bar": b}
    return None


def orb_window(now, at, or_minutes, until, explicit_day=None):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    if day.isoformat() in MUHURAT:
        raise ScanError("%s is %s. This scanner does not run on the Muhurat session."
                        % (day_label(day), MUHURAT[day.isoformat()]))
    ok, why = trading_day(day)
    if not ok:
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    s = session(day)
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, s[1])
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (clock_label(at), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (clock_label(at), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s IST on %s. Replay the session with --at 10:00."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s IST: the %d-minute opening range runs 09:15 to %s and the first bar after it "
                        "closes at %s. Run orb again after %s."
                        % (clock_label(cutoff), or_minutes, clock_label(range_end), clock_label(first_close),
                           clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:00." % (clock_label(cutoff), clock_label(until), clock_label(first_close)))
    return day, cutoff


def scan_orb(data, symbols, day, cutoff, or_minutes, min_price=0.0, min_volume=0, bands=None):
    """data = {SYM: [bars]} with real highs and lows. Returns (hits, counts)."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "circuit": 0, "scanned": 0}
    hits, locked = [], []
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff)
        if not usable(bars, day, cutoff, n_range):
            counts["unusable"] += 1
            continue
        vol = sum(b["v"] for b in bars)
        if bars[-1]["c"] < min_price or vol < min_volume:
            counts["illiquid"] += 1
            continue
        band = (bands or {}).get(sym)
        if all(at_band(b, band) for b in bars[:n_range]):
            counts["circuit"] += 1   # locked at a band through the whole range
            continue
        counts["scanned"] += 1
        h = orb_hit(bars, n_range)
        if h:
            bar = h.pop("_bar")
            if at_band(bar, band):
                counts["circuit"] += 1
                locked.append(sym)
                continue
            hits.append(dict({"symbol": sym, "setups": ["orb"]}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    counts["locked"] = sorted(locked)
    return hits, counts


# ---------------------------------------------------------------- fetching and files

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_bars(symbols, interval, start, end, a=None, backend=None):
    """INDstocks bars, 5 symbols a request. -> ({SYM: [bars]}, {SYM: error})."""
    b = backend or trader_data.backend()
    data, failed = {}, {}
    known = []
    for s in symbols:
        try:
            b.resolve(s)
            known.append(s)
        except trader_data.DataError as e:
            failed[s] = str(e)
    batches = [known[i:i + PER_CALL] for i in range(0, len(known), PER_CALL)]
    for k, batch in enumerate(batches):
        try:
            got = b.candles_many(batch, interval, start, end)
        except trader_data.DataError as e:
            msg = str(e)
            if "token" in msg.lower():
                raise ScanError(msg)   # a dead token fails every request: stop, do not report a partial scan
            for s in batch:
                failed[s] = msg
            continue
        for s in batch:
            if got.get(s):
                data[s] = got[s]
            else:
                failed[s] = "no bars from INDstocks"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def bundle(data, failed, interval, start, end, now):
    return {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
            "source": "indstocks", "interval": interval, "start": str(start), "end": str(end),
            "bars": data, "failed": failed}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write_text(path, text):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    if path.endswith(".gz"):
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(text)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


def _absorb(doc, name, data, failed):
    if not isinstance(doc, dict) or not isinstance(doc.get("bars"), dict):
        raise ScanError("offline file %s is not a trader-scanner bundle (no \"bars\")" % name)
    for sym, bars in doc["bars"].items():
        if isinstance(bars, list):
            data[sym.upper()] = bars
    for sym, why in (doc.get("failed") or {}).items():
        failed[sym.upper()] = str(why)


def load_offline(path):
    """A bundle file (--save), or a folder of them -> ({SYM: [bars]}, {SYM: error}, interval)."""
    data, failed, interval = {}, {}, None
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        paths = [os.path.join(path, f) for f in names]
    elif os.path.isfile(path):
        paths = [path]
    else:
        raise ScanError("no offline file or folder at %s" % path)
    for p in paths:
        try:
            doc = json.loads(read_text(p))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (p, e))
        _absorb(doc, os.path.basename(p), data, failed)
        interval = interval or doc.get("interval")
    return data, failed, interval


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    write_text(path, json.dumps(doc, indent=2) + "\n")


def money(x):
    return "{:,.2f}".format(x)


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def _p(x):
    return "{:,.2f}".format(x)


def master_rows(a, backend=None):
    """The equity instrument master: --master FILE, else downloaded through trader_data."""
    if getattr(a, "master", None):
        return trader_data.parse_instruments(read_text(a.master))
    b = backend or trader_data.backend()
    try:
        return b.instruments("equity")
    except trader_data.DataError as e:
        raise ScanError(str(e))


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    t0 = time.time()
    now = parse_now(a.now)
    rows = master_rows(a)
    surv, surv_note = {"asm": {}, "gsm": {}}, None
    if a.no_surveillance:
        surv_note = "surveillance lists skipped (--no-surveillance)"
    elif a.offline:
        docs = {}
        for key, name in NSE_LISTS:
            p = os.path.join(a.offline, key + ".json")
            if not os.path.exists(p):
                raise ScanError("no %s.json in %s" % (key, a.offline))
            docs[key] = json.loads(read_text(p))
        surv = parse_surveillance(docs["asm"], docs["gsm"])
    else:
        docs = {}
        try:
            for k, (key, name) in enumerate(NSE_LISTS):
                if k and a.pause:
                    SLEEP(a.pause)
                docs[key] = nse_get(name)
            surv = parse_surveillance(docs["asm"], docs["gsm"])
        except ScanError as e:
            surv_note = "surveillance lists unavailable, nothing excluded: %s" % e
    kept, counts = build_universe(rows, surv)
    if not a.no_write:
        write_text(a.out, "".join("%s|%s\n" % (sym, name) for sym, name in kept))
        write_json(os.path.join(os.path.dirname(a.out) or ".", "surveillance.json"),
                   {"created_by": "trader-scanner", "as_of": now.strftime("%Y-%m-%d %H:%M IST"),
                    "source": "nseindia.com reportASM and reportGSM" if not surv_note else None,
                    "note": surv_note, "asm": surv["asm"], "gsm": surv["gsm"]})
    dropped = dict((k, v) for k, v in counts.items() if k not in ("keep", "rows"))
    if a.json:
        print(json.dumps({"count": len(kept), "rows": counts.get("rows", 0), "dropped": dropped,
                          "asm": len(surv["asm"]), "gsm": len(surv["gsm"]), "note": surv_note,
                          "out": None if a.no_write else a.out, "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("INDstocks equity master: %s rows (NSE and BSE)." % "{:,}".format(counts.get("rows", 0)))
        print("Kept %s NSE EQ-series equity shares." % "{:,}".format(len(kept)))
        print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k)
                                      for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])) + ".")
        if surv_note:
            print("Note: " + surv_note)
        else:
            print("NSE surveillance lists: %d ASM, %d GSM symbols excluded where they were EQ-series shares."
                  % (len(surv["asm"]), len(surv["gsm"])))
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name a line) and surveillance.json next to it." % a.out)
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    offline_syms = None
    bands = {}
    start = now.date() - datetime.timedelta(days=DAILY_LOOKBACK_DAYS)
    end = now.date() + datetime.timedelta(days=1)
    if a.offline:
        data, failed, _ = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "Fetching daily bars for %s symbols (%d INDstocks requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + PER_CALL - 1) // PER_CALL))
        data, failed = fetch_bars(symbols, "1d", start, end, a)
        bands = band_table(master_rows(a))
        if a.save:
            write_text(a.save, json.dumps(bundle(data, failed, "1day", start, end, now)))
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_volume, bands)
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%S+05:30"), "mode": "daily",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "circuit": counts["circuit"], "source": "indstocks",
           "filters": {"min_price_inr": a.min_price, "min_volume": a.min_volume}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Daily scan, %s IST, closes through %s (INDstocks daily bars)." % (
        now.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (INR %s+ and %s+ shares)%s%s; "
          "%s scanned." % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
                           (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
                           "{:,}".format(counts["fetched"] - counts["illiquid"]), _n(a.min_price),
                           "{:,}".format(a.min_volume),
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           (", %d locked at a circuit band" % counts["circuit"]) if counts["circuit"] else "",
                           "{:,}".format(counts["scanned"])))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (price x volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-12s %10s  RSI %5.1f  score %s  %s" % (
            h["symbol"], money(h["price"]), h["rsi"], "%d/6" % h["score"] if h.get("score") is not None else "n/a",
            ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print("Score: of 6 daily conditions (close > EMA20, EMA20 > EMA50, RSI 50-70, MACD > signal, ADX > 20); "
          "a state, not a signal.")
    print(NOTE + " Next: pick one chart, then /trader-news SYMBOL.")
    return 0 if hits else 1


# ---------------------------------------------------------------- the daily trend behind an intraday hit

def trend_flags(daily, hits, day):
    """Adds "trend" (above_ema50 / below_ema50 / None) and "ema50" to each orb hit from the daily bars
    dated BEFORE the scan day (so the day being traded never leaks in). Shown, never used to filter."""
    for h in hits:
        closes = [b["c"] for b in sorted(daily.get(h["symbol"]) or [], key=lambda b: b["ts"])
                  if ist_of(b["ts"]).date() < day]
        e = trader_data.ema(closes, 50)[-1] if len(closes) >= 50 else None
        h["ema50"] = round(e, 2) if e is not None else None
        h["trend"] = None if e is None else ("above_ema50" if closes[-1] > e else "below_ema50")
    return hits


TREND_LABEL = {"above_ema50": "above EMA50", "below_ema50": "below EMA50", None: "no daily history"}


# ---------------------------------------------------------------- breadth: the 10:00 gate

GATE_PCT = 65.0
GATE_TIME = "10:00"
BREADTH_STATE = "trader/state/breadth.json"


def breadth_from_quotes(quotes, min_price=50.0):
    """Share of charts trading above their open right now, from a {SYM: quote} dict (ltp, open, volume)."""
    counted = above = 0
    for sym, q in quotes.items():
        o, ltp, vol = q.get("open"), q.get("ltp"), q.get("volume")
        if not o or not ltp or o < min_price or not vol:
            continue
        counted += 1
        above += ltp > o
    return {"counted": counted, "above": above, "pct": round(100.0 * above / counted, 1) if counted else None}


def breadth_from_bundle(data, day, cutoff, min_price=50.0):
    """The same reading from saved 5-minute bars: the last bar closed by `cutoff` against the 09:15 open."""
    counted = above = 0
    for sym, s in data.items():
        bars = session_bars(s, day, cutoff)
        if not bars or bars[0]["o"] < min_price:
            continue
        counted += 1
        above += bars[-1]["c"] > bars[0]["o"]
    return {"counted": counted, "above": above, "pct": round(100.0 * above / counted, 1) if counted else None}


def gate_verdict(reading, now, gate_pct, gate_time):
    """{"open": bool, "reason": str}. Before the gate time the gate is closed: the reading is too early."""
    t = now.strftime("%H:%M")
    if reading["pct"] is None:
        return {"open": False, "reason": "no charts to count"}
    if t < gate_time:
        return {"open": False, "reason": "read at %s, before the gate time %s: too early to decide" % (t, gate_time)}
    if reading["pct"] >= gate_pct:
        return {"open": True, "reason": "%.1f%% of %s charts above their open at %s, at or over the %s%% gate"
                % (reading["pct"], "{:,}".format(reading["counted"]), t, _n(gate_pct))}
    return {"open": False, "reason": "%.1f%% of %s charts above their open at %s, under the %s%% gate"
            % (reading["pct"], "{:,}".format(reading["counted"]), t, _n(gate_pct))}


def cmd_breadth(a):
    now = parse_now(a.now)
    cfg = read_config(a.config)
    gate_pct = float(a.gate if a.gate is not None else cfg.get("breadth_gate_pct", GATE_PCT))
    gate_time = parse_hhmm(a.gate_time or cfg.get("gate_time") or GATE_TIME, "--gate-time").strftime("%H:%M")
    day = now.date()
    s = session(day)
    if s is None and not a.offline:
        raise ScanError("NSE is closed on %s (%s): nothing to read" % (day_label(day), trading_day(day)[1] or "no session"))
    if a.offline:
        data, failed, _ = load_offline(a.offline)
        if not a.now:
            raise ScanError("offline: give --now \"YYYY-MM-DD HH:MM\" for the reading time")
        reading = breadth_from_bundle(data, day, now, a.min_price)
        source = "bundle %s" % os.path.basename(a.offline.rstrip("/"))
    else:
        symbols = read_symbols(a)
        progress(a, "Quoting %s symbols (%d INDstocks requests)..." % ("{:,}".format(len(symbols)),
                                                                        (len(symbols) + trader_data.QUOTES_PER_CALL - 1)
                                                                        // trader_data.QUOTES_PER_CALL))
        try:
            quotes = trader_data.backend().quote(symbols)
        except trader_data.DataError as e:
            raise ScanError(str(e))
        reading = breadth_from_quotes(quotes, a.min_price)
        source = "indstocks quotes"
    verdict = gate_verdict(reading, now, gate_pct, gate_time)
    doc = {"created_by": "trader-scanner", "date": day.isoformat(), "time": now.strftime("%H:%M"),
           "counted": reading["counted"], "above": reading["above"], "pct": reading["pct"],
           "gate_pct": gate_pct, "gate_time": gate_time, "open": verdict["open"], "reason": verdict["reason"],
           "source": source, "note": "A share of charts above their open, not a forecast."}
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0 if verdict["open"] else 1
    print("Breadth %s %s IST: %s of %s charts above their 09:15 open (%s)."
          % (day_label(day), now.strftime("%H:%M"), "{:,}".format(reading["above"]), "{:,}".format(reading["counted"]),
             ("%.1f%%" % reading["pct"]) if reading["pct"] is not None else "n/a"))
    print("GATE %s: %s." % ("OPEN" if verdict["open"] else "CLOSED", verdict["reason"]))
    if verdict["open"]:
        print("Breakouts from %s on may be taken today; trader-risk reads this file. Re-run before each entry: "
              "breadth moves." % gate_time)
    else:
        print("No new entries while the gate is closed (trader-risk blocks them). Re-run at %s%s."
              % (gate_time, " or later" if now.strftime("%H:%M") >= gate_time else ""))
    if not a.no_write:
        print("Wrote %s." % a.out)
    return 0 if verdict["open"] else 1


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or DEFAULT_UNTIL, "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    bands = {}
    if a.offline:
        data, failed, _ = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ist_of(s[-1]["ts"]).date() for s in data.values() if s]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day)
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "Fetching 5-minute bars for %s symbols (%d INDstocks requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + PER_CALL - 1) // PER_CALL))
        data, failed = fetch_bars(symbols, "5m", day, day + datetime.timedelta(days=1), a)
        bands = band_table(master_rows(a))
        if a.save:
            write_text(a.save, json.dumps(bundle(data, failed, "5minute", day, day + datetime.timedelta(days=1), now)))
    hits, counts = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_volume, bands)
    nfailed = len([s for s in symbols if s in (failed or {})])
    daily = None
    if hits and a.daily:
        daily, _, _ = load_offline(a.daily)
    elif hits and not a.offline and not a.no_trend:
        progress(a, "Fetching daily bars for the %d hits (%d INDstocks requests)..."
                 % (len(hits), (len(hits) + PER_CALL - 1) // PER_CALL))
        daily, _ = fetch_bars([h["symbol"] for h in hits], "1d", day - datetime.timedelta(days=DAILY_LOOKBACK_DAYS),
                              day, a)
    if daily is not None:
        trend_flags(daily, hits, day)
    as_of = "%s %s IST" % (day.isoformat(), cutoff.strftime("%H:%M"))
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%S+05:30"), "mode": "orb",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "circuit": counts["circuit"], "locked": counts["locked"], "source": "indstocks",
           "or_minutes": a.or_minutes, "interval": "%dm" % BAR_MIN,
           "filters": {"min_price_inr": a.min_price, "min_volume": a.min_volume}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s IST: %d-minute range (09:15 to %s), 5-minute bars, INDstocks."
          % (day_label(day), clock_label(cutoff), a.or_minutes, clock_label(slot(day, a.or_minutes // BAR_MIN))))
    print("Fetched %s of %s charts%s; %s usable%s%s."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             (" (%s under the price / volume floor)" % "{:,}".format(counts["illiquid"])) if counts["illiquid"] else "",
             ("; %s locked at a circuit band, not tradable: %s" % (counts["circuit"], ", ".join(counts["locked"]) or "range locked"))
             if counts["circuit"] else ""))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), "{:,}".format(counts["scanned"]), clock_label(cutoff)))
        for h in hits[:a.show]:
            print("  %5s  %-12s %10s  range %s to %s  %+.2f%% over%s" % (
                h["at"], h["symbol"], money(h["price"]), _p(h["or_low"]), _p(h["or_high"]), h["pct_over"],
                ("  daily %s" % TREND_LABEL[h["trend"]]) if "trend" in h else ""))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
        if daily is not None:
            above = sum(1 for h in hits if h.get("trend") == "above_ema50")
            print("Daily trend: %d of %d hits closed above their 50-day EMA the day before (a flag, not a filter)."
                  % (above, len(hits)))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news SYMBOL and /trader-entry SYMBOL.")
    return 0 if hits else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (trader-scanner, NSE): daily setups and opening-range breakouts.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="INDstocks master + NSE surveillance lists -> trader/universe.txt")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--master", metavar="FILE", help="a saved instruments CSV (.gz ok) instead of downloading")
    p.add_argument("--offline", metavar="DIR", help="a folder with asm.json and gsm.json instead of nseindia.com")
    p.add_argument("--no-surveillance", action="store_true", help="skip the ASM/GSM lists")
    p.add_argument("--now", help='pretend it is this time ("2026-11-04 18:00" IST)')
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--pause", type=float, default=1.0, help="seconds between nseindia.com requests")
    p.add_argument("--json", action="store_true")

    def common(p, min_price, min_volume):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated NSE symbols instead of the universe")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of symbols")
        p.add_argument("--universe", help="symbol list, one SYMBOL|Name a line (default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price, help="INR (%s)" % _n(min_price))
        p.add_argument("--min-volume", type=int, default=min_volume, help="shares (%s)" % "{:,}".format(min_volume))
        p.add_argument("--offline", metavar="PATH", help="a saved bundle (--save) or folder instead of the network")
        p.add_argument("--save", metavar="PATH", help="save the fetched bars as a bundle (.json or .json.gz)")
        p.add_argument("--master", metavar="FILE", help="a saved instruments CSV for the circuit bands")
        p.add_argument("--now", help='pretend it is this time ("2026-11-03 10:00" IST, or ...Z)')
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="pre-market setups from daily bars")
    common(p, 50.0, 500000)
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, 50.0, 0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--at", help="cut the clock at this IST time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, %s)" % DEFAULT_UNTIL)
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--daily", metavar="PATH", help="a saved daily bundle for the trend flag (offline runs)")
    p.add_argument("--no-trend", action="store_true", help="skip the daily-bar fetch for the trend flag")

    p = sub.add_parser("breadth", help="the 10:00 gate: the share of charts above their open, from live quotes")
    common(p, 50.0, 0)
    p.add_argument("--gate", type=float, help="gate percent (default: config breadth_gate_pct, %s)" % _n(GATE_PCT))
    p.add_argument("--gate-time", help="HH:MM the gate can first open (default: config gate_time, %s)" % GATE_TIME)
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.set_defaults(out=BREADTH_STATE)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb, "breadth": cmd_breadth}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

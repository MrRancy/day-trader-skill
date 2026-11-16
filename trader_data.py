#!/usr/bin/env python3
"""trader_data: the one place every skill reads Indian market data from. Standard library only.

Backends: indstocks (default) | kite (not built yet) | yahoo (daily closes only, not built yet).
Every skill reads bars through this module, never a vendor endpoint directly.

    python3 trader_data.py whoami                                  # is INDSTOCKS_TOKEN still good?
    python3 trader_data.py instruments [--source equity] [--out FILE]
    python3 trader_data.py candles RELIANCE 5m 2026-11-03 [2026-11-04] [--show N] [--out FILE] [--json]
    python3 trader_data.py quote RELIANCE,TCS [--json]

Symbols are NSE trading symbols (RELIANCE). "BSE:RELIANCE" picks the BSE line, "NSE_2885" is
an INDstocks scrip code used as is. Times are IST (Asia/Kolkata, UTC+5:30, no DST), naive.
Bars are dicts {"ts": epoch seconds of the bar OPEN, "o", "h", "l", "c", "v"}; a 5m bar
stamped 09:15 covers 09:15:00 to 09:19:59 and closes at 09:20. Intraday bars are anchored to
the 09:15 session open (09:15, 09:20, ...), not to wall-clock :00/:05.

The token comes from the INDSTOCKS_TOKEN environment variable, or from a .env file next to
this module. It expires 24 hours after generation. It is never written to disk or printed.

Skills import it with:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import trader_data
"""

import argparse
import csv
import datetime
import gzip
import http.client
import io
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (compatible; day-trader-india/0.1; +https://github.com/Jakeschincariol/day-trader-skill)"
EPOCH = datetime.datetime(1970, 1, 1)
IST = datetime.timedelta(hours=5, minutes=30)
TOKEN_VAR = "INDSTOCKS_TOKEN"
BACKEND_VAR = "TRADER_DATA_BACKEND"
PAUSE = 0.25        # seconds between requests: data and quote APIs allow 5 a second
BACKOFF = 5.0       # seconds before the one retry after a 429
THROTTLE_BACKOFF = 20.0   # seconds before retrying a 403 (20 s, then 40 s): a burst trips INDstocks' limit as a 403
SLEEP = time.sleep  # tests swap this out

# our short names -> INDstocks path values, and the widest window one call may ask for
INTERVALS = {
    "1m": "1minute", "2m": "2minute", "3m": "3minute", "5m": "5minute", "10m": "10minute",
    "15m": "15minute", "30m": "30minute", "1h": "60minute", "2h": "120minute", "4h": "240minute",
    "1d": "1day", "1w": "1week", "1mo": "1month",
}
MAX_DAYS = {"1minute": 7, "2minute": 7, "3minute": 7, "4minute": 7, "5minute": 7, "10minute": 7,
            "15minute": 7, "30minute": 7, "60minute": 15, "120minute": 15, "180minute": 15,
            "240minute": 15, "1day": 365, "1week": 365, "1month": 365}
SECONDS = {"1minute": 60, "2minute": 120, "3minute": 180, "4minute": 240, "5minute": 300,
           "10minute": 600, "15minute": 900, "30minute": 1800, "60minute": 3600,
           "120minute": 7200, "180minute": 10800, "240minute": 14400, "1day": 86400}
CANDLES_PER_CALL = 5     # scrip-codes per historical request
QUOTES_PER_CALL = 200    # scrip-codes per quote request (1,000 makes the GET URL too long: HTTP 414)
SERIES_RANK = {"EQ": 0, "BE": 1, "BZ": 2, "A": 0, "B": 1, "T": 2, "X": 3, "XT": 3}
QUOTE_FIELDS = (("ltp", "live_price"), ("open", "day_open"), ("high", "day_high"), ("low", "day_low"),
                ("prev_close", "prev_close"), ("volume", "volume"), ("upper_circuit", "upper_circuit"),
                ("lower_circuit", "lower_circuit"), ("change", "day_change"),
                ("change_pct", "day_change_percentage"), ("high_52w", "52week_high"), ("low_52w", "52week_low"))


class DataError(Exception):
    """A plain-English reason the data could not be read."""


# ---------------------------------------------------------------- the NSE calendar (shared by every skill)

OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
# NSE capital-market trading holidays, nseindia.com/api/holiday-master?type=trading, read 2026-11-04.
# 2027 is not published yet: weekdays in 2027 count as trading days until it is.
NSE_HOLIDAYS = {
    "2026-01-15": "Municipal Corporation Election - Maharashtra",
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr (Ramadan Eid)",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan",
    "2026-11-10": "Diwali-Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
# Muhurat trading: a one-hour special session on Diwali evening, timing notified by NSE each year.
MUHURAT = {"2026-11-08": "Diwali Laxmi Pujan: Muhurat trading, a one-hour evening session"}
CALENDAR_YEARS = (2026,)


def trading_day(d):
    """(is_trading_day, reason). Weekends and the NSE holiday list; a note for a year not published yet."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    if d.year not in CALENDAR_YEARS:
        return True, "no NSE holiday calendar for %d yet" % d.year
    return True, ""


def session(d):
    """(open_time, close_time) IST, or None if closed all day. NSE has no early closes."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, CLOSE


# NSE tick sizes by price band (the master's TICK_SIZE column, in paise, is the authority; this is the
# rule it follows, for when the master is not at hand): price < 250 -> 0.01, < 1,000 -> 0.05,
# < 5,000 -> 0.10, < 10,000 -> 0.50, < 20,000 -> 1.00, else 5.00.
TICK_BANDS = ((250, 0.01), (1000, 0.05), (5000, 0.10), (10000, 0.50), (20000, 1.00), (float("inf"), 5.00))


def tick_size(symbol=None, price=None, rows=None, exch="NSE"):
    """The tick in INR: the master's TICK_SIZE for the symbol when rows are given (paise -> INR),
    else the price-band rule. Returns (tick, source)."""
    if symbol and rows:
        sym = str(symbol).strip().upper().split(":")[-1]
        best = None
        for r in rows:
            if (r.get("EXCH") or "").upper() == exch and (r.get("TRADING_SYMBOL") or "").upper() == sym:
                rank = SERIES_RANK.get((r.get("SERIES") or "").upper(), 9)
                if best is None or rank < best[0]:
                    best = (rank, r)
        if best is not None:
            try:
                paise = int(float(best[1].get("TICK_SIZE") or 0))
                if paise > 0:
                    return paise / 100.0, "master"
            except ValueError:
                pass
    if price is not None:
        for top, tick in TICK_BANDS:
            if float(price) < top:
                return tick, "price band"
    return 0.05, "default"


# ---------------------------------------------------------------- charges on an NSE equity intraday round trip
# Rates as published on zerodha.com/charges (read 2026-11-04), brokerage as INDstocks publishes it
# (a flat Rs 10 an order). Percentages are of the traded value. Override any of them under "charges"
# in trader/config.json.
CHARGE_RATES = {
    "brokerage_per_order_inr": 10.0,   # INDstocks: flat, each order (the buy and the sell)
    "stt_intraday_sell_pct": 0.025,    # securities transaction tax, sell side only for intraday
    "exchange_txn_pct": 0.00307,       # NSE transaction charge, both sides
    "sebi_fee_pct": 0.0001,            # SEBI turnover fee, Rs 10 a crore, both sides
    "ipft_pct": 0.0001,                # NSE investor protection fund, Rs 10 a crore, both sides
    "stamp_duty_buy_pct": 0.003,       # stamp duty, buy side only for intraday
    "gst_pct": 18.0,                   # on brokerage + exchange + SEBI + IPFT
}


def charges(qty, buy_price, sell_price, rates=None):
    """Every charge on buying qty shares at buy_price and selling them at sell_price the same day,
    in INR, each rounded to the paisa. {"brokerage", "stt", "exchange", "sebi", "ipft", "stamp",
    "gst", "total", "per_share"}."""
    r = dict(CHARGE_RATES)
    r.update(rates or {})
    q = int(qty)
    if q <= 0:
        return dict((k, 0.0) for k in ("brokerage", "stt", "exchange", "sebi", "ipft", "stamp", "gst", "total", "per_share"))
    buy_turn, sell_turn = q * float(buy_price), q * float(sell_price)
    both = buy_turn + sell_turn
    brokerage = 2 * float(r["brokerage_per_order_inr"])
    stt = sell_turn * float(r["stt_intraday_sell_pct"]) / 100
    exch = both * float(r["exchange_txn_pct"]) / 100
    sebi = both * float(r["sebi_fee_pct"]) / 100
    ipft = both * float(r["ipft_pct"]) / 100
    stamp = buy_turn * float(r["stamp_duty_buy_pct"]) / 100
    gst = (brokerage + exch + sebi + ipft) * float(r["gst_pct"]) / 100
    parts = {"brokerage": brokerage, "stt": stt, "exchange": exch, "sebi": sebi, "ipft": ipft, "stamp": stamp, "gst": gst}
    out = dict((k, round(v + 1e-9, 2)) for k, v in parts.items())
    out["total"] = round(sum(parts.values()) + 1e-9, 2)
    out["per_share"] = round(out["total"] / q, 4)
    return out


def charges_leg(qty, price, side, rates=None):
    """The charges on ONE fill: a buy or a sell of qty shares at price, intraday. Rounded to the paisa.
    The two legs of a round trip add up to charges() give or take a paisa of rounding."""
    r = dict(CHARGE_RATES)
    r.update(rates or {})
    q = int(qty)
    if q <= 0:
        return {"brokerage": 0.0, "stt": 0.0, "exchange": 0.0, "sebi": 0.0, "ipft": 0.0, "stamp": 0.0, "gst": 0.0, "total": 0.0}
    turn = q * float(price)
    sell = str(side).lower() in ("sell", "s")
    brokerage = float(r["brokerage_per_order_inr"])
    stt = turn * float(r["stt_intraday_sell_pct"]) / 100 if sell else 0.0
    exch = turn * float(r["exchange_txn_pct"]) / 100
    sebi = turn * float(r["sebi_fee_pct"]) / 100
    ipft = turn * float(r["ipft_pct"]) / 100
    stamp = 0.0 if sell else turn * float(r["stamp_duty_buy_pct"]) / 100
    gst = (brokerage + exch + sebi + ipft) * float(r["gst_pct"]) / 100
    parts = {"brokerage": brokerage, "stt": stt, "exchange": exch, "sebi": sebi, "ipft": ipft, "stamp": stamp, "gst": gst}
    out = dict((k, round(v + 1e-9, 2)) for k, v in parts.items())
    out["total"] = round(sum(parts.values()) + 1e-9, 2)
    return out


# ---------------------------------------------------------------- indicators (shared by scanner, exit, timemachine)
# Textbook definitions, standard library only. Lists in, lists out, oldest first; None where the value is not
# defined yet. EMA is seeded with the SMA of its first n values; RSI, ATR and ADX use Wilder's smoothing.

def sma(values, n):
    out = [None] * len(values)
    if n <= 0:
        return out
    run = 0.0
    for i, v in enumerate(values):
        run += v
        if i >= n:
            run -= values[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def ema(values, n):
    """Exponential moving average, seeded with the SMA of the first n values."""
    out = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out[n - 1] = e
    for i in range(n, len(values)):
        e = (values[i] - e) * k + e
        out[i] = e
    return out


def wilder(values, n):
    """Wilder's smoothing (an EMA with alpha 1/n), seeded with the mean of the first n values."""
    out = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    s = sum(values[:n]) / n
    out[n - 1] = s
    for i in range(n, len(values)):
        s = (s * (n - 1) + values[i]) / n
        out[i] = s
    return out


def rsi_series(closes, n=14):
    """Wilder's RSI per bar (None for the first n bars)."""
    out = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = wilder(gains, n), wilder(losses, n)
    for i in range(len(gains)):
        if ag[i] is None:
            continue
        out[i + 1] = 100.0 if al[i] == 0 else 100 - 100 / (1 + ag[i] / al[i])
    return out


def true_ranges(highs, lows, closes):
    """True range per bar; the first bar has no previous close and uses its own high - low."""
    out = []
    for i in range(len(closes)):
        tr = highs[i] - lows[i]
        if i:
            tr = max(tr, abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        out.append(tr)
    return out


def atr_series(highs, lows, closes, n=14):
    """Wilder's ATR per bar. With fewer than n bars it averages what there is (so a stop can be set
    early in a session); the caller can see that from the index of the first value."""
    trs = true_ranges(highs, lows, closes)
    if len(trs) >= n:
        return wilder(trs, n)
    out = [None] * len(trs)
    if trs:
        out[-1] = sum(trs) / len(trs)
    return out


def supertrend(highs, lows, closes, n=10, mult=3.0):
    """Supertrend(n, mult): (line, direction) per bar, direction +1 when the price is above the line
    (an uptrend), -1 below. The bands follow the usual rule: a lower band only rises in an uptrend,
    an upper band only falls in a downtrend, and a close through the other band flips the direction."""
    m = len(closes)
    line, direction = [None] * m, [None] * m
    atr = atr_series(highs, lows, closes, n)
    upper = lower = None
    d = None
    for i in range(m):
        if atr[i] is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2
        bu, bl = hl2 + mult * atr[i], hl2 - mult * atr[i]
        if d is None:
            upper, lower, d = bu, bl, 1 if closes[i] > hl2 else -1
        elif closes[i] > upper:
            d, upper, lower = 1, bu, bl
        elif closes[i] < lower:
            d, upper, lower = -1, bu, bl
        else:
            upper = bu if (d < 0 and bu < upper) or d > 0 else upper
            lower = bl if (d > 0 and bl > lower) or d < 0 else lower
        line[i] = lower if d > 0 else upper
        direction[i] = d
    return line, direction


def macd(closes, fast=12, slow=26, signal=9):
    """(macd line, signal line, histogram) per bar."""
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    valid = [v for v in line if v is not None]
    sig_valid = ema(valid, signal)
    sig = [None] * len(line)
    j = 0
    for i, v in enumerate(line):
        if v is not None:
            sig[i] = sig_valid[j]
            j += 1
    hist = [None if (a is None or b is None) else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def adx_series(highs, lows, closes, n=14):
    """Wilder's ADX per bar, with (+DI, -DI): ({"adx", "plus_di", "minus_di"} lists)."""
    m = len(closes)
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, m):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    trs = true_ranges(highs, lows, closes)
    s_tr, s_plus, s_minus = wilder(trs[1:], n), wilder(plus_dm[1:], n), wilder(minus_dm[1:], n)
    plus_di, minus_di, dx = [None] * m, [None] * m, []
    for i in range(len(s_tr)):
        if s_tr[i] is None:
            continue
        p = 100.0 * s_plus[i] / s_tr[i] if s_tr[i] else 0.0
        q = 100.0 * s_minus[i] / s_tr[i] if s_tr[i] else 0.0
        plus_di[i + 1], minus_di[i + 1] = p, q
        dx.append(100.0 * abs(p - q) / (p + q) if (p + q) else 0.0)
    adx = [None] * m
    sm = wilder(dx, n)
    first = next((i for i in range(m) if plus_di[i] is not None), None)
    if first is not None:
        for j, v in enumerate(sm):
            if v is not None:
                adx[first + j] = v
    return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}


def bbands(closes, n=20, k=2.0):
    """Bollinger bands: ({"upper", "middle", "lower", "width"} lists); width = (upper - lower) / middle.
    The standard deviation is the population one over the n closes, as charting packages use."""
    m = len(closes)
    mid = sma(closes, n)
    upper, lower, width = [None] * m, [None] * m, [None] * m
    for i in range(n - 1, m):
        win = closes[i - n + 1:i + 1]
        mu = mid[i]
        sd = (sum((x - mu) ** 2 for x in win) / n) ** 0.5
        upper[i], lower[i] = mu + k * sd, mu - k * sd
        width[i] = (upper[i] - lower[i]) / mu if mu else None
    return {"upper": upper, "middle": mid, "lower": lower, "width": width}


def crossed_above(fast, slow, within=1):
    """True when fast crossed above slow on one of the last `within` bars."""
    m = min(len(fast), len(slow))
    for i in range(m - 1, max(m - 1 - within, 0), -1):
        a0, a1, b0, b1 = fast[i - 1], fast[i], slow[i - 1], slow[i]
        if None in (a0, a1, b0, b1):
            continue
        if a0 <= b0 and a1 > b1:
            return True
    return False


SCORE_PARTS = ("close_above_ema20", "ema20_above_ema50", "rsi_above_50", "rsi_under_70", "macd_above_signal",
               "adx_above_20")


def score_card(highs, lows, closes):
    """Six yes/no conditions on the last bar, out of 6 (None when 51 bars are not there yet):
    close > EMA20, EMA20 > EMA50, RSI14 > 50, RSI14 < 70, MACD(12,26,9) > signal, ADX14 > 20."""
    if len(closes) < 51:
        return None
    e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
    r = rsi_series(closes)[-1]
    line, sig, _ = macd(closes)
    adx = adx_series(highs, lows, closes)["adx"][-1]
    parts = {"close_above_ema20": closes[-1] > e20, "ema20_above_ema50": e20 > e50,
             "rsi_above_50": r is not None and r > 50, "rsi_under_70": r is not None and r < 70,
             "macd_above_signal": line[-1] is not None and sig[-1] is not None and line[-1] > sig[-1],
             "adx_above_20": adx is not None and adx > 20}
    return {"score": sum(1 for v in parts.values() if v), "of": 6, "parts": parts,
            "ema20": round(e20, 2), "ema50": round(e50, 2), "rsi": None if r is None else round(r, 1),
            "adx": None if adx is None else round(adx, 1)}


# ---------------------------------------------------------------- trade metrics (shared by review and the replays)

def trade_metrics(trades):
    """Efficiency of a list of closed trades, each {"date": "YYYY-MM-DD", "net": rupees after charges,
    "risk_inr": rupees at risk at the stop (optional)}, in the order they happened.

    n, won, lost, net; expectancy (mean net a trade) with a 95 % interval (normal approximation, mean +- 1.96
    standard errors; None under 2 trades); net R a trade (net / rupee risk, only trades with a risk) with its
    interval; profit factor (rupees won / rupees lost; None with no loss); max drawdown (the deepest fall of the
    running net from its high, starting at 0); weekly net by ISO week."""
    nets = [float(t["net"]) for t in trades]
    n = len(nets)

    def mean_ci(xs):
        if not xs:
            return None, None, None
        m = sum(xs) / len(xs)
        if len(xs) < 2:
            return m, None, None
        sd = (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5
        h = 1.96 * sd / len(xs) ** 0.5
        return m, m - h, m + h

    exp, lo, hi = mean_ci(nets)
    rs = [float(t["net"]) / float(t["risk_inr"]) for t in trades if t.get("risk_inr")]
    r, rlo, rhi = mean_ci(rs)
    won = sum(x for x in nets if x > 0)
    lost = -sum(x for x in nets if x < 0)
    peak = run = dd = 0.0
    for x in nets:
        run += x
        peak = max(peak, run)
        dd = max(dd, peak - run)
    weekly = {}
    for t in trades:
        y, w, _ = datetime.date.fromisoformat(t["date"]).isocalendar()
        key = "%d-W%02d" % (y, w)
        weekly[key] = round(weekly.get(key, 0.0) + float(t["net"]), 2)

    def rd(x, k=2):
        return round(x, k) if x is not None else None

    return {"n": n, "won": sum(1 for x in nets if x > 0), "lost": sum(1 for x in nets if x <= 0),
            "net": round(sum(nets), 2), "expectancy": rd(exp), "expectancy_ci95": [rd(lo), rd(hi)],
            "net_R": rd(r, 3), "net_R_ci95": [rd(rlo, 3), rd(rhi, 3)], "r_trades": len(rs),
            "profit_factor": rd(won / lost) if lost > 0 else None, "max_drawdown": round(dd, 2),
            "weekly": dict(sorted(weekly.items()))}


def render_metrics(m, unit="trade"):
    """One line: n, net, expectancy and its interval, net R, profit factor, max drawdown."""
    if not m or not m["n"]:
        return "no closed trades"

    def rs(x):
        return ("Rs " + "{:+,.2f}".format(x)) if x is not None else "n/a"
    ci = m["expectancy_ci95"]
    rci = m["net_R_ci95"]
    return ("%d %ss (%d won, %d lost), net %s; expectancy %s a %s (95%% %s to %s); net R %s a %s (95%% %s to %s); "
            "profit factor %s; max drawdown Rs %s" % (
                m["n"], unit, m["won"], m["lost"], rs(m["net"]), rs(m["expectancy"]), unit, rs(ci[0]), rs(ci[1]),
                ("%+.3f" % m["net_R"]) if m["net_R"] is not None else "n/a", unit,
                ("%+.3f" % rci[0]) if rci[0] is not None else "n/a", ("%+.3f" % rci[1]) if rci[1] is not None else "n/a",
                ("%.2f" % m["profit_factor"]) if m["profit_factor"] is not None else "n/a",
                "{:,.2f}".format(m["max_drawdown"])))


def go_no_go(replay_test, paper, min_paper_trades=200):
    """The rule before real money (decided 2026-11-16): net expectancy above zero on the held-out replay days AND
    on at least min_paper_trades paper trades. Both are trade_metrics() dicts (or None). Returns (go, reasons)."""
    reasons = []
    if not replay_test or not replay_test["n"]:
        reasons.append("no held-out replay trades yet (timemachine.py multiday)")
    elif replay_test["expectancy"] is None or replay_test["expectancy"] <= 0:
        reasons.append("held-out replay expectancy %s a trade is not above zero" % replay_test["expectancy"])
    if not paper or paper["n"] < min_paper_trades:
        reasons.append("%d paper trades, %d needed" % (paper["n"] if paper else 0, min_paper_trades))
    elif paper["expectancy"] is None or paper["expectancy"] <= 0:
        reasons.append("paper expectancy %s a trade is not above zero" % paper["expectancy"])
    return not reasons, reasons


def next_open(d):
    """The next session's open after day d, as 'YYYY-MM-DD 09:15'."""
    nd = d + datetime.timedelta(days=1)
    for _ in range(20):
        if session(nd):
            return "%s %s" % (nd.isoformat(), OPEN.strftime("%H:%M"))
        nd += datetime.timedelta(days=1)
    return None


# ---------------------------------------------------------------- transport

class _IPv4FirstHTTPSConnection(http.client.HTTPSConnection):
    """api.indstocks.com sits behind Cloudflare and publishes IPv6 addresses. On a machine whose
    IPv6 route is dead, plain urllib waits for each one to time out (about 20 s apiece) before it
    reaches IPv4; curl does not. Try the IPv4 addresses first, then anything else."""

    def connect(self):
        err = None
        infos = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM)
        infos.sort(key=lambda i: i[0] != socket.AF_INET)
        for fam, typ, proto, _, sa in infos:
            sock = None
            try:
                sock = socket.socket(fam, typ, proto)
                sock.settimeout(self.timeout)
                sock.connect(sa)
                self.sock = sock
                break
            except OSError as e:
                err = e
                if sock is not None:
                    sock.close()
        else:
            raise err or OSError("no address for %s" % self.host)
        if getattr(self, "_tunnel_host", None):
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _IPv4FirstHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_IPv4FirstHTTPSConnection, req, context=self._context)


OPENER = urllib.request.build_opener(_IPv4FirstHandler())


# ---------------------------------------------------------------- time

def ist_of(ts):
    """Epoch seconds -> naive IST datetime."""
    return EPOCH + datetime.timedelta(seconds=int(ts)) + IST


def ts_of(ist):
    """Naive IST datetime (or date) -> epoch seconds."""
    if isinstance(ist, datetime.date) and not isinstance(ist, datetime.datetime):
        ist = datetime.datetime(ist.year, ist.month, ist.day)
    return int((ist - IST - EPOCH).total_seconds())


def ms_of(ist):
    """Naive IST datetime (or date) -> epoch milliseconds, as the API's start_time/end_time want."""
    return ts_of(ist) * 1000


def parse_ist(text):
    """'2026-11-03', '2026-11-03 10:15' or '2026-11-03T10:15[:00]' -> naive IST datetime."""
    if isinstance(text, datetime.datetime):
        return text
    if isinstance(text, datetime.date):
        return datetime.datetime(text.year, text.month, text.day)
    t = str(text).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(t, fmt)
        except ValueError:
            pass
    raise DataError("cannot read the time %r (want YYYY-MM-DD or YYYY-MM-DD HH:MM, IST)" % (text,))


def now_ist():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + IST


def api_interval(interval):
    """'5m' -> '5minute'; an exact API value passes through."""
    v = INTERVALS.get(str(interval).lower(), str(interval).lower())
    if v not in MAX_DAYS:
        raise DataError("unknown interval %r (use one of %s)" % (interval, ", ".join(INTERVALS)))
    return v


# ---------------------------------------------------------------- token

def read_dotenv(path):
    """KEY=VALUE lines -> dict. Quotes around the value are stripped, # lines are skipped."""
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k.startswith("export "):
                    k = k[7:].strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                out[k] = v
    except OSError:
        pass
    return out


def token(var=TOKEN_VAR, dotenv=None):
    """The access token from the environment, else from .env next to this module. Never print it."""
    t = os.environ.get(var, "").strip()
    if not t:
        t = read_dotenv(dotenv or os.path.join(ROOT, ".env")).get(var, "").strip()
    if not t:
        raise DataError("%s is not set: generate a token at indstocks.com/app/api-trading/access-tokens "
                        "and export it (it lasts 24 hours)" % var)
    return t


# ---------------------------------------------------------------- INDstocks

class INDstocks(object):
    """INDstocks REST API (api-docs.indstocks.com). Free, bearer token, 24-hour tokens."""

    name = "indstocks"
    BASE = "https://api.indstocks.com"

    def __init__(self, token_value=None):
        self._token = token_value
        self._master = {}        # source -> rows
        self._codes = {}         # (EXCH, SYMBOL) -> scrip code, from the equity master
        self._last_request = 0.0

    # -- transport

    def _token_value(self):
        if self._token is None:
            self._token = token()
        return self._token

    def http_get(self, url, timeout=30):
        """One GET with the token. Returns the body text. Raises DataError in plain words."""
        wait = PAUSE - (time.time() - self._last_request)
        if wait > 0:
            SLEEP(wait)
        req = urllib.request.Request(url, headers={"Authorization": self._token_value(),
                                                   "User-Agent": UA, "Accept": "*/*"})
        path = url.replace(self.BASE, "").split("?")[0]
        last = "no answer"
        for attempt in (0, 1, 2):
            try:
                self._last_request = time.time()
                with OPENER.open(req, timeout=timeout) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                try:
                    body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 403 and attempt < 2:
                    # INDstocks answers 403 with the "token expired" text when a burst trips its rate limit
                    # (seen 2026-11-06 after about 900 requests in five minutes, with a token that whoami
                    # confirmed): back off before believing it.
                    last = "HTTP 403 (a throttle after a burst, or the token)"
                    SLEEP(THROTTLE_BACKOFF * (attempt + 1))
                    continue
                if e.code in (401, 403):
                    raise DataError("INDstocks answered HTTP %d for %s: the token is invalid or has expired "
                                    "(it dies with the day, not 24 hours after generation)%s. Generate a new one "
                                    "and set %s." % (e.code, path,
                                                     ", or a burst of requests tripped its rate limit" if e.code == 403 else "",
                                                     TOKEN_VAR))
                if e.code == 429 and attempt == 0:
                    last = "HTTP 429"
                    SLEEP(BACKOFF)
                    continue
                if e.code >= 500 and attempt == 0:
                    last = "HTTP %d" % e.code
                    SLEEP(1.0)
                    continue
                raise DataError("INDstocks answered HTTP %d for %s: %s" % (e.code, path, _api_message(body)))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                if attempt < 2:
                    SLEEP(1.0 + attempt)   # a DNS or socket hiccup: 1 s, then 2 s
                    continue
        raise DataError("INDstocks did not answer (%s) for %s" % (last, path))

    def get_json(self, path, params=None):
        url = self.BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params, safe=",_")
        text = self.http_get(url)
        try:
            return json.loads(text)
        except ValueError:
            raise DataError("INDstocks returned something that is not JSON for %s" % path)

    def post_json(self, path, body, timeout=30):
        """One POST with a JSON body and the token (orders, cancels). Returns the parsed answer.
        No retry: a repeated POST could place an order twice. The caller decides what to do with
        a DataError. Order endpoints also need the static IP whitelisted at indstocks.com."""
        wait = PAUSE - (time.time() - self._last_request)
        if wait > 0:
            SLEEP(wait)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.BASE + path, data=data, method="POST",
                                     headers={"Authorization": self._token_value(), "User-Agent": UA,
                                              "Accept": "application/json", "Content-Type": "application/json"})
        self._last_request = time.time()
        try:
            with OPENER.open(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", "replace")
            except Exception:
                err_body = ""
            finally:
                e.close()
            if e.code in (401, 403):
                raise DataError("INDstocks answered HTTP %d for %s: the token is invalid or has expired, or this "
                                "machine's IP is not whitelisted for orders. %s" % (e.code, path, _api_message(err_body)))
            raise DataError("INDstocks answered HTTP %d for %s: %s" % (e.code, path, _api_message(err_body)))
        except (urllib.error.URLError, OSError) as e:
            raise DataError("INDstocks did not answer (%s) for %s: check the order book before trying again"
                            % (getattr(e, "reason", e), path))
        try:
            return json.loads(text)
        except ValueError:
            raise DataError("INDstocks returned something that is not JSON for %s" % path)

    # -- instruments

    def instruments_text(self, source="equity"):
        return self.http_get(self.BASE + "/market/instruments?source=" + source)

    def instruments(self, source="equity"):
        """Rows of the instrument master as dicts (the CSV's own column names), cached per process."""
        if source not in self._master:
            self._master[source] = parse_instruments(self.instruments_text(source), source)
        return self._master[source]

    def load_instruments(self, path, source="equity"):
        """Use a saved master (tests, offline) instead of downloading one."""
        self._master[source] = parse_instruments(read_text(path), source)
        self._codes = {}
        return self._master[source]

    def resolve(self, symbol):
        """'RELIANCE' -> 'NSE_2885'; 'BSE:RELIANCE' -> 'BSE_500325'; 'NSE_2885' passes through."""
        s = str(symbol).strip().upper()
        if "_" in s:
            seg, num = s.split("_", 1)
            if seg in ("NSE", "BSE", "NFO", "MCX") and num.isdigit():
                return s
        exch, sym = ("NSE", s) if ":" not in s else s.split(":", 1)
        if not self._codes:
            self._codes = code_table(self.instruments("equity"))
        code = self._codes.get((exch, sym))
        if not code:
            raise DataError("%s is not in the INDstocks %s equity master" % (sym, exch))
        return code

    # -- candles

    def raw_candles(self, codes, interval, start_ms, end_ms):
        """One historical request, the API's own JSON document."""
        codes = list(dict.fromkeys(codes))
        if len(codes) > CANDLES_PER_CALL:
            raise DataError("at most %d scrip codes per historical request" % CANDLES_PER_CALL)
        if start_ms > end_ms:
            raise DataError("start is after end")
        return self.get_json("/market/historical/" + interval,
                             {"scrip-codes": ",".join(codes), "start_time": start_ms, "end_time": end_ms})

    def candles_many(self, symbols, interval, start, end=None):
        """{symbol: [bars]} for any number of symbols, 5 a request, one request per window."""
        iv = api_interval(interval)
        start_dt, end_dt = window(start, end, iv)
        codes = {}
        for s in symbols:
            codes[s] = self.resolve(s)
        back = {}
        for s, c in codes.items():
            back.setdefault(c, s)
        out = {}
        for s in symbols:
            out[s] = []
        uniq = list(dict.fromkeys(codes.values()))
        for i in range(0, len(uniq), CANDLES_PER_CALL):
            batch = uniq[i:i + CANDLES_PER_CALL]
            for a, b in windows(start_dt, end_dt, MAX_DAYS[iv]):
                doc = self.raw_candles(batch, iv, ms_of(a), ms_of(b))
                for code, bars in parse_candles(doc).items():
                    if code in back:
                        out[back[code]].extend(bars)
        for s in out:
            out[s] = tidy(out[s], ts_of(start_dt), ts_of(end_dt))
        return out

    def candles(self, symbol, interval, start, end=None):
        """Bars for one symbol between start (inclusive) and end (exclusive), IST. end defaults to
        start + one day when start is a bare date and the bars are intraday, else now."""
        return self.candles_many([symbol], interval, start, end)[symbol]

    # -- quotes

    def raw_quotes(self, codes, kind="full"):
        codes = list(dict.fromkeys(codes))
        if len(codes) > QUOTES_PER_CALL:
            raise DataError("at most %d scrip codes per quote request" % QUOTES_PER_CALL)
        return self.get_json("/market/quotes/" + kind, {"scrip-codes": ",".join(codes)})

    def quote(self, symbols):
        """{symbol: quote} with ltp, open, high, low, prev_close, volume, upper/lower circuit, change."""
        if isinstance(symbols, str):
            symbols = [symbols]
        codes = {}
        for s in symbols:
            codes[s] = self.resolve(s)
        back = {}
        for s, c in codes.items():
            back.setdefault(c, s)
        uniq = list(dict.fromkeys(codes.values()))
        out = {}
        for i in range(0, len(uniq), QUOTES_PER_CALL):
            doc = self.raw_quotes(uniq[i:i + QUOTES_PER_CALL])
            for code, q in parse_quotes(doc).items():
                if code in back:
                    out[back[code]] = q
        return out

    def whoami(self):
        doc = self.get_json("/user/profile")
        return doc.get("data") or {}


class Kite(object):
    name = "kite"

    def __getattr__(self, item):
        raise DataError("the kite backend is not built yet: use indstocks")


class Yahoo(object):
    name = "yahoo"

    def __getattr__(self, item):
        raise DataError("the yahoo backend (daily closes only) is not built yet: use indstocks")


BACKENDS = {"indstocks": INDstocks, "kite": Kite, "yahoo": Yahoo}
_default = {}


def backend(name=None):
    """The backend named, else $TRADER_DATA_BACKEND, else indstocks. One instance per name."""
    name = (name or os.environ.get(BACKEND_VAR) or "indstocks").lower()
    if name not in BACKENDS:
        raise DataError("unknown data backend %r (use one of %s)" % (name, ", ".join(BACKENDS)))
    if name not in _default:
        _default[name] = BACKENDS[name]()
    return _default[name]


def instruments(source="equity", backend_name=None):
    return backend(backend_name).instruments(source)


def candles(symbol, interval, start, end=None, backend_name=None):
    return backend(backend_name).candles(symbol, interval, start, end)


def quote(symbols, backend_name=None):
    return backend(backend_name).quote(symbols)


# ---------------------------------------------------------------- parsing (pure, tested offline)

def _api_message(body):
    try:
        doc = json.loads(body)
        if isinstance(doc, dict):
            return str(doc.get("message") or doc.get("debug_info") or doc.get("error_code") or body[:200])
        return body.strip()[:200]
    except ValueError:
        return body.strip()[:200] or "no detail"


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def parse_instruments(text, source="equity"):
    """The instruments CSV -> list of dict rows. The index file has no real header: read it by position."""
    if source == "index":
        rows = []
        for r in csv.reader(io.StringIO(text)):
            if len(r) >= 3 and r[0] != "EXCH":
                rows.append({"EXCH": r[0], "INDEX_NAME": r[1], "SECURITY_ID": r[2]})
        return rows
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "SECURITY_ID" not in rows[0] or "TRADING_SYMBOL" not in rows[0]:
        raise DataError("the instruments file is not the shape this tool knows (no SECURITY_ID column)")
    return rows


def code_table(rows):
    """Equity rows -> {(EXCH, TRADING_SYMBOL): 'EXCH_SECURITYID'}, the main series first (EQ on NSE,
    A on BSE), so RELIANCE resolves to its cash line, not a BE/T line of the same name."""
    best = {}
    for r in rows:
        if (r.get("INSTRUMENT_NAME") or "").upper() != "EQUITY":
            continue
        exch = (r.get("EXCH") or "").upper()
        sym = (r.get("TRADING_SYMBOL") or "").upper()
        sid = r.get("SECURITY_ID")
        if not (exch and sym and sid):
            continue
        rank = SERIES_RANK.get((r.get("SERIES") or "").upper(), 9)
        key = (exch, sym)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, "%s_%s" % (exch, sid))
    return dict((k, v[1]) for k, v in best.items())


def parse_candles(doc):
    """The historical JSON -> {code: [bars]}, bars as {"ts","o","h","l","c","v"} sorted by ts."""
    ok = isinstance(doc, dict) and (doc.get("success") is True or doc.get("status") == "success")
    if not ok:
        raise DataError("INDstocks historical: %s" % _api_message(json.dumps(doc) if isinstance(doc, dict) else str(doc)))
    out = {}
    for code, body in (doc.get("data") or {}).items():
        bars = []
        for c in (body or {}).get("candles") or []:
            try:
                bars.append({"ts": int(c["ts"]), "o": float(c["o"]), "h": float(c["h"]),
                             "l": float(c["l"]), "c": float(c["c"]), "v": int(c.get("v") or 0)})
            except (KeyError, TypeError, ValueError):
                continue
        bars.sort(key=lambda b: b["ts"])
        out[code] = bars
    return out


def parse_quotes(doc):
    """The full-quote JSON -> {code: {ltp, open, high, low, prev_close, volume, upper_circuit,
    lower_circuit, change, change_pct, high_52w, low_52w}}. Fields the API leaves out are None."""
    if not isinstance(doc, dict) or doc.get("status") != "success":
        raise DataError("INDstocks quotes: %s" % _api_message(json.dumps(doc) if isinstance(doc, dict) else str(doc)))
    out = {}
    for code, q in (doc.get("data") or {}).items():
        q = q or {}
        row = {}
        for ours, theirs in QUOTE_FIELDS:
            row[ours] = _num(q.get(theirs))
        out[code] = row
    return out


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def window(start, end, iv):
    """(start, end) as naive IST datetimes. A bare date for end means midnight starting that day,
    so ('2026-11-03', '2026-11-04') is exactly one session. No end: one day for intraday bars
    starting on a date, else now."""
    s = parse_ist(start)
    if end is None:
        if SECONDS.get(iv, 86400) < 86400 and s.time() == datetime.time(0, 0):
            e = s + datetime.timedelta(days=1)
        else:
            e = now_ist()
    else:
        e = parse_ist(end)
    if e <= s:
        raise DataError("the end %s is not after the start %s" % (e, s))
    return s, e


def windows(start, end, max_days):
    """Split [start, end) into pieces no wider than the API allows for the interval."""
    out = []
    a = start
    step = datetime.timedelta(days=max_days)
    while a < end:
        b = min(a + step, end)
        out.append((a, b))
        a = b
    return out


def tidy(bars, start_ts, end_ts):
    """Drop duplicates (a window boundary can repeat a bar) and anything outside [start, end)."""
    seen = {}
    for b in bars:
        if start_ts <= b["ts"] < end_ts:
            seen[b["ts"]] = b
    return [seen[t] for t in sorted(seen)]


def bar_label(bar, iv="5minute"):
    t = ist_of(bar["ts"])
    return t.strftime("%Y-%m-%d") if SECONDS.get(iv, 0) >= 86400 else t.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- CLI

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


def cmd_whoami(a):
    me = backend(a.backend).whoami()
    print("INDstocks token is good: user %s, NSE %s, BSE %s" % (
        me.get("user_id", "?"), "yes" if me.get("is_nse_onboarded") else "no",
        "yes" if me.get("is_bse_onboarded") else "no"))
    return 0


def cmd_instruments(a):
    b = backend(a.backend)
    text = b.instruments_text(a.source)
    rows = parse_instruments(text, a.source)
    if a.out:
        write_text(a.out, text)
    by_exch = {}
    for r in rows:
        by_exch[r.get("EXCH", "?")] = by_exch.get(r.get("EXCH", "?"), 0) + 1
    print("%s master: %d rows (%s)%s" % (a.source, len(rows), ", ".join("%s %d" % kv for kv in sorted(by_exch.items())),
                                         " -> " + a.out if a.out else ""))
    return 0


def cmd_candles(a):
    b = backend(a.backend)
    iv = api_interval(a.interval)
    s, e = window(a.start, a.end, iv)
    code = b.resolve(a.symbol)
    if a.out:
        if (e - s).days > MAX_DAYS[iv]:
            raise DataError("--out saves one raw answer: keep the window within %d days for %s" % (MAX_DAYS[iv], iv))
        doc = b.raw_candles([code], iv, ms_of(s), ms_of(e))
        write_text(a.out, json.dumps(doc))
        bars = tidy(parse_candles(doc).get(code, []), ts_of(s), ts_of(e))
    else:
        bars = b.candles(a.symbol, a.interval, a.start, a.end)
    if a.json:
        print(json.dumps({"symbol": a.symbol.upper(), "code": code, "interval": iv, "start": str(s), "end": str(e),
                          "bars": bars}, indent=1))
        return 0 if bars else 1
    print("%s (%s) %s bars, %s to %s IST: %d bars%s" % (a.symbol.upper(), code, a.interval, s, e, len(bars),
                                                        " -> " + a.out if a.out else ""))
    if not bars:
        print("no bars: a holiday, a weekend, or a window before the data starts")
        return 1
    shown = bars if a.show <= 0 else bars[:a.show]
    print("%-16s %10s %10s %10s %10s %10s" % ("open at (IST)", "open", "high", "low", "close", "volume"))
    for bar in shown:
        print("%-16s %10.2f %10.2f %10.2f %10.2f %10d" % (bar_label(bar, iv), bar["o"], bar["h"], bar["l"], bar["c"], bar["v"]))
    if len(shown) < len(bars):
        print("... %d more" % (len(bars) - len(shown)))
    return 0


def cmd_quote(a):
    b = backend(a.backend)
    syms = [s.strip() for s in a.symbols.split(",") if s.strip()]
    if a.out:
        codes = [b.resolve(s) for s in syms]
        doc = b.raw_quotes(codes)
        write_text(a.out, json.dumps(doc))
        parsed = parse_quotes(doc)
        q = dict((s, parsed[c]) for s, c in zip(syms, codes) if c in parsed)
    else:
        q = b.quote(syms)
    if a.json:
        print(json.dumps(q, indent=1))
        return 0
    print("%-12s %10s %10s %10s %10s %10s %12s   band" % ("symbol", "ltp", "open", "high", "low", "prev", "volume"))
    for s in syms:
        r = q.get(s) or q.get(s.upper())
        if not r:
            print("%-12s no quote" % s)
            continue
        print("%-12s %10s %10s %10s %10s %10s %12s   %s to %s" % (
            s.upper(), _f(r["ltp"]), _f(r["open"]), _f(r["high"]), _f(r["low"]), _f(r["prev_close"]),
            _f(r["volume"], 0), _f(r["lower_circuit"]), _f(r["upper_circuit"])))
    return 0


def _f(x, d=2):
    if x is None:
        return "-"
    return ("%%.%df" % d) % x


def main(argv=None):
    p = argparse.ArgumentParser(description="Market data for the day-trader-india skills (INDstocks).")
    p.add_argument("--backend", default=None, help="indstocks (default) | kite | yahoo")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("whoami", help="check the token")
    i = sub.add_parser("instruments", help="download the instrument master")
    i.add_argument("--source", default="equity", choices=["equity", "fno", "index", "mcx"])
    i.add_argument("--out", default=None, help="save the raw CSV here (.gz to compress)")
    c = sub.add_parser("candles", help="historical bars")
    c.add_argument("symbol")
    c.add_argument("interval", help="1m 5m 15m 1h 1d ...")
    c.add_argument("start", help="YYYY-MM-DD or 'YYYY-MM-DD HH:MM', IST, inclusive")
    c.add_argument("end", nargs="?", default=None, help="exclusive; default one day after a date start")
    c.add_argument("--show", type=int, default=10, help="rows to print (0 = all)")
    c.add_argument("--out", default=None, help="save the raw API answer here")
    c.add_argument("--json", action="store_true")
    q = sub.add_parser("quote", help="live quotes")
    q.add_argument("symbols", help="comma separated, RELIANCE,TCS")
    q.add_argument("--out", default=None, help="save the raw API answer here")
    q.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 1
    try:
        return {"whoami": cmd_whoami, "instruments": cmd_instruments, "candles": cmd_candles, "quote": cmd_quote}[a.cmd](a)
    except DataError as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""The Day: the conductor for the eleven trader skills (trader-day, NSE). Standard library only.

It knows the clock, the state files in trader/ and the order of the chain, runs the mechanical
steps by calling the other skills' scripts, and stops at every step that needs a judgment or a yes.

    python3 day.py status                      # where the day stands and the exact next command
    python3 day.py morning                     # 08:45: the daily scan, then news on the hits
    python3 day.py gate                        # 10:00: breadth, and the opening-range scan if the gate opens
    python3 day.py watch                       # while a trade is open: alerts, then the paper ledger's tick
    python3 day.py evening --note "..."        # after 15:30: save the day's bars, replay the universe, journal
    python3 day.py --dry-run morning           # print the commands instead of running them
    python3 day.py schedule install            # weekdays 08:45 morning, 10:01 gate, 15:40 evening (Windows)

It never picks a chart, never sets a stop, never says yes: those are /trader-entry, /trader-exit,
/trader-timemachine, /trader-budget, /trader-risk and /trader-broker with the user. It never places
an order. Every step it runs is one of the other scripts with the flags the routine fixes.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import trader_data  # noqa: E402

SCRIPTS = {
    "scanner": ("trader-scanner", "scan.py"), "news": ("trader-news", "news.py"),
    "entry": ("trader-entry", "entry.py"), "exit": ("trader-exit", "exit.py"),
    "timemachine": ("trader-timemachine", "timemachine.py"), "budget": ("trader-budget", "size.py"),
    "risk": ("trader-risk", "check.py"), "broker": ("trader-broker", "ticket.py"),
    "alerts": ("trader-alerts", "alerts.py"), "analyst": ("trader-analyst", "results.py"),
    "insiders": ("trader-insiders", "insiders.py"),
}
FILES = {
    "config": "trader/config.json", "universe": "trader/universe.txt", "watchlist": "trader/watchlist.json",
    "breadth": "trader/state/breadth.json", "entry": "trader/entry.json", "plan": "trader/plan.json",
    "timemachine": "trader/timemachine.json", "orders": "trader/orders.json", "risk": "trader/state/risk.json",
    "paper": "trader/paper.json", "alerts": "trader/alerts.json", "replays": "trader/replays",
    "journal": "trader/journal",
}
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
PHASES = [  # (from, to, name)
    ("00:00", "08:45", "before the day"), ("08:45", "09:15", "pre-market"), ("09:15", "10:00", "early session"),
    ("10:00", "11:00", "entry window"), ("11:00", "15:15", "holding window"), ("15:15", "15:30", "flat by"),
    ("15:30", "24:00", "evening"),
]


class DayError(Exception):
    pass


LOG = None          # the open log file of an unattended run (--log), which the steps write into too

# The unattended day: Windows Task Scheduler runs these on weekdays; day.py itself skips NSE holidays.
SCHEDULE = [("morning", "08:45"), ("gate", "10:01"), ("evening", "15:40")]
TASK_PREFIX = "day-trader-india"


def now_ist(text=None):
    if text:
        try:
            return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError:
            raise DayError("--now wants \"YYYY-MM-DD HH:MM\" (IST)")
    return datetime.datetime.now(IST).replace(tzinfo=None, second=0, microsecond=0)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def script(name):
    folder, file = SCRIPTS[name]
    return os.path.join(ROOT, folder, file)


def cfg_value(cfg, key, default):
    v = (cfg or {}).get(key)
    return v if v not in (None, "") else default


def phase_of(now, cfg):
    """The phase the clock is in, with the config's own gate, cutoff and flat-by times."""
    t = now.strftime("%H:%M")
    gate = str(cfg_value(cfg, "gate_time", "10:00"))
    cutoff = str(cfg_value(cfg, "no_entries_after", "11:00"))
    flat = str(cfg_value(cfg, "flat_by", "15:15"))
    bounds = [("00:00", "08:45", "before the day"), ("08:45", "09:15", "pre-market"), ("09:15", gate, "early session"),
              (gate, cutoff, "entry window"), (cutoff, flat, "holding window"), (flat, "15:30", "flat by"),
              ("15:30", "24:00", "evening")]
    for a, b, name in bounds:
        if a <= t < b:
            return name
    return "evening"


def today_doc(doc, key, today):
    """The doc when its date field (key) is today's, else None."""
    if not doc:
        return None
    v = str(doc.get(key) or "")
    return doc if v[:10] == today else None


def state(now, cfg):
    """Everything the conductor needs to decide the next step."""
    today = now.date().isoformat()
    s = {"today": today, "time": now.strftime("%H:%M"), "phase": phase_of(now, cfg)}
    open_day, why = trader_data.trading_day(now.date())
    s["trading_day"] = open_day
    s["why_closed"] = why
    s["universe"] = os.path.exists(FILES["universe"])
    w = read_json(FILES["watchlist"])
    s["scan"] = {"mode": w.get("mode"), "hits": len(w.get("hits") or []), "as_of": w.get("as_of")} \
        if w and str(w.get("created_at") or "")[:10] == today else None    # a daily scan's as_of is yesterday's close
    b = today_doc(read_json(FILES["breadth"]), "date", today)
    s["gate"] = {"time": b["time"], "pct": b["pct"], "open": b["open"]} if b else None
    e = today_doc(read_json(FILES["entry"]), "date", today)
    s["entry"] = {"symbol": e.get("symbol"), "status": e.get("status"), "time": e.get("time")} if e else None
    p = today_doc(read_json(FILES["plan"]), "date", today)
    s["plan"] = {"symbol": p.get("symbol"), "stop": p.get("stop"), "target": p.get("target")} if p else None
    tmd = read_json(FILES["timemachine"])
    s["timemachine"] = bool(tmd and str((tmd.get("plan") or {}).get("date") or "")[:10] == today)
    o = read_json(FILES["orders"])
    s["orders"] = len(o.get("orders") or []) if o and str(o.get("created_at") or o.get("date") or "")[:10] == today else 0
    r = read_json(FILES["risk"])
    s["risk"] = {"pass": r.get("pass"), "block": r.get("block")} if r and str(r.get("checked_at") or "")[:10] == today else None
    ledger = read_json(FILES["paper"]) or {}
    pos = ledger.get("positions") or {}
    s["position"] = {sym: {"qty": v.get("qty"), "avg": v.get("avg_price")} for sym, v in pos.items()}
    sells = [x for x in ledger.get("orders") or [] if x.get("txn_type") == "SELL" and x.get("status") == "SUCCESS"
             and str(x.get("updated_at") or "")[:10] == today and "pnl" in x]
    s["closed"] = [{"symbol": x["symbol"], "pnl": x["pnl"]} for x in sells]
    s["replay"] = os.path.exists(os.path.join(FILES["replays"], "%s.json" % today))
    s["bars_saved"] = os.path.exists(os.path.join(FILES["replays"], "orb5m-%s.json.gz" % today))
    s["journal"] = os.path.exists(os.path.join(FILES["journal"], "%s.json" % today))
    s["max_trades"] = int(cfg_value(cfg, "max_trades_per_day", 2))
    s["stop_after_loss"] = bool(cfg_value(cfg, "stop_after_loss", False))
    return s


def next_step(s):
    """(what to do, the command or the hand-off) from the state. One step at a time."""
    if not s["trading_day"]:
        return "No session today (%s)." % (s["why_closed"] or "closed"), None
    ph = s["phase"]
    if ph == "before the day":
        return "Nothing until 08:45.", None
    if not s["universe"]:
        return "No universe yet.", "day.py morning (builds trader/universe.txt first)"
    if ph == "pre-market":
        if not s["scan"] or s["scan"]["mode"] != "daily":
            return "The pre-market scan has not run.", "day.py morning"
        return "Read the hits and the news; take nothing before the gate.", None
    if ph == "early session":
        return "Watch the early breakouts, take nothing: the entry window opens at the gate time.", "scan.py orb (optional)"
    lost = [c for c in s["closed"] if c["pnl"] <= 0]
    if s["position"]:
        sym = ", ".join(s["position"])
        if ph in ("entry window", "holding window"):
            return "%s is open on the paper ledger." % sym, "day.py watch (every few minutes, or /loop 5m)"
        if ph == "flat by":
            return "%s is still open at flat-by." % sym, "ticket.py flatten --symbol %s, then check.py and your yes" % sym
    if ph in ("entry window", "holding window"):
        if s["stop_after_loss"] and lost:
            return "A loss closed the day (%s)." % ", ".join(c["symbol"] for c in lost), "day.py evening after 15:30"
        if len(s["closed"]) >= s["max_trades"]:
            return "%d trades done, the day's limit." % len(s["closed"]), "day.py evening after 15:30"
        if ph == "holding window":
            return "Past the entry cutoff with nothing open.", "day.py evening after 15:30"
        if not s["gate"]:
            return "The gate has not been read.", "day.py gate"
        if not s["gate"]["open"]:
            return "The gate is closed (%s%% at %s): a watching day." % (s["gate"]["pct"], s["gate"]["time"]), \
                "day.py gate again later in the window, else day.py evening"
        if not s["entry"]:
            return "Gate open. Pick ONE chart from the opening-range hits.", "entry.py check SYMBOL --setup orb"
        if s["entry"]["status"] == "waiting":
            return "%s is waiting for a close above the range." % s["entry"]["symbol"], "entry.py watch %s" % s["entry"]["symbol"]
        if s["entry"]["status"] != "triggered":
            return "%s %s: pick another chart or wait." % (s["entry"]["symbol"], s["entry"]["status"]), "entry.py check SYMBOL --setup orb"
        if not s["plan"]:
            return "%s triggered at %s: set the exit before anything else." % (s["entry"]["symbol"], s["entry"]["time"]), \
                "exit.py plan --target 2R --write"
        if not s["timemachine"]:
            return "Plan set: replay it.", "timemachine.py run --write"
        if not s["orders"]:
            return "Size it.", "size.py --write"
        if not s["risk"]:
            return "Check it.", "check.py"
        if s["risk"]["block"]:
            return "Risk blocked it: read the reason, then fix or drop the trade.", None
        return "Risk passed. Show the ticket and wait for the user's yes.", "ticket.py show, then approve and place"
    if ph == "flat by":
        return "Flat by: nothing open.", "day.py evening after 15:30"
    if not s["bars_saved"]:
        return "Evening: save the day's bars and replay.", "day.py evening --note \"...\""
    if not s["replay"]:
        return "Bars saved, replay missing.", "timemachine.py universe trader/replays/orb5m-%s.json.gz --compare --others --write" % s["today"]
    if not s["journal"]:
        return "Write the journal.", "check.py journal --note \"...\""
    return "The day is done. Friday: check.py review --days 7.", None


def render_status(s):
    out = ["%s %s IST: %s%s." % (s["today"], s["time"], s["phase"],
                                  "" if s["trading_day"] else " (NSE closed: %s)" % (s["why_closed"] or "no session"))]
    out.append("  scan      %s" % ("%s, %d hits (%s)" % (s["scan"]["mode"], s["scan"]["hits"], s["scan"]["as_of"]) if s["scan"] else "not today"))
    out.append("  gate      %s" % ("%s, %s%% at %s" % ("open" if s["gate"]["open"] else "closed", s["gate"]["pct"], s["gate"]["time"]) if s["gate"] else "not read"))
    out.append("  entry     %s" % ("%s %s %s" % (s["entry"]["symbol"], s["entry"]["status"], s["entry"]["time"] or "") if s["entry"] else "none"))
    out.append("  plan      %s" % ("%s stop %s target %s" % (s["plan"]["symbol"], s["plan"]["stop"], s["plan"]["target"]) if s["plan"] else "none"))
    out.append("  machine   %s" % ("run" if s["timemachine"] else "not run"))
    out.append("  orders    %s" % ("%d proposed, risk %s" % (s["orders"], ("%d pass %d block" % (s["risk"]["pass"], s["risk"]["block"])) if s["risk"] else "not run") if s["orders"] else "none"))
    out.append("  ledger    %s%s" % (", ".join("%s %s at %s" % (k, v["qty"], v["avg"]) for k, v in s["position"].items()) or "no position",
                                     ("; closed today: " + ", ".join("%s %+.2f" % (c["symbol"], c["pnl"]) for c in s["closed"])) if s["closed"] else ""))
    out.append("  evening   bars %s, replay %s, journal %s" % tuple("yes" if x else "no" for x in (s["bars_saved"], s["replay"], s["journal"])))
    what, cmd = next_step(s)
    out.append("Next: %s%s" % (what, ("  ->  %s" % cmd) if cmd else ""))
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- running the other scripts

def run_step(label, args, dry_run, ok_codes=(0, 1)):
    """Run one sibling script, streaming its output; a non-zero code that the step treats as 'nothing found'
    (1) is not an error."""
    cmd = [sys.executable] + args
    print("== %s\n$ %s" % (label, " ".join(os.path.basename(a) if a.endswith(".py") else a for a in args)))
    if dry_run:
        return 0
    sys.stdout.flush()
    p = subprocess.run(cmd, stdout=LOG, stderr=LOG) if LOG else subprocess.run(cmd)
    if p.returncode not in ok_codes:
        raise DayError("%s failed (exit %d); stop and read the message above" % (label, p.returncode))
    return p.returncode


def token_check():
    """(ok, message): one INDstocks call. A rejected token is replaced once from the TOTP credentials when .env
    has them (trader_data.generate_token); the new token lives in this process's environment, so every step this
    run starts inherits it, and it is never written down."""
    had = os.environ.get(trader_data.TOKEN_VAR)
    try:
        me = trader_data.INDstocks().whoami()
        fresh = os.environ.get(trader_data.TOKEN_VAR) != had
        return True, "token %s (user %s)" % ("refreshed from the TOTP credentials" if fresh else "good",
                                             me.get("user_id", "?"))
    except trader_data.DataError as e:
        return False, "token: %s" % str(e).splitlines()[0]


def require_token(step):
    ok, msg = token_check()
    if not ok:
        raise DayError("%s. Regenerate it at indstocks.com (API trading, access tokens) and put it on the "
                       "INDSTOCKS_TOKEN line of .env, or set %s there for the automatic one, then run day.py %s "
                       "again." % (msg, ", ".join(trader_data.TOTP_VARS), step))
    print("== %s" % msg)


def cmd_status(a, now, cfg):
    s = state(now, cfg)
    if not a.now and not a.no_token:
        ok, msg = token_check()
        s["token"] = {"ok": ok, "message": msg}
    if a.json:
        s["next"] = dict(zip(("what", "command"), next_step(s)))
        print(json.dumps(s, indent=2))
    else:
        text = render_status(s)
        if s.get("token"):
            text = text.replace("\nNext:", "\n  token     %s\nNext:" % s["token"]["message"], 1)
            if not s["token"]["ok"]:
                text += "The token is dead: regenerate it at indstocks.com (API trading, access tokens) and put it on " \
                        "the INDSTOCKS_TOKEN line of .env before any step that reads the market.\n"
        sys.stdout.write(text)
    return 0


def cmd_morning(a, now, cfg):
    if not a.dry_run:
        require_token("morning")
    stamp = ["--now", now.strftime("%Y-%m-%d %H:%M")] if a.now else []
    if not os.path.exists(FILES["universe"]) or a.universe:
        run_step("the universe (INDstocks master minus ASM/GSM)", [script("scanner"), "universe"], a.dry_run)
    code = run_step("the pre-market scan", [script("scanner"), "daily", "--show", str(a.show)] + stamp, a.dry_run)
    if code == 0 or a.dry_run:
        run_step("news on the hits", [script("news"), "--hours", "18"] + (["--now", now.strftime("%Y-%m-%dT%H:%M")] if a.now else []),
                 a.dry_run)
    print("Morning done. Read, do not trade. Next: day.py gate at %s." % cfg_value(cfg, "gate_time", "10:00"))
    return 0


def cmd_gate(a, now, cfg):
    stamp = ["--now", now.strftime("%Y-%m-%d %H:%M")] if a.now else []
    off = ["--offline", a.offline] if a.offline else []
    if not a.dry_run and not a.offline and not a.now:
        require_token("gate")
    code = run_step("breadth", [script("scanner"), "breadth"] + stamp + off, a.dry_run)
    if a.dry_run:
        run_step("the opening-range scan", [script("scanner"), "orb", "--show", str(a.show)] + stamp + off, True)
        return 0
    if code != 0:
        print("Gate closed: a watching day unless breadth lifts before %s. Run day.py gate again later, or "
              "day.py evening after the close." % cfg_value(cfg, "no_entries_after", "11:00"))
        return 1
    run_step("the opening-range scan", [script("scanner"), "orb", "--show", str(a.show)] + stamp + off
             + (["--daily", a.daily] if a.daily else []), False)
    print("Gate open. Pick ONE chart: entry.py check SYMBOL --setup orb, then exit, timemachine, budget, risk, "
          "and the ticket waits for the user's yes.")
    return 0


def cmd_watch(a, now, cfg):
    stamp = ["--now", now.strftime("%Y-%m-%d %H:%M")] if a.now else []
    run_step("alerts", [script("alerts"), "check"] + stamp, a.dry_run)
    run_step("the paper ledger", [script("broker"), "paper", "tick"] + stamp + (["--price", a.price] if a.price else []),
             a.dry_run)
    return 0


def cmd_evening(a, now, cfg):
    today = now.date().isoformat()
    if not a.dry_run and not a.now:
        require_token("evening")
    os.makedirs(FILES["replays"], exist_ok=True)
    bundle = "%s/orb5m-%s.json.gz" % (FILES["replays"], today)
    if not os.path.exists(bundle) or a.refetch:
        # --until 15:30 overrides the config's no_entries_after: this run saves the whole session's bars,
        # it does not look for an entry. The bundle must exist afterwards or the evening stops here.
        run_step("saving the day's 5-minute bars for every chart (about ten minutes)",
                 [script("scanner"), "orb", "--now", "%s 15:00" % today, "--until", "15:30", "--save", bundle,
                  "--no-trend", "--out", "%s/watchlist-%s.json" % (FILES["replays"], today), "--show", "0"],
                 a.dry_run)
        if not a.dry_run and not os.path.exists(bundle):
            raise DayError("the scanner did not save %s; read its message above, then run day.py evening again" % bundle)
    else:
        print("== bars already saved: %s" % bundle)
    run_step("the evening replay", [script("timemachine"), "universe", bundle, "--compare", "--others", "--write"],
             a.dry_run, ok_codes=(0,))
    run_step("the pre-registered tests, scored on new sessions only",
             [script("timemachine"), "registered", "--write", os.path.join("trader", "replays", "registered.json")],
             a.dry_run, ok_codes=(0,))
    notes = []
    for n in a.note or []:
        notes += ["--note", n]
    run_step("the journal", [script("risk"), "journal", "--today", today] + notes, a.dry_run, ok_codes=(0,))
    print("Evening done. Friday: check.py review --days 7.")
    return 0


def schedule_tasks(python, root):
    """[(task name, HH:MM, command line)] for Windows Task Scheduler: weekdays, from the project folder, logged."""
    out = []
    for step, at in SCHEDULE:
        cmd = '"%s" "%s" %s --root --log "%s"' % (python, os.path.join(root, "trader-day", "day.py"), step,
                                                   os.path.join(root, "trader", "logs"))
        out.append(("%s %s" % (TASK_PREFIX, step), at, cmd))
    return out


def cmd_schedule(a, now, cfg):
    tasks = schedule_tasks(a.python or sys.executable, ROOT)
    if a.action == "show" or os.name != "nt":
        for name, at, cmd in tasks:
            print("%s  weekdays %s  %s" % (name, at, cmd))
        if os.name != "nt":
            print("Not Windows: put these in crontab -e instead, e.g. 45 8 * * 1-5 cd %s && python3 "
                  "trader-day/day.py morning --log trader/logs" % ROOT)
        return 0
    for name, at, cmd in tasks:
        if a.action == "install":
            argv = ["schtasks", "/Create", "/TN", name, "/TR", cmd, "/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI",
                    "/ST", at, "/F"]
        else:
            argv = ["schtasks", "/Delete", "/TN", name, "/F"]
        if a.dry_run:
            print("$ " + " ".join(argv))
            continue
        p = subprocess.run(argv, capture_output=True, text=True)
        print("%s %s: %s" % (a.action, name, (p.stdout or p.stderr).strip().splitlines()[-1] if (p.stdout or p.stderr) else p.returncode))
        if p.returncode != 0:
            return 1
    if a.action == "install":
        print("Installed. They run while you are signed in and the computer is awake; each skips weekends and NSE "
              "holidays itself and logs to trader/logs/DATE.log. Entries still wait for you: the gate step stops "
              "at the breakouts. Remove with: day.py schedule remove")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="The Day: the conductor for the trader skills (trader-day, NSE).")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--now", help='pretend it is this IST time, "YYYY-MM-DD HH:MM"')
    common.add_argument("--config", default=FILES["config"])
    common.add_argument("--dry-run", action="store_true", help="print the commands a step would run")
    common.add_argument("--root", action="store_true", help="work from the project folder (for scheduled runs)")
    common.add_argument("--log", metavar="DIR", help="append everything to DIR/DATE.log (for scheduled runs)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("status", parents=[common], help="where the day stands and the next step")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-token", action="store_true", help="skip the INDstocks token check (live runs check it)")
    p = sub.add_parser("morning", parents=[common], help="the universe (if missing), the daily scan, news on the hits")
    p.add_argument("--universe", action="store_true", help="rebuild trader/universe.txt first")
    p.add_argument("--show", type=int, default=15)
    p = sub.add_parser("gate", parents=[common], help="breadth; the opening-range scan when the gate opens")
    p.add_argument("--offline", metavar="BUNDLE", help="a saved 5-minute bundle instead of live data")
    p.add_argument("--daily", metavar="BUNDLE", help="a saved daily bundle for the trend flag (offline)")
    p.add_argument("--show", type=int, default=20)
    p = sub.add_parser("watch", parents=[common], help="alerts, then the paper ledger's tick")
    p.add_argument("--price", help="[SYMBOL=]PRICE for the tick instead of a quote")
    p = sub.add_parser("evening", parents=[common], help="save the bars, replay the universe, write the journal")
    p.add_argument("--note", action="append", help="a journal note (repeatable)")
    p.add_argument("--refetch", action="store_true", help="fetch the bars again even if saved")
    p = sub.add_parser("schedule", parents=[common], help="the unattended weekday runs in Windows Task Scheduler")
    p.add_argument("action", choices=["show", "install", "remove"])
    p.add_argument("--python", help="the Python to run (default: this one)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    global LOG
    if a.root:
        os.chdir(ROOT)
    if a.log:
        os.makedirs(a.log, exist_ok=True)
        LOG = open(os.path.join(a.log, "%s.log" % now_ist(a.now).date().isoformat()), "a", encoding="utf-8")
        sys.stdout = sys.stderr = LOG
        print("\n######## day.py %s at %s IST" % (a.cmd, now_ist().strftime("%Y-%m-%d %H:%M:%S")))
    try:
        now = now_ist(a.now)
        cfg = read_json(a.config) or {}
        if a.cmd in ("morning", "gate", "watch", "evening"):
            open_day, why = trader_data.trading_day(now.date())
            if not open_day:
                print("No session today (%s): nothing to run." % why)
                return 0
        return {"status": cmd_status, "morning": cmd_morning, "gate": cmd_gate, "watch": cmd_watch,
                "evening": cmd_evening, "schedule": cmd_schedule}[a.cmd](a, now, cfg)
    except DayError as e:
        print("day.py: %s" % e, file=sys.stderr)
        return 2
    finally:
        if LOG:
            sys.stdout.flush()
            LOG.close()
            LOG = None
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__


if __name__ == "__main__":
    sys.exit(main())

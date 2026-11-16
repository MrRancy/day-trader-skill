---
name: trader-day
description: >-
  The Day: the conductor for the eleven trader skills on NSE. Knows the IST
  clock, the state files in trader/ and the order of the chain (scanner,
  news, breadth gate, entry, exit, time machine, budget, risk, broker,
  alerts, the evening replay, the journal), says where the day stands and
  what the next step is, and runs the mechanical steps itself: the morning
  scan and news, the 10:00 breadth gate and the opening-range scan, the
  alert-and-ledger watch while a trade is open, the evening fetch, replay
  and journal. Stops at every judgment: which chart, the stop, the yes.
  Use when the user says "start the day", "what's next", "where are we",
  "run the morning", "check the gate", "watch the trade", "close out the
  day", "do the evening", "/trader-day", or at any point of a paper-trading
  day when they want the next step rather than a specific tool.
argument-hint: "[status | morning | gate | watch | evening]"
---

# trader-day

Eleven skills, one day. This one keeps the order and the clock so neither
of us has to. It reads the files the others write, says what has happened
and what comes next, and runs the steps that need no judgment by calling
the other skills' scripts with the routine's own flags. It never picks a
chart, never sets a stop, never says yes, never places an order.

Tool in this folder (standard library Python; the others' scripts do the work):

```bash
python3 "${CLAUDE_SKILL_DIR}/day.py" status                 # where the day stands, and the next command
python3 "${CLAUDE_SKILL_DIR}/day.py" morning                # 08:45
python3 "${CLAUDE_SKILL_DIR}/day.py" gate                   # 10:00
python3 "${CLAUDE_SKILL_DIR}/day.py" watch                  # every few minutes while a trade is open
python3 "${CLAUDE_SKILL_DIR}/day.py" evening --note "..."   # after 15:30
python3 "${CLAUDE_SKILL_DIR}/day.py" morning --dry-run      # the commands a step would run
```

## The day, phase by phase

| phase (IST) | what the conductor does | what stays with the user and Claude |
| --- | --- | --- |
| before 08:45 | nothing | sleep |
| 08:45 pre-market | `morning`: the token check first, the universe if missing, `scan.py daily`, `news.py` on the hits | read the hits and the news, take nothing |
| 09:15 to the gate | nothing, or `scan.py orb` to watch | watch the early breakouts, take nothing |
| the gate (10:00) | `gate`: `scan.py breadth`; if open, `scan.py orb` | gate closed: a watching day. Open: pick ONE chart |
| entry window to 11:00 | `status` names the next tool each time | `/trader-entry`, `/trader-exit` (2R), `/trader-timemachine`, `/trader-budget`, `/trader-risk`, then `/trader-broker` shows the ticket and waits for the yes |
| a trade is open | `watch`: `alerts.py check`, `ticket.py paper tick` | read the alerts; a second trade only after the first closed green, before 11:00 |
| 15:15 flat by | `status` says what is still open | `ticket.py flatten`, risk, the yes |
| after 15:30 | `evening`: `scan.py orb --save` for every chart, `timemachine.py universe --compare --write`, `check.py journal --note` | write the notes: what went wrong, in plain words |
| Friday | | `check.py review --days 7` |

The clock boundaries come from `trader/config.json`: `gate_time`,
`no_entries_after`, `flat_by`. `status` reads the breadth reading, the
watchlist, the entry, the plan, the Time Machine, the orders, the risk
verdicts, the paper ledger's positions and today's closed trades, the
saved bars, the replay and the journal, and names exactly one next step.

The evening also scores the pre-registered tests on the sessions saved since
2026-11-17 (`timemachine.py registered --write trader/replays/registered.json`).

While the config has `entries_paused` (set 2026-11-17: the long breakout is
retired), the morning, the gate and the evening still run and save
everything, the status says "Paper entries are paused", the gate step says
there is nothing to pick, and trader-risk blocks any entry. The evening's
saved bars are what the registered tests are scored on, so keep it running.

## The unattended day

```bash
python3 "${CLAUDE_SKILL_DIR}/day.py" schedule show       # the three tasks it would create
python3 "${CLAUDE_SKILL_DIR}/day.py" schedule install    # Windows Task Scheduler, weekdays
python3 "${CLAUDE_SKILL_DIR}/day.py" schedule remove
```

Three weekday tasks run `day.py morning` at 08:45, `day.py gate` at 10:01 and
`day.py evening` at 15:40, from the project folder (`--root`), each appending
to `trader/logs/DATE.log` (`--log`). Each step skips weekends and NSE
holidays itself and checks the token first; with the TOTP lines in `.env` a
dead token is replaced in memory (trader-broker, "The automatic token"). The
gate step stops at the breakouts: picking a chart, the stop and the yes still
wait for the user, so nothing is placed by a schedule. The tasks run while
the user is signed in and the computer is awake.

## The rules

- **One step at a time.** `status` names one next command. Run it, read it,
  run `status` again. No step is skipped, and the chain's order (entry
  before exit before Time Machine before budget before risk before the
  ticket) is the conductor's job to hold.
- **The conductor never decides a trade.** Which chart, which stop, whether
  the odds are good enough, and the yes: those are the user's, with the
  skill for that step. The conductor says "pick ONE chart" and stops.
- **Limits are read, not enforced here.** trader-risk enforces the gate,
  the trade count, the loss cap and stop-after-loss. The conductor only
  reports them so the next step is sensible.
- **Every step is another skill's script with the routine's flags.** Nothing
  is reimplemented; `--dry-run` prints the commands so they can be run by
  hand.
- **Exit code 1 from a step is a finding, not a failure:** no setups, a
  closed gate, nothing new. The conductor says so and goes on. Any other
  non-zero code stops it with the message on screen.
- **The token first.** A live `status` and every `morning` start with one
  INDstocks call. The token expired overnight on 2026-11-06 although it had
  been good at 20:46 the evening before: its life is tied to the day, not to
  24 hours from generation, so regenerate it before 08:45, every day.

## Example: a watching day

```
2026-11-05 10:01 IST: entry window.
  scan      daily, 88 hits (2026-11-04 close)
  gate      closed, 48.8% at 10:00
  entry     none
  plan      none
  machine   not run
  orders    none
  ledger    no position
  evening   bars no, replay no, journal no
Next: The gate is closed (48.8% at 10:00): a watching day.  ->  day.py gate again later in the window, else day.py evening
```

Then in the evening: `day.py evening --note "gate closed at 49%; watched,
took nothing"` saves the bars, replays the rule on every chart (the gate
sat the day out, the baseline lost Rs 34,618 after charges), writes the
journal, and `status` reads "The day is done."

## What it never does

- Never places, approves or modifies an order; never calls a broker.
- Never picks a symbol, a stop, a target or a size.
- Never runs a live step the user did not ask for: each command is one
  phase, started by the user.
- Never skips the risk check or the yes.

## Safety

Education and tooling, not financial advice. Nothing places an order
except `/trader-broker`, after `/trader-risk` passes and the user types an
explicit yes for that order. The paper ledger is the default; the config's
`"paper": true` keeps it that way.

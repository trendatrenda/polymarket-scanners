"""
pm_hourly.py  --  what GitHub runs every 2 hours: the rewards scanner snapshot, then one
paper market-maker cycle, reading Polymarket's market list and order books only ONCE
(that halves the run time and the GitHub minutes).

    python pm_hourly.py

Exits with an error (so the GitHub run shows RED) if either part was unhealthy; the
results of the healthy part are saved either way. Before anything, the minutes guard
(pm_minutes.py) decides whether this run fits the month's GitHub minutes.
"""
import sys
import time
import traceback

import pm_minutes
import pm_mm_paper as mm
import pm_rewards_scanner as rs


def main():
    started = time.time()
    skip, hard, msg = pm_minutes.should_skip()
    print(f"  GitHub minutes: {msg}")
    if skip:
        pm_minutes.finish(pm_minutes.start("skipped"))   # a skipped job is still billed
        print("  SKIPPED this run (minutes guard) -- see pm_minutes.csv")
        rs.log("minutes guard: " + msg)
        sys.exit(1 if hard else 0)                       # slowing down is normal; a stop is RED
    rs.HARD_DEADLINE[0] = started + 540                  # no request may start past 9 min
    key = pm_minutes.start("hourly")
    try:
        run(started)
    finally:
        pm_minutes.finish(key)


def run(started):
    bad = []
    pre = None
    try:
        mkts, books = rs.scan()
        if mkts and rs.failure_share() <= 0.2:
            pre = (mkts, books)
        else:
            bad.append("rewards scanner")
    except Exception:
        traceback.print_exc()
        rs.log("scanner crashed: " + traceback.format_exc()[-300:])
        bad.append("rewards scanner")
    try:
        # if the scanner's listing failed, don't read it all again (that's what failed):
        # the market maker still checks old windows, with no new quotes
        mm.cycle(pre if pre else ([], {}), started=started)
    except SystemExit as e:
        if e.code:
            if isinstance(e.code, str):
                print(e.code)
                rs.log("market maker stopped: " + e.code)
            bad.append("market maker")
    except Exception:
        traceback.print_exc()
        rs.log("market maker crashed: " + traceback.format_exc()[-300:])
        bad.append("market maker")
    if bad:
        print(f"  UNHEALTHY this run: {', '.join(bad)} (see pm_rewards_errors.txt)")
        sys.exit(1)


if __name__ == "__main__":
    main()

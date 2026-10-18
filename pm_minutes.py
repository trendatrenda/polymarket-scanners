"""
pm_minutes.py  --  keeps the GitHub free minutes (2,000 a month) from running out.

Both pm_hourly.py and pm_paper_bot.py write a row to pm_minutes.csv when they start and
fill in how long they took when they finish. A row that never got finished (the run was
killed) counts as 11 minutes. Every GitHub job also costs about 1 minute of setup.

Pacing, not a cliff: if this month's minutes are ahead of schedule (more than the
monthly limit x the share of the month gone), every other 2-hour run is skipped (2 in 3
when far ahead) (a
normal, GREEN run that says so -- and the summary shows the average run length, so slow
runs get noticed); at the hard limit nothing runs until next month and the run is RED.
A minutes file that can't be read is renamed to .bad<time>; for that month a pessimistic
6 minutes per 2-hour slot up to the damage is assumed for what was lost.
"""
import csv
import math
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "pm_minutes.csv"
FIELDS = ["start", "program", "seconds"]
MONTH_LIMIT = 1700            # of the 2,000 free minutes (the rest is a safety margin)
KILLED_MIN = 11               # a run with no finish time was probably killed at the limit


class Broken(Exception):
    pass


def _rows():
    """All rows. A missing file is an empty month; a file that can't be READ is renamed
    to .bad and raises Broken -- it is never silently overwritten (that would reset the
    month's count and switch the guard off)."""
    if not LOG.exists():
        return []
    try:
        with LOG.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error, UnicodeDecodeError) as e:
        try:
            LOG.replace(LOG.with_suffix(f".bad{int(time.time())}"))
        except OSError:
            pass
        raise Broken(f"{LOG.name} could not be read ({type(e).__name__}) -- renamed to .bad")


def _write(rows):
    tmp = LOG.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(LOG)


def start(program):
    """Record a run starting; returns its key for finish()."""
    key = f"{time.time():.3f}"
    try:
        rows = _rows()
        rows.append({"start": key, "program": program, "seconds": ""})
        _write(rows)
    except (OSError, Broken):
        pass
    return key


def finish(key):
    try:
        rows = _rows()
        for r in rows:
            if r.get("start") == key:
                r["seconds"] = str(round(time.time() - float(key)))
        _write(rows)
    except (OSError, ValueError, Broken):
        pass


def used_this_month():
    """Estimated billed minutes so far this month (rounded UP per run, +1 per job).
    If the log can't be read, the WHOLE budget is assumed used (stop, loudly)."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    total = 0
    try:
        rows = _rows()
    except Broken:
        rows = []
    floor = 0
    for bad in LOG.parent.glob(LOG.stem + ".bad*"):
        # a log damaged THIS month (date from the file NAME -- file dates are reset by every
        # GitHub checkout): what was used before is unknown, so assume a pessimistic 6
        # minutes for every 2-hour slot of the month up to the damage, and let pacing work
        try:
            when = datetime.fromtimestamp(int(bad.suffix[4:] or 0), timezone.utc)
        except (ValueError, OverflowError, OSError):
            continue
        if when.strftime("%Y-%m") == month:
            first = when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            floor = max(floor, int((when - first).total_seconds() // 7200 + 1) * 6)
    for r in rows:
        try:
            t = datetime.fromtimestamp(float(r["start"]), timezone.utc)
        except (KeyError, TypeError, ValueError):
            total += KILLED_MIN                      # unreadable: count it as expensive
            continue
        if t.strftime("%Y-%m") != month:
            continue
        try:
            total += math.ceil(float(r["seconds"]) / 60) if r.get("seconds") else KILLED_MIN
        except ValueError:
            total += KILLED_MIN
        if r.get("program") in ("hourly", "skipped"):
            total += 1                               # GitHub setup + saving, once per job
    return total + floor


def average_seconds(program="hourly", last=50):
    """Average length of the last finished runs of one program (None if none yet)."""
    try:
        xs = [float(r["seconds"]) for r in _rows()
              if r.get("program") == program and r.get("seconds")][-last:]
    except (Broken, ValueError):
        return None
    return sum(xs) / len(xs) if xs else None


def should_skip():
    """(skip, hard, message). Paces the month instead of stopping like a cliff:
    hard=True means the monthly limit is reached (a RED stop); a pacing skip is normal."""
    now = datetime.now(timezone.utc)
    used = used_this_month()
    if used >= MONTH_LIMIT:
        return True, True, f"about {used} GitHub minutes used this month (limit {MONTH_LIMIT})" \
                           f" -- nothing runs until next month"
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (first.replace(month=first.month % 12 + 1, year=first.year + (first.month == 12)))
    gone = (now - first).total_seconds() / (nxt - first).total_seconds()
    slot = int(now.timestamp() // 7200)
    far = used > MONTH_LIMIT * gone * 1.25                # far ahead: run 1 slot in 3
    if used > MONTH_LIMIT * gone and (slot % 3 != 0 if far else slot % 2 == 1):
        return True, False, f"about {used} minutes used, ahead of schedule " \
                            f"({MONTH_LIMIT * gone:.0f} by now) -- this run is skipped to slow down"
    return False, False, f"about {used} minutes used this month"

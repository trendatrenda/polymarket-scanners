"""
pm_rewards_scanner.py  --  Polymarket idea #34, STEP 1: are LIQUIDITY REWARDS worth it?
======================================================================================
Polymarket pays a daily reward pot in many markets to people who keep orders
resting near the middle price (no trade needed). Your share depends on how big
and how close your orders are compared with everyone else's.
This scanner asks: if we put $100 into two-sided orders in a rewarded market,
roughly how much of the pot would we get per day?  It only READS -- no wallet,
no keys, no orders.

    python pm_rewards_scanner.py                one snapshot
    python pm_rewards_scanner.py --every 30     every 30 minutes until Ctrl+C
    python pm_rewards_scanner.py --summary      what the snapshots say so far

Polymarket's published formula (docs.polymarket.com/market-makers/liquidity-rewards):
  an order's score  S = ((v - s) / v)^2 x size     v = max spread (cents), s = its distance
                                                   from the middle price (cents)
  side scores Qone (Yes bids + No asks) and Qtwo (Yes asks + No bids); a two-sided
  maker scores min(Qone, Qtwo); the pot is split by score.

THE LOCKED RULES (agreed before the first run)
  Our quote   $100: buy "Yes" at (middle - d) AND buy "No" at (1 - middle - d), the
              same number of shares each side (that's a two-sided quote).
              Two settings: d = 1 cent (tight) and d = half the max spread (relaxed).
              Must meet the market's minimum size, middle price 10-90%.
  Others      everyone else's qualifying orders now in the book. Order books show
              price LEVELS, not single makers, so we give three estimates of their
              total score: low (min of both sides), mid (average), high (max).
  Our reward  pot per day x our score / (our score + others' score)   [mid estimate]
  STEP 1 PASSES if, over about a week of snapshots, at least 5 different markets
              offer >= $0.50 per day for $100 (relaxed quote, mid estimate) in at
              least half of the snapshots where they appear.  Then STEP 2 (paper
              market-making with fills and losses) is worth building. If not, #34 stops.
  NOT counted yet: losses from being filled before a price move, and the extra
  maker rebates when filled. Step 2 measures those.
"""
import argparse
import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com"
LOG_CSV = HERE / "pm_rewards_log.csv"          # old single file (still read by --summary)
LOG_DIR = HERE / "rewards_logs"                 # new: one file per day (keeps files small)


def day_log(t):
    LOG_DIR.mkdir(exist_ok=True)
    return LOG_DIR / f"{t:%Y-%m-%d}.csv"


def all_logs():
    return ([LOG_CSV] if LOG_CSV.exists() else []) + sorted(LOG_DIR.glob("*.csv"))
LOG = HERE / "pm_rewards_errors.txt"
CAPITAL = 100.0
PAUSE = 0.15
BATCH = 100
MIN_DAYS_LEFT = 2        # markets ending sooner: insiders/live data pick off makers
FIELDS = ["time", "market_id", "question", "daily_pot", "max_spread_c", "min_size", "mid",
          "days_left", "book_spread_c", "competitors",
          "volume_24h", "others_low", "others_mid", "others_high",
          "tight_shares", "tight_usd_day", "relaxed_shares", "relaxed_usd_day",
          "relaxed_usd_day_low", "relaxed_usd_day_high", "too_small_for_100"]


def log(msg):
    with LOG.open("a") as f:
        f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def request_json(url, params=None, body=None):
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(4):
        try:
            req = urllib.request.Request(full, data=data, headers={
                "User-Agent": "Mozilla/5.0 (research)", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            log(f"HTTP {e.code} for {full}: {e.read().decode(errors='replace')[:200]}")
            return None
        except Exception as e:
            log(f"{type(e).__name__} for {full}: {e}")
            time.sleep(3 * (attempt + 1))
    return None


def get_json(url, params=None):
    return request_json(url, params)


def post_json(url, body):
    return request_json(url, body=body)


def as_list(x):
    if isinstance(x, list):
        return x
    try:
        return json.loads(x) if x else []
    except (TypeError, json.JSONDecodeError):
        return []


def num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def daily_pot(m, today):
    """Today's reward per day for this market (sum of active allocations)."""
    total = 0.0
    for r in m.get("clobRewards") or []:
        s, e = str(r.get("startDate") or "")[:10], str(r.get("endDate") or "")[:10]
        if (not s or s <= today) and (not e or e >= today):
            total += num(r.get("rewardsDailyRate"))
    return total


def rewarded_markets():
    """Open markets with a reward pot today (several sort orders, merged)."""
    today = date.today().isoformat()
    found = {}
    for order in ("volume24hr", "liquidity", None):
        offset = 0
        while offset < 2000:
            params = {"closed": "false", "active": "true", "limit": 100, "offset": offset}
            if order:
                params.update({"order": order, "ascending": "false"})
            page = get_json(GAMMA, params)
            if not isinstance(page, list) or not page:
                break
            for m in page:
                pot = daily_pot(m, today)
                toks = as_list(m.get("clobTokenIds"))
                if pot > 0 and len(toks) == 2 and m.get("acceptingOrders", True) is not False \
                        and num(m.get("rewardsMaxSpread")) > 0:
                    found[str(m.get("id"))] = (m, pot)
            if len(page) < 100:
                break
            offset += 100
            time.sleep(PAUSE)
    return list(found.values())


def books_for(tokens):
    out = {}
    for i in range(0, len(tokens), BATCH):
        chunk = tokens[i:i + BATCH]
        res = post_json(f"{CLOB}/books", [{"token_id": t} for t in chunk])
        for b in res if isinstance(res, list) else []:
            if isinstance(b, dict) and b.get("asset_id") is not None:
                out[str(b["asset_id"])] = b
        for t in chunk:
            if t not in out:
                b = get_json(f"{CLOB}/book", {"token_id": t})
                if isinstance(b, dict):
                    out[t] = b
                time.sleep(PAUSE)
        time.sleep(PAUSE)
    return out


def levels(book, side):
    try:
        return [(num(x["price"]), num(x["size"])) for x in (book or {}).get(side) or []]
    except (KeyError, TypeError):
        return []


def score(dist_c, v):
    return ((v - dist_c) / v) ** 2 if 0 <= dist_c < v else 0.0


def evaluate(m, pot, yes_book, no_book):
    """Others' score now, and what $100 two-sided would earn per day."""
    v, min_size = num(m.get("rewardsMaxSpread")), num(m.get("rewardsMinSize"))
    yb, ya = levels(yes_book, "bids"), levels(yes_book, "asks")
    nb, na = levels(no_book, "bids"), levels(no_book, "asks")
    # Polymarket's middle price ignores orders smaller than the minimum size
    yb_big = [(p, s) for p, s in yb if s >= min_size] or yb
    ya_big = [(p, s) for p, s in ya if s >= min_size] or ya
    if not yb or not ya:
        return None
    best_bid, best_ask = max(p for p, _ in yb_big), min(p for p, _ in ya_big)
    mid = (best_bid + best_ask) / 2
    if not 0.10 <= mid <= 0.90:
        return None

    def q(lv, ref):          # ref = the middle price for that token
        return sum(score(abs(p - ref) * 100, v) * s for p, s in lv if s >= min_size)

    # The "No" book can be a MIRROR of the "Yes" book (a Yes bid at 48c shown again as a
    # No ask at 52c). Then those are the same orders -- count them only once.
    def mirror(a, b):
        sa = {(round(1 - p, 4), round(s, 2)) for p, s in a}
        sb = {(round(p, 4), round(s, 2)) for p, s in b}
        return bool(sa) and sa <= sb
    na_x = [] if mirror(yb, na) else na
    nb_x = [] if mirror(ya, nb) else nb
    q_one = q(yb, mid) + q(na_x, 1 - mid)     # Yes bids + No asks
    q_two = q(ya, mid) + q(nb_x, 1 - mid)     # Yes asks + No bids
    others = (min(q_one, q_two), (q_one + q_two) / 2, max(q_one, q_two))

    res = {}
    for name, d_c in (("tight", 1.0), ("relaxed", v / 2)):
        d = d_c / 100
        cost_per_pair = (mid - d) + (1 - mid - d)            # one Yes share + one No share
        shares = CAPITAL / cost_per_pair if cost_per_pair > 0 else 0
        ours = score(d_c, v) * shares                        # two-sided -> min of sides
        res[name] = (shares, [pot * ours / (ours + o) if ours + o > 0 else 0 for o in others])
    return {"mid": mid, "v": v, "min_size": min_size, "others": others,
            "spread_c": (best_ask - best_bid) * 100,
            "yes_bid": max(p for p, _ in yb), "yes_ask": min(p for p, _ in ya), **res}


def scan():
    t0 = datetime.now(timezone.utc)
    clock = time.time()
    print(f"\n  [{datetime.now():%H:%M}] Finding markets with a reward pot today...")
    mkts = rewarded_markets()
    toks = [str(t) for m, _ in mkts for t in as_list(m.get("clobTokenIds"))]
    print(f"  {len(mkts)} rewarded markets ({sum(p for _, p in mkts):,.0f} $/day in pots). "
          f"Reading {len(toks):,} order books...")
    books = books_for(toks)
    rows = []
    for m, pot in mkts:
        ty, tn = [str(t) for t in as_list(m.get("clobTokenIds"))]
        e = evaluate(m, pot, books.get(ty), books.get(tn))
        if not e:
            continue
        try:
            end = datetime.fromisoformat(str(m.get("endDate")).replace("Z", "+00:00"))
            days_left = round((end - t0).total_seconds() / 86400, 1)
        except ValueError:
            days_left = ""
        ts, tu = e["tight"]
        rs, ru = e["relaxed"]
        rows.append({"time": t0.strftime("%Y-%m-%d %H:%M"), "market_id": m.get("id"),
                     "question": str(m.get("question") or "")[:90], "daily_pot": round(pot, 2),
                     "max_spread_c": e["v"], "min_size": e["min_size"], "mid": round(e["mid"], 3),
                     "days_left": days_left, "book_spread_c": round(e["spread_c"], 1),
                     "competitors": "yes" if e["others"][1] > 0 else "no",
                     "volume_24h": round(num(m.get("volume24hr")), 0),
                     "others_low": round(e["others"][0], 1), "others_mid": round(e["others"][1], 1),
                     "others_high": round(e["others"][2], 1),
                     "tight_shares": round(ts, 1), "tight_usd_day": round(tu[1], 3),
                     "relaxed_shares": round(rs, 1), "relaxed_usd_day": round(ru[1], 3),
                     "relaxed_usd_day_low": round(ru[2], 3), "relaxed_usd_day_high": round(ru[0], 3),
                     "too_small_for_100": "yes" if rs < e["min_size"] else "no"})
    log_csv = day_log(t0)
    if log_csv.exists():                          # older file with fewer columns -> upgrade it
        with log_csv.open(newline="") as f:
            rd = csv.DictReader(f)
            if rd.fieldnames != FIELDS:
                old = list(rd)
                with log_csv.open("w", newline="") as g:
                    w = csv.DictWriter(g, fieldnames=FIELDS, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(old)
    first = not log_csv.exists()
    with log_csv.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if first:
            w.writeheader()
        w.writerows(rows)
    ok = [r for r in rows if realistic(r)]
    W = 78
    print("=" * W)
    print(f" #34 LIQUIDITY REWARDS (step 1)  |  {datetime.now():%Y-%m-%d %H:%M}  |  $100, no real money")
    print("=" * W)
    print(f" Rewarded markets priced 10-90%: {len(rows)}   realistic for us: {len(ok)}"
          f"   ({round(time.time() - clock)}s)")
    print(f"   (realistic = $100 meets the minimum size, {MIN_DAYS_LEFT}+ days left, and other makers"
          f" already quote there)")
    empty = sum(r["competitors"] == "no" for r in rows)
    print(f"   skipped: {empty} with NOBODY quoting (usually ending today = risky), "
          f"{sum(r['days_left'] != '' and r['days_left'] < MIN_DAYS_LEFT for r in rows)} ending within"
          f" {MIN_DAYS_LEFT} days")
    print(f" Best 12 for a RELAXED $100 quote (mid estimate; range low-high):")
    for r in sorted(ok, key=lambda r: -r["relaxed_usd_day"])[:12]:
        print(f"   ${r['relaxed_usd_day']:>5.2f}/day (${r['relaxed_usd_day_low']:.2f}-"
              f"{r['relaxed_usd_day_high']:.2f})  tight ${r['tight_usd_day']:>5.2f}  "
              f"pot ${r['daily_pot']:>5.0f}  {r['days_left']:>5}d  {r['question'][:28]}")
    big = sum(r["relaxed_usd_day"] >= 0.5 for r in ok)
    print(f"\n Markets paying >= $0.50/day for $100 right now: {big}   (step 1 needs 5+, most of the week)")
    print(f" Saved to {LOG_DIR.name}/{log_csv.name}.  NOT included yet: losses when filled before a price move.")
    print("=" * W)


def realistic(r):
    try:
        dl = float(r["days_left"])
    except (TypeError, ValueError, KeyError):
        dl = 99
    return (r["too_small_for_100"] == "no" and dl >= MIN_DAYS_LEFT
            and r.get("competitors", "yes") == "yes")


def summary():
    if not all_logs():
        print("  Nothing saved yet -- run a scan first.")
        return
    rows = []
    for p in all_logs():
        with p.open(newline="") as f:
            rows += [r for r in csv.DictReader(f) if r.get("competitors") and realistic(r)]
    snaps = sorted({r["time"] for r in rows})
    per = {}
    for r in rows:
        g = per.setdefault(r["market_id"], {"q": r["question"], "seen": 0, "good": 0, "usd": []})
        g["seen"] += 1
        g["usd"].append(float(r["relaxed_usd_day"]))
        g["good"] += float(r["relaxed_usd_day"]) >= 0.5
    passing = [g for g in per.values() if g["good"] >= max(1, g["seen"] / 2)]
    W = 78
    print("=" * W)
    print(" #34 LIQUIDITY REWARDS -- STEP 1 SUMMARY ($100, relaxed quote, mid estimate)")
    print("=" * W)
    print(f" Snapshots: {len(snaps)} ({snaps[0] if snaps else '-'} -> {snaps[-1] if snaps else '-'} UTC)"
          f"   markets seen: {len(per)}")
    print(f" Markets paying >= $0.50/day in at least half their snapshots: {len(passing)}")
    for g in sorted(passing, key=lambda g: -np_median(g["usd"]))[:12]:
        print(f"   typical ${np_median(g['usd']):.2f}/day   seen {g['seen']:>3}x   {g['q'][:45]}")
    days = (datetime.strptime(snaps[-1], "%Y-%m-%d %H:%M") -
            datetime.strptime(snaps[0], "%Y-%m-%d %H:%M")).days if snaps else 0
    if days < 7:
        verdict = ("on track (5+ markets so far) -- wait for 7 days" if len(passing) >= 5
                   else "not on track yet -- wait for 7 days")
    else:
        verdict = "PASS" if len(passing) >= 5 else "NOT a pass"
    print(f"\n STEP 1 (needs ~7 days of snapshots and 5+ markets): {days} days so far -> {verdict}")
    print("=" * W)


def np_median(xs):
    s = sorted(xs)
    n = len(s)
    return (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2) if n else 0.0


def main():
    ap = argparse.ArgumentParser(description="#34 liquidity rewards, step 1 (read-only)")
    ap.add_argument("--every", type=float, default=0)
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    if a.summary:
        summary()
        return
    if not a.every:
        scan()
        return
    print(f"  Scanning every {a.every:g} minutes. Ctrl+C to stop.")
    try:
        while True:
            start = time.time()
            try:
                scan()
            except Exception as e:
                log(f"scan failed: {type(e).__name__}: {e}")
                print(f"  Scan failed ({type(e).__name__}) -- trying again next time.")
            wait = max(0, a.every * 60 - (time.time() - start))
            print(f"  Next scan at {datetime.fromtimestamp(time.time() + wait):%H:%M}.")
            time.sleep(wait)
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()

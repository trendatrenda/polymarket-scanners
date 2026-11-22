"""
pm_kalshi_match.py  --  #42 step 1: can we pair up the SAME question on Kalshi and Polymarket?
==============================================================================================
One-time run (no AI, $0, Kalshi used as DATA only -- we never trade there). Reads every open
non-sports market on both sites, pairs questions with nearly the same words AND an end date
within 3 days, and lists the pairs with both prices so a human can check them by eye.
If matching works, step 2 records how the two prices move over time (who follows whom).
Writes km_results.txt and km_pairs.csv.
"""
import csv
import re
import sys
import time
from datetime import datetime, timezone

import pm_rewards_scanner as rs
from pm_longshot_test import is_sport

GAMMA = "https://gamma-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
MIN_SCORE, MAX_DAYS, MAX_SPREAD = 0.45, 3.0, 0.10
STOP = set("will the a an of in on at by to be is for and or with than from this that does do before after "
           "end yes no any its as are was who what which".split())
LINES = []


def say(s=""):
    print(s, flush=True)
    LINES.append(s)


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


SAME = {"decrease": "cut", "lower": "cut", "reduce": "cut", "increase": "hike", "raise": "hike",
        "hold": "unchanged", "change": "unchanged", "pause": "unchanged", "interest": "", "rate": "",
        "decision": "", "meeting": "", "after": "", "announce": "", "bp": "bps"}


def norm(w):
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss") and not w[0].isdigit():
        w = w[:-1]
    return SAME.get(w, w)


def words(text):
    t = str(text or "").lower().replace(",", "")
    t = re.sub(r"(\d)\s*(bps|bp|%)", r"\1\2", t)
    out = {norm(w) for w in re.findall(r"[a-z]+|\d+(?:\.\d+)?%?|\d+bps?", t) if w not in STOP and (len(w) > 1 or w.isdigit())}
    return {w for w in out if w}


def key_numbers(ws):
    """Numbers that change the question (thresholds, amounts) -- years and day-of-month left out."""
    return {w for w in ws if w[0].isdigit() and not re.fullmatch(r"20\d\d|[0-3]?\d", w)}


def kalshi_price(m, side):
    """Kalshi gives cents (yes_bid) or dollars (yes_bid_dollars). Returns dollars or None."""
    v = f(m.get(f"{side}_dollars"))
    if v is None:
        c = f(m.get(side))
        v = c / 100 if c is not None else None
    return v


def kalshi_markets():
    out, cursor, why = [], "", {}
    for _ in range(200):
        p = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        got = rs.get_json(f"{KALSHI}/events", p)
        if not isinstance(got, dict):
            if not out:
                raise SystemExit("PROBLEM: Kalshi's event list did not load (see rewards_logs)")
            break
        for ev in got.get("events") or []:
            cat = str(ev.get("category") or "").lower()
            for m in ev.get("markets") or []:
                tk = str(m.get("ticker") or "")
                if "sport" in cat or tk.startswith("KXMVE"):
                    r = "sports / combo"
                else:
                    b, a = kalshi_price(m, "yes_bid"), kalshi_price(m, "yes_ask")
                    end = ts(m.get("close_time") or m.get("expiration_time"))
                    if b is None or a is None or not 0 < b < a < 1 or a - b > MAX_SPREAD or not end:
                        r = "no tight price"
                    else:
                        r = "ok"
                        text = f"{ev.get('title', '')} {m.get('yes_sub_title') or m.get('subtitle') or ''}"
                        out.append({"site": "K", "id": tk, "text": text.strip()[:160], "w": words(text),
                                    "mid": (a + b) / 2, "end": end, "vol": f(m.get("volume")) or 0})
                why[r] = why.get(r, 0) + 1
        cursor = got.get("cursor") or ""
        if not cursor:
            break
    return out, why


def poly_markets():
    out, why = [], {}
    for page in range(240):
        got = rs.get_json(f"{GAMMA}/events", {"closed": "false", "active": "true", "limit": 50, "offset": page * 50})
        if not isinstance(got, list):
            if page == 0:
                raise SystemExit("PROBLEM: Polymarket's event list did not load")
            break
        if not got:
            break
        for ev in got:
            for m in ev.get("markets") or []:
                if is_sport(m):
                    r = "sports"
                else:
                    b, a, end = f(m.get("bestBid")), f(m.get("bestAsk")), ts(m.get("endDate"))
                    if b is None or a is None or not 0 < b < a < 1 or a - b > MAX_SPREAD or not end \
                            or m.get("closed") or not m.get("active"):
                        r = "no tight price"
                    else:
                        r = "ok"
                        text = f"{m.get('question', '')} {m.get('groupItemTitle') or ''}"
                        out.append({"site": "P", "id": str(m.get("id")), "text": text.strip()[:160],
                                    "w": words(text), "mid": (a + b) / 2, "end": end,
                                    "vol": f(m.get("volumeNum")) or 0})
                why[r] = why.get(r, 0) + 1
        if len(got) < 50:
            break
    return out, why


def match(P, K):
    idx = {}
    for i, k in enumerate(K):
        for w in k["w"]:
            idx.setdefault(w, []).append(i)
    pairs = []
    for p in P:
        cand = {}
        for w in p["w"]:
            if len(idx.get(w, [])) > 400:          # very common word: no help in matching
                continue
            for i in idx.get(w, []):
                cand[i] = cand.get(i, 0) + 1
        best, bs = None, 0.0
        for i, shared in cand.items():
            k = K[i]
            if abs(k["end"] - p["end"]) > MAX_DAYS * 86400:
                continue
            if key_numbers(p["w"]) != key_numbers(k["w"]):
                continue                              # different thresholds -> not the same question
            score = shared / len(p["w"] | k["w"])
            if score > bs:
                best, bs = k, score
        if best and bs >= MIN_SCORE:
            pairs.append((bs, p, best))
    return sorted(pairs, key=lambda x: -x[0])


def main():
    rs.HARD_DEADLINE[0] = time.time() + 40 * 60
    K, kw = kalshi_markets()
    P, pw = poly_markets()
    say(f"#42 KALSHI <-> POLYMARKET MATCHING   {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    say(f"Kalshi markets usable: {len(K)}   ({kw})")
    say(f"Polymarket markets usable: {len(P)}   ({pw})")
    pairs = match(P, K)
    gaps = [abs(p["mid"] - k["mid"]) for _, p, k in pairs]
    say(f"pairs found (score >= {MIN_SCORE}): {len(pairs)}")
    if gaps:
        gaps_s = sorted(gaps)
        say(f"price gap between the two sites: median {gaps_s[len(gaps_s) // 2] * 100:.1f}c   "
            f"over 5c: {sum(g > 0.05 for g in gaps)}   over 10c: {sum(g > 0.10 for g in gaps)}")
    say("\nTOP PAIRS (check by eye: is it really the same question?)")
    for s, p, k in pairs[:60]:
        say(f"  score {s:.2f}  P {p['mid'] * 100:5.1f}c  K {k['mid'] * 100:5.1f}c  gap {abs(p['mid'] - k['mid']) * 100:4.1f}c")
        say(f"      P: {p['text'][:110]}")
        say(f"      K: {k['text'][:110]}")
    with open("km_pairs.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["score", "poly_id", "poly_text", "poly_mid", "kalshi_ticker", "kalshi_text", "kalshi_mid", "gap"])
        for s, p, k in pairs:
            w.writerow([round(s, 3), p["id"], p["text"], round(p["mid"], 4), k["id"], k["text"], round(k["mid"], 4),
                        round(p["mid"] - k["mid"], 4)])
    say(f"\nrequests: {rs.STATS}")
    open("km_results.txt", "w").write("\n".join(LINES) + "\n")


if __name__ == "__main__":
    sys.exit(main())

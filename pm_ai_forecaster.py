"""
pm_ai_forecaster.py  --  #38 AI FORECASTER paper test (forward only, new markets only)
=====================================================================================
Runs with the other bots on GitHub. NO wallet, NO real bets. It needs ONE secret:
ANTHROPIC_API_KEY (a GitHub Secret -- never in a file, never printed).
    python pm_ai_forecaster.py              (normal run)
    python pm_ai_forecaster.py --summary    (score so far, no AI calls, no cost)

LOCKED RULES (set 26 Oct 2026, before any forecast was made)
  Markets   open Yes/No markets ending 7-30 days from now, most traded first, Yes price
            between 5% and 95%; no sports games and no crypto/stock-price markets.
            Each market is forecast ONCE. At most 20 new forecasts per 24 hours.
  News      the latest headlines about the question from Google News (free public feed):
            title, site, date. Only what was published before the forecast.
  The AI    Claude Sonnet 5.5 reads the question, its exact rules and the headlines, and
            gives the chance that "Yes" happens. It is NEVER shown the market's price.
  The bet   $20 at the REAL order book (the actual asks, level by level, + the market's
            own fee): buy Yes if the AI's chance beats the Yes cost by 8+ points, buy No
            if the AI's "No" chance beats the No cost by 8+ points. Held to the end.
  Verdict   after 200+ settled forecasts: PASS only if the AI's chances were MORE accurate
            than the market's (Brier) AND the paper bets made a profit. Else retire.
  Cost cap  stops calling the AI if this month's estimated API cost reaches $15.
Files: ai_forecasts.csv (every forecast), ai_bets.csv (paper bets), ai_cost.csv
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import pm_rewards_scanner as rs
from pm_euro_paper import STAKE, fee_rate_of, mid_price, taker_cost

HERE = Path(__file__).resolve().parent
FC, BETS, COST = HERE / "ai_forecasts.csv", HERE / "ai_bets.csv", HERE / "ai_cost.csv"
GAMMA = "https://gamma-api.polymarket.com"
NEWS = "https://news.google.com/rss/search"
API = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-5-5"
PRICE_IN, PRICE_OUT = 2.0, 10.0          # $ per million tokens (Sonnet 5.5 list price)
PER_DAY, EDGE, MONTH_CAP = 20, 0.08, 15.0
RUN_SECONDS = 300
F_FIELDS = ["time", "market_id", "cid", "question", "end", "q", "mid_yes", "ask_yes", "ask_no",
            "decision", "headlines", "reason", "result"]
B_FIELDS = ["placed", "market_id", "cid", "question", "end", "side", "token", "q", "avg_ask", "fee",
            "cost", "shares", "status", "settled", "payout", "pnl"]
SKIP = re.compile(r"\bvs\.?\b|\bwin (the|on)\b.*(game|match)|\b(nba|nfl|nhl|mlb|ufc|epl|premier league|"
                  r"champions league|la liga|serie a|bundesliga|ligue 1|grand prix|f1|tennis|golf|cricket|"
                  r"o/u|spread|points|goals|touchdown)\b|\b(bitcoin|btc|ethereum|eth|solana|xrp|doge|crypto|"
                  r"price of|close (above|below)|above \$|below \$|between \$|s&p 500|nasdaq|dow jones|"
                  r"stock price|market cap)\b", re.I)


def now():
    return datetime.now(timezone.utc)


def iso(d):
    return d.isoformat(timespec="seconds")


def read(p):
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def write(p, fields, rows):
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(p)


# ---------------------------------------------------------------------------- markets
def candidates(done_ids):
    t = now()
    out = []
    for page in range(4):
        ms = rs.get_json(f"{GAMMA}/markets", {"active": "true", "closed": "false", "limit": 100,
                                              "offset": page * 100, "order": "volume24hr", "ascending": "false",
                                              "end_date_min": iso(t + timedelta(days=7)),
                                              "end_date_max": iso(t + timedelta(days=30))})
        ms = [m for m in (ms if isinstance(ms, list) else []) if isinstance(m, dict)]
        if not ms:
            break
        for m in ms:
            q = str(m.get("question") or "")
            outs = [str(o).lower() for o in rs.as_list(m.get("outcomes"))]
            toks = rs.as_list(m.get("clobTokenIds"))
            if str(m.get("id")) in done_ids or outs != ["yes", "no"] or len(toks) != 2 or SKIP.search(q):
                continue
            if not m.get("acceptingOrders", True) or m.get("closed"):
                continue
            try:
                end = datetime.fromisoformat(str(m.get("endDate")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if not (t + timedelta(days=7) <= end <= t + timedelta(days=30)):
                continue
            out.append(m)
        time.sleep(0.15)
    return out


# ---------------------------------------------------------------------------- news
def search_words(q):
    q = re.sub(r"^(will|is|are|does|do|can|has|have|did)\s+", "", q.strip(), flags=re.I)
    q = re.sub(r"\b(by|before|on|in)\s+(january|february|march|april|may|june|july|august|september|october|"
               r"november|december)\b.*$", "", q, flags=re.I)
    return re.sub(r"[?\"'“”]", "", q).strip()[:120]


def headlines(q, limit=15):
    url = NEWS + "?" + urllib.parse.urlencode({"q": search_words(q), "hl": "en-US", "gl": "US", "ceid": "US:en"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (research)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            root = ET.fromstring(r.read())
    except Exception:
        return None                                        # feed failed (counted, reported)
    items = []
    t = now()
    for it in root.iter("item"):
        try:
            d = parsedate_to_datetime(it.findtext("pubDate"))
        except (TypeError, ValueError):
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if d > t:
            continue                                       # never anything "from the future"
        src = it.find("source")
        items.append((d, (it.findtext("title") or "").strip(), (src.text if src is not None else "") or ""))
    items.sort(key=lambda x: x[0], reverse=True)
    return items[:limit]


# ---------------------------------------------------------------------------- the AI
PROMPT = """You are a careful, well-calibrated forecaster. Today is {today} (UTC).

Question: {question}
This market closes: {end}

The exact resolution rules (these decide the answer, read them carefully):
{rules}

Recent news headlines (newest first; headlines can be incomplete or wrong):
{news}

Estimate the probability that this question resolves YES under the rules above.
Think about the base rate, the time left, what must happen for YES, and what the news
actually shows. Avoid being overconfident: use values near 0 or 1 only when the outcome
is nearly certain.

Answer with at most 4 short sentences of reasoning, then a final line exactly like:
PROBABILITY: 0.37"""


def ask_ai(key, m, news):
    body = {"model": MODEL, "max_tokens": 400, "temperature": 0,
            "messages": [{"role": "user", "content": PROMPT.format(
                today=now().strftime("%Y-%m-%d %H:%M"), question=m.get("question"), end=m.get("endDate"),
                rules=str(m.get("description") or "(no rules text)")[:3000],
                news="\n".join(f"- {d:%Y-%m-%d} {t} ({s})" for d, t, s in news) or "(no headlines found)")}]}
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            j = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return None, f"API error {e.code}", 0, 0
    except Exception as e:
        return None, f"API error {type(e).__name__}", 0, 0
    text = "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text")
    u = j.get("usage") or {}
    found = re.findall(r"PROBABILITY:\s*([01](?:\.\d+)?)", text)
    q = float(found[-1]) if found else None
    if q is not None and not 0 <= q <= 1:
        q = None
    reason = re.sub(r"\s+", " ", text.split("PROBABILITY")[0]).strip()[:300]
    return q, reason, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)


def month_cost():
    m = now().strftime("%Y-%m")
    return sum(float(r["usd"]) for r in read(COST) if r["time"].startswith(m))


# ---------------------------------------------------------------------------- settle + run
def settle(fc, bets):
    ids = {r["market_id"] for r in fc if r["result"] == ""} | {b["market_id"] for b in bets if b["status"] == "open"}
    res = {}
    for mid in ids:
        m = rs.get_json(f"{GAMMA}/markets/{mid}")
        if not isinstance(m, dict) or not m.get("closed"):
            continue
        st = m.get("umaResolutionStatus")
        if st is not None and str(st).lower() != "resolved":
            continue
        px = [str(x) for x in rs.as_list(m.get("outcomePrices"))]
        if px in (["1", "0"], ["0", "1"]):
            res[mid] = (1 if px[0] == "1" else 0, [str(t) for t in rs.as_list(m.get("clobTokenIds"))])
        time.sleep(0.1)
    for r in fc:
        if r["result"] == "" and r["market_id"] in res:
            r["result"] = res[r["market_id"]][0]
    for b in bets:
        if b["status"] == "open" and b["market_id"] in res:
            y, toks = res[b["market_id"]]
            won = (y == 1) == (b["side"] == "yes")
            pay = float(b["shares"]) if won else 0.0
            b.update(status="won" if won else "lost", settled=iso(now()), payout=round(pay, 4),
                     pnl=round(pay - STAKE, 4))
    return fc, bets


def run():
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    fc, bets = read(FC), read(BETS)
    fc, bets = settle(fc, bets)
    write(FC, F_FIELDS, fc)
    write(BETS, B_FIELDS, bets)
    if not key:
        print("  ANTHROPIC_API_KEY is not set (GitHub Secret missing) -- no new forecasts this run")
        summary(fc, bets)
        sys.exit(1)                                     # turn the run red so it gets noticed
    spent = month_cost()
    recent = [r for r in fc if r["time"] >= iso(now() - timedelta(hours=24))]
    room = PER_DAY - len(recent)
    print(f"  this month's estimated AI cost: ${spent:.2f} (cap ${MONTH_CAP:.0f});"
          f" forecasts in the last 24 h: {len(recent)} of {PER_DAY}")
    if room <= 0 or spent >= MONTH_CAP:
        print("  nothing to do this run (daily limit reached or monthly cost cap)")
        summary(fc, bets)
        return
    cands = candidates({r["market_id"] for r in fc})[:room]
    print(f"  new markets to forecast: {len(cands)}")
    stop = time.time() + RUN_SECONDS
    feed_fail = 0

    def one(m):
        if time.time() > stop:
            return m, None
        news = headlines(str(m.get("question")))
        return m, news

    with ThreadPoolExecutor(max_workers=4) as pool:
        got = list(pool.map(one, cands))
    calls = []
    for m, news in got:
        if news is None:
            feed_fail += 1
            continue
        calls.append((m, news))

    def think(x):
        m, news = x
        if time.time() > stop or month_cost() >= MONTH_CAP:
            return m, news, (None, "skipped (time/cost)", 0, 0)
        return m, news, ask_ai(key, m, news)

    costs = read(COST)
    with ThreadPoolExecutor(max_workers=4) as pool:
        out = list(pool.map(think, calls))
    errors = 0
    for m, news, (q, reason, tin, tout) in out:
        usd = tin / 1e6 * PRICE_IN + tout / 1e6 * PRICE_OUT
        if tin or tout:
            costs.append({"time": iso(now()), "market_id": m.get("id"), "in": tin, "out": tout, "usd": round(usd, 5)})
        if q is None:
            errors += reason.startswith("API error")
            print(f"  {str(m.get('question'))[:60]}: no forecast ({reason[:40]})")
            continue
        toks = [str(t) for t in rs.as_list(m.get("clobTokenIds"))]
        fr = fee_rate_of(m)
        ay, fy, cy = taker_cost(toks[0], fr)
        an, fn, cn = taker_cost(toks[1], fr)
        mid = mid_price(toks[0])
        dec, pick = "no bet", None
        if mid != "" and not 0.05 <= mid <= 0.95:
            dec = "price outside 5-95%: forecast kept, no bet"
        else:
            if cy is not None and q - cy >= EDGE:
                pick = ("yes", toks[0], q, ay, fy, cy)
            if cn is not None and (1 - q) - cn >= EDGE and (pick is None or (1 - q) - cn > q - cy):
                pick = ("no", toks[1], 1 - q, an, fn, cn)
            if cy is None and cn is None:
                dec = "book too thin"
        row = {"time": iso(now()), "market_id": m.get("id"), "cid": m.get("conditionId"),
               "question": m.get("question"), "end": m.get("endDate"), "q": round(q, 4),
               "mid_yes": "" if mid == "" else round(mid, 4), "ask_yes": "" if ay is None else round(ay, 4),
               "ask_no": "" if an is None else round(an, 4), "headlines": len(news), "reason": reason, "result": ""}
        if pick:
            side, tok, qq, av, fee, cost = pick
            bets.append({"placed": row["time"], "market_id": m.get("id"), "cid": m.get("conditionId"),
                         "question": m.get("question"), "end": m.get("endDate"), "side": side, "token": tok,
                         "q": round(qq, 4), "avg_ask": round(av, 4), "fee": round(fee, 5), "cost": round(cost, 4),
                         "shares": round(STAKE / cost, 4), "status": "open", "settled": "", "payout": "", "pnl": ""})
            dec = f"BET {side.upper()} at {cost:.3f} (AI says {qq:.2f})"
        row["decision"] = dec
        fc.append(row)
        print(f"  {str(m.get('question'))[:60]}: AI {q:.2f} vs market {row['mid_yes']} -> {dec}")
    write(FC, F_FIELDS, fc)
    write(BETS, B_FIELDS, bets)
    write(COST, ["time", "market_id", "in", "out", "usd"], costs)
    if feed_fail:
        print(f"  news feed failed for {feed_fail} market(s) (they are tried again next run)")
    summary(fc, bets)
    if errors and errors == len(out):
        print("  UNHEALTHY: every AI call failed -- check the API key / credit")
        sys.exit(1)


def summary(fc=None, bets=None):
    fc = read(FC) if fc is None else fc
    bets = read(BETS) if bets is None else bets
    done = [b for b in bets if b["status"] in ("won", "lost")]
    pnl = sum(float(b["pnl"]) for b in done)
    print(f"\n  #38 AI FORECASTER: {len(fc)} forecasts, {len(bets)} paper bets ({len(done)} settled,"
          f" profit ${pnl:.2f}{f', {pnl / (len(done) * STAKE):+.1%} per bet' if done else ''})")
    sc = [r for r in fc if r["result"] != "" and r["mid_yes"] not in ("", None)]
    if sc:
        ba = sum((float(r["q"]) - float(r["result"])) ** 2 for r in sc) / len(sc)
        bm = sum((float(r["mid_yes"]) - float(r["result"])) ** 2 for r in sc) / len(sc)
        print(f"  accuracy on {len(sc)} settled markets (Brier, lower = better): AI {ba:.4f}   market {bm:.4f}")
    print(f"  this month's estimated AI cost: ${month_cost():.2f}")
    print(f"  verdict after 200+ settled forecasts ({len(sc)} so far): PASS only if the AI beats the"
          f" market's accuracy AND the bets made a profit")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", action="store_true")
    if ap.parse_args().summary:
        summary()
        return
    run()


if __name__ == "__main__":
    main()

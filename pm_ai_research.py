"""
pm_ai_research.py  --  #38b RESEARCH-FIRST AI forecaster, a team of agents (paper test)
======================================================================================
Runs with the other bots on GitHub. NO wallet, NO real bets. Needs the same secret as #38:
ANTHROPIC_API_KEY (GitHub Secret -- never in a file, never printed).
    python pm_ai_research.py              (normal run)
    python pm_ai_research.py --summary    (score so far; no AI calls, no cost)

WHY: #38 (one AI, headlines only) lost to the market: accuracy 0.29 vs 0.16, 0 of 19 bets
won. Its weak point was INFORMATION. #38b fixes that first.

LOCKED RULES (set 11 Nov 2026, before any forecast was made)
  Markets    open Yes/No markets ending 3-30 days from now, NEWEST first, small (under
             $250,000 traded in total), Yes price 5-95%, and a REAL order book (buying Yes
             + buying No costs at most $1.06 -- added 11 Nov after the first 8 forecasts
             all landed on brand-new markets with empty books; those 8 are not scored). No sports, no crypto/stock prices,
             no elections, no "how many posts/tweets" counts. One market per event, ever.
             At most 8 new markets per 24 hours.
  The team   for each market, 5 steps (the market's price is NEVER shown to any of them):
             1. RESEARCHER  searches the web (up to 3 searches, prediction-market sites
                            blocked) and writes a dated fact sheet with sources.
             2. RULES       spells out exactly what must happen for "Yes", what does not
                            count, and the deadline.
             3. YES SIDE    the strongest honest case for Yes.
             4. NO SIDE     the strongest honest case for No.
             5. JUDGE       reads all of it and gives the final chance + confidence.
             Steps 1-4 use the small cheap model, the judge uses Sonnet.
  The bet    $20 at the REAL order book (actual asks, level by level, + the market's own
             fee) when the judge's chance beats the cost by 8+ points AND confidence is
             medium or high AND the researcher actually found something.
  Risk       one bet per event; at most 2 open bets on the same topic (Middle East,
             Russia-Ukraine, Trump, AI/tech, ...); at most $300 in open bets in total.
             Held to the end.
  Verdict    first check after 50 settled markets, final after 150: PASS only if the judge
             is MORE accurate than the market (Brier) AND the bets made a profit.
  Cost cap   stops calling the AI when this month's estimated cost reaches $12.
Files: air_forecasts.csv (every forecast, with the fact sheet), air_bets.csv, air_cost.csv
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pm_ai_forecaster as base
import pm_rewards_scanner as rs
from pm_ai_forecaster import GAMMA, HERE, event_key, iso, now, read, write
from pm_euro_paper import STAKE, fee_rate_of, mid_price, taker_cost

FC, BETS, COST = HERE / "air_forecasts.csv", HERE / "air_bets.csv", HERE / "air_cost.csv"
API = "https://api.anthropic.com/v1/messages"
SMALL, BIG = "claude-haiku-5-5", "claude-sonnet-5-5"
PRICE = {SMALL: (0.10, 0.50), BIG: (2.0, 10.0)}          # $ per million tokens (in, out)
SEARCH_USD = 0.01                                        # $10 per 1,000 web searches
PER_DAY, EDGE, MONTH_CAP = 8, 0.08, 12.0
MAX_VOLUME, MAX_PER_TOPIC, MAX_OPEN_USD = 250_000, 2, 300.0
RUN_SECONDS = 400
BLOCKED = ["polymarket.com", "kalshi.com", "manifold.markets", "metaculus.com", "predictit.org",
           "polyguana.com", "predictiongenius.io", "futuresearch.ai", "oddschecker.com"]
F_FIELDS = ["time", "market_id", "event", "topic", "cid", "question", "end", "q", "confidence", "q_yes_side",
            "q_no_side", "mid_yes", "ask_yes", "ask_no", "decision", "searches", "facts", "rules_read",
            "reason", "rules_warning", "result", "team", "team_errors"]
TEAM = "v3"      # 13 Nov fix: the small agents "think" before answering and ran out of room at 350 words-ish
                 # (stop: max_tokens). Now they get SMALL_TOKENS. Only v3 rows count in the verdict.
SMALL_TOKENS = 2500                                    # room for thinking + a short answer (< $0.002 each)
B_FIELDS = ["placed", "market_id", "event", "topic", "cid", "question", "end", "side", "token", "q", "avg_ask",
            "fee", "cost", "shares", "status", "settled", "payout", "pnl"]
EXTRA_SKIP = re.compile(r"\b(election|elections|senate|house seat|governor|mayor|primary|runoff|nominee|"
                        r"midterm|ballot|referendum|parliament|seats?|poll|approval rating|"
                        r"tweets?|posts? from|truth social posts|how many|number of|"
                        r"wnba|ncaa|mvp|finals|championship|world cup|league|tournament|open winner)\b", re.I)
SPORT = re.compile(r"\bTDs?\b|touchdown|yards|receptions|rushing|passing|interception|sacks?\b|strikeouts?|"
                   r"home runs?|rebounds|assists|anytime|first (td|goal|basket|scorer)|\bwin on 20\d\d|"
                   r"\bvs\.?\b|\b(nfl|nba|mlb|nhl|mls|ufc|atp|wta|ipl|fc|sc|cf)\b|cowboys|seahawks|"
                   r"cricket|innings|wicket|\bruns\b|knockout|\bround \d|\bset \d|\bmap \d|esports?|"
                   r"\d\+ (goals|points|kills|tds)|to score|clean sheet|draw\b", re.I)


def is_sport(m):
    """Polymarket marks sports markets itself (game time / sports market type); the words
    are a second net."""
    return bool(m.get("sportsMarketType") or m.get("gameStartTime") or m.get("gameId")
                or SPORT.search(str(m.get("question") or "")))


TOPICS = [("middle east", r"iran|israel|gaza|hamas|hezbollah|hormuz|lebanon|syria|yemen|houthi|saudi"),
          ("russia-ukraine", r"russia|ukrain|putin|zelensk|kremlin|crimea"),
          ("china-taiwan", r"china|chinese|taiwan|xi jinping|beijing"),
          ("trump", r"trump|white house|executive order|pardon"),
          ("ai-tech", r"\bai\b|openai|gpt|anthropic|claude|gemini|nvidia|model|apple|google|meta|tesla|spacex"),
          ("economy", r"fed\b|rate cut|inflation|cpi|gdp|jobs report|recession|tariff"),
          ("entertainment", r"movie|box office|album|song|spotify|netflix|oscar|grammy|billboard")]


MAX_GAP = 0.06            # a market counts as tradeable only if buying Yes + buying No costs
                          # at most $1.06 together (a tight, real order book)


def tight(ask_yes, ask_no):
    try:
        return float(ask_yes) + float(ask_no) <= 1 + MAX_GAP + 1e-9
    except (TypeError, ValueError):
        return False


def tradeable(m):
    """Brand-new markets often have an EMPTY book (asks of 90-99c on both sides): nothing can
    be bought at a fair price and the 'market price' means nothing. Checked BEFORE any AI
    money is spent: first Polymarket's own best bid/ask, then the real book for $20."""
    try:
        bid, ask = float(m.get("bestBid")), float(m.get("bestAsk"))
    except (TypeError, ValueError):
        return False
    if not (0 < bid < ask < 1) or ask - bid > MAX_GAP or not 0.05 <= (bid + ask) / 2 <= 0.95:
        return False
    toks = [str(x) for x in rs.as_list(m.get("clobTokenIds"))]
    ay = taker_cost(toks[0], 0.0)[0]
    an = taker_cost(toks[1], 0.0)[0]
    return tight(ay, an)


WHY = {}                  # why markets were skipped in the last search (printed each run)


def topic_of(m):
    q = str(m.get("question") or "").lower()
    for name, pat in TOPICS:
        if re.search(pat, q):
            return name
    return "other:" + str(m.get("_event") or m.get("id"))


# ---------------------------------------------------------------------------- markets
def candidates(done_ids, used_events, want):
    t = now()
    out, used = [], set(used_events)
    why = WHY
    why.clear()
    for page in range(60):                                 # newest pages are mostly sports props:
        if len(out) >= want:                               # keep looking until we have enough
            break
        ms = rs.get_json(f"{GAMMA}/markets", {"active": "true", "closed": "false", "limit": 100,
                                              "offset": page * 100, "order": "startDate", "ascending": "false",
                                              "end_date_min": iso(t + timedelta(days=3)),
                                              "end_date_max": iso(t + timedelta(days=30))})
        ms = [m for m in (ms if isinstance(ms, list) else []) if isinstance(m, dict)]
        if not ms:
            break
        for m in ms:
            q = str(m.get("question") or "")
            outs = [str(o).lower() for o in rs.as_list(m.get("outcomes"))]
            reason = ("already done" if str(m.get("id")) in done_ids else
                      "not a Yes/No market" if outs != ["yes", "no"] or len(rs.as_list(m.get("clobTokenIds"))) != 2 else
                      "sports" if is_sport(m) else
                      "crypto/stock price" if base.SKIP.search(q) else
                      "election/count" if EXTRA_SKIP.search(q) else
                      "not open" if not m.get("acceptingOrders", True) or m.get("closed") else "")
            why["looked at"] = why.get("looked at", 0) + 1
            if reason:
                why[reason] = why.get(reason, 0) + 1
                continue
            try:
                end = datetime.fromisoformat(str(m.get("endDate")).replace("Z", "+00:00"))
                vol = float(m.get("volumeNum") or m.get("volume") or 0)
            except (TypeError, ValueError):
                continue
            if not (t + timedelta(days=3) <= end <= t + timedelta(days=30)) or vol > MAX_VOLUME:
                why["too big / wrong dates"] = why.get("too big / wrong dates", 0) + 1
                continue
            k = event_key(m)
            if k in used:
                why["same event as another"] = why.get("same event as another", 0) + 1
                continue
            if not tradeable(m):
                why["no real order book yet"] = why.get("no real order book yet", 0) + 1
                continue
            used.add(k)
            m["_event"] = k
            m["_topic"] = topic_of(m)
            out.append(m)
        time.sleep(0.1)
    return out


# ---------------------------------------------------------------------------- talking to the AI
def call(key, model, prompt, max_tokens, search=False):
    """One request. Returns (text, usd, searches, error). The key is never printed."""
    msgs = [{"role": "user", "content": prompt}]
    body = {"model": model, "max_tokens": max_tokens, "messages": msgs}
    if search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3,
                          "blocked_domains": BLOCKED}]
    text, usd, searches, nudged, waited = "", 0.0, 0, False, False
    for _ in range(6):                                     # "pause_turn" = long search, continue
        req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=150) as r:
                j = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and not waited:        # "slow down" / "busy": wait, try once more
                waited = True
                try:
                    pause = min(60, max(30, int(e.headers.get("retry-after") or 30)))
                except (TypeError, ValueError):
                    pause = 30
                time.sleep(pause)
                continue
            try:
                msg = json.loads(e.read().decode()).get("error", {}).get("message", "")
            except Exception:
                msg = ""
            return text, usd, searches, f"API error {e.code}: {msg[:160]}"
        except Exception as e:
            return text, usd, searches, f"API error {type(e).__name__}"
        u = j.get("usage") or {}
        n = int((u.get("server_tool_use") or {}).get("web_search_requests") or 0)
        pin, pout = PRICE.get(model, PRICE[BIG])
        usd += (int(u.get("input_tokens") or 0) * pin + int(u.get("output_tokens") or 0) * pout) / 1e6
        usd += n * SEARCH_USD
        searches += n
        text += "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text")
        why = j.get("stop_reason")
        kinds = ",".join(sorted({str(c.get("type")) for c in j.get("content", [])})) or "nothing"
        if why == "pause_turn":
            body["messages"] = msgs + [{"role": "assistant", "content": j.get("content", [])}]
            continue
        if text.strip() or not search or nudged or not j.get("content"):
            return text, usd, searches, "" if text.strip() else f"empty reply (stop: {why}; got: {kinds})"
        nudged = True                                      # searched but wrote nothing: ask once
        body["messages"] = msgs + [{"role": "assistant", "content": j.get("content", [])},
                                   {"role": "user", "content": "Now write the fact sheet from what you found."}]
    return text, usd, searches, "" if text.strip() else "empty reply (search kept pausing)"


HEAD = ("Today is {today} (UTC). Your own training knowledge is OLDER than today: when it conflicts with "
        "dated, sourced information you are given or find, trust the newer information and treat it as real.\n\n"
        "Question: {question}\nMarket closes: {end}\n\nExact resolution rules:\n{rules}\n")

P_RESEARCH = HEAD + """
You are the RESEARCHER. Use web search (at most 3 searches, make each one count) to find the most
recent facts that decide this question: official statements, data, schedules, credible news.
Do NOT look for or report betting odds or prediction-market prices.

Write a FACT SHEET of at most 10 short bullet points. Every bullet: the fact, its date, the source name.
Then one line "STATUS:" -- where things stand right now.
Then one line "MISSING:" -- important things you could not find (or "nothing")."""

P_RULES = HEAD + """
You are the RULES READER. Do not forecast. In at most 5 short lines say:
YES NEEDS: exactly what must happen, by when (with time zone), according to which source.
DOES NOT COUNT: things people might wrongly think count.
IF DELAYED OR CANCELLED: what the rules say happens.
TRAP: the single detail most likely to surprise a careless trader (or "none")."""

P_SIDE = HEAD + """
Rules reading:
{rules_read}

Fact sheet from the researcher:
{facts}

You are the {side} SIDE. Give the strongest HONEST case that this resolves {side}, in at most 4
sentences, using only the facts above and well-known background. Then, on a last line, your own
honest estimate of the chance of YES (not advocacy), exactly like:
PROBABILITY: 0.37"""

P_JUDGE = HEAD + """
Rules reading:
{rules_read}

Fact sheet from the researcher ({searches} web searches):
{facts}

Case for YES:
{yes_case}

Case for NO:
{no_case}

You are the JUDGE, a careful and well-calibrated forecaster. Weigh the base rate (how often things
like this happen in this much time), the time left, the exact rules, and the facts. Do not split
the difference by habit; do not be overconfident. If the fact sheet is thin, say so with low
confidence. Answer with at most 4 short sentences, then exactly these three lines:
PROBABILITY: 0.37
CONFIDENCE: low|medium|high
RULES_WARNING: one sentence, or none"""


def prob(text):
    """Reads 'PROBABILITY: 0.37' -- also '**PROBABILITY:** 37%' and similar spellings."""
    f = re.findall(r"PROBABILITY\W{0,6}(\d{1,3}(?:\.\d+)?)\s*(%?)", str(text), re.I)
    if not f:
        return None
    q = float(f[-1][0])
    if f[-1][1] or q > 1:
        q /= 100
    return q if 0 <= q <= 1 else None


def one_line(s, n):
    return re.sub(r"\s+", " ", str(s)).strip()[:n]


def team(key, m, stop):
    """Runs the 5 agents for one market. Returns a dict (q None = no forecast) + cost."""
    ctx = {"today": now().strftime("%Y-%m-%d %H:%M"), "question": m.get("question"), "end": m.get("endDate"),
           "rules": str(m.get("description") or "(no rules text)")[:3500]}
    out = {"usd": 0.0, "searches": 0, "q": None, "note": ""}
    if time.time() > stop - 150:                           # not enough time for a full team: next run
        out["note"] = "not started (run time used up), tries again next run"
        return out

    def ask(model, tpl, max_tokens, search=False, **kw):
        if time.time() > stop:
            return "", "out of time"
        text, usd, n, err = call(key, model, tpl.format(**ctx, **kw), max_tokens, search)
        out["usd"] += usd
        out["searches"] += n
        return text, err

    facts, err = ask(SMALL, P_RESEARCH, 1600, search=True)
    if err.startswith("API error 400") and not facts:       # small model can't search: use Sonnet
        facts, err = ask(BIG, P_RESEARCH, 1600, search=True)
        out["note"] = "researcher ran on the big model; "
    if err or not facts.strip():
        out["note"] += f"research failed ({err or 'empty'})"
        return out
    errs = []
    rules_read, err = ask(SMALL, P_RULES, SMALL_TOKENS)
    if err or not rules_read.strip():
        errs.append(f"rules: {err or 'empty'}")
        rules_read = "(rules reader unavailable)"
    ys = ask(SMALL, P_SIDE, SMALL_TOKENS, rules_read=rules_read, facts=facts, side="YES")   # one after the other,
    ns = ask(SMALL, P_SIDE, SMALL_TOKENS, rules_read=rules_read, facts=facts, side="NO")    # not at the same time
    for name, (txt, e) in (("yes side", ys), ("no side", ns)):
        if e or not txt.strip():
            errs.append(f"{name}: {e or 'empty'}")
        elif prob(txt) is None:
            errs.append(f"{name}: no number")
    out["errors"] = one_line("; ".join(errs), 300)
    verdict, err = ask(BIG, P_JUDGE, 900, rules_read=rules_read, facts=facts, searches=out["searches"],
                       yes_case=ys[0] or "(unavailable)", no_case=ns[0] or "(unavailable)")
    if err or prob(verdict) is None:
        out["note"] += f"judge failed ({err or 'no number; reply ended: ' + one_line(verdict[-120:], 120)})"
        return out
    c = re.findall(r"CONFIDENCE\W{0,6}(low|medium|high)", verdict, re.I)
    w = re.findall(r"RULES_WARNING\W{0,4}\s*(.+)", verdict)
    warn = one_line(w[-1], 250) if w else ""
    out.update(q=prob(verdict), confidence=c[-1].lower() if c else "unknown",
               warn="" if warn.lower().rstrip(".") in ("none", "n/a", "no") else warn,
               reason=one_line(verdict.split("PROBABILITY")[0], 400), facts=one_line(facts, 1500),
               rules_read=one_line(rules_read, 500), q_yes=prob(ys[0]), q_no=prob(ns[0]))
    return out


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
            res[mid] = 1 if px[0] == "1" else 0
        time.sleep(0.1)
    for r in fc:
        if r["result"] == "" and r["market_id"] in res:
            r["result"] = res[r["market_id"]]
    for b in bets:
        if b["status"] == "open" and b["market_id"] in res:
            won = (res[b["market_id"]] == 1) == (b["side"] == "yes")
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
        sys.exit(1)
    spent = month_cost()
    recent = [r for r in fc if r["time"] >= iso(now() - timedelta(hours=24))]
    room = PER_DAY - len(recent)
    print(f"  this month's estimated AI cost: ${spent:.2f} (cap ${MONTH_CAP:.0f});"
          f" markets researched in the last 24 h: {len(recent)} of {PER_DAY}")
    if room <= 0 or spent >= MONTH_CAP:
        print("  nothing to do this run (daily limit reached or monthly cost cap)")
        summary(fc, bets)
        return
    tried = {}
    for r in read(COST):
        tried[r["market_id"]] = tried.get(r["market_id"], 0) + 1
    skip = {r["market_id"] for r in fc} | {k for k, v in tried.items() if v >= 2}   # failed twice: give up
    cands = candidates(skip, {r["event"] for r in fc}, room)[:room]
    print(f"  new markets to research: {len(cands)}   (search: {dict(WHY)})")
    stop = time.time() + RUN_SECONDS
    results = [(m, team(key, m, stop)) for m in cands]     # one market at a time (12 Nov: 3 at once hit
                                                           # the API's per-minute limit)
    costs = read(COST)
    failed = 0
    for m, t in results:
        if t["usd"]:
            costs.append({"time": iso(now()), "market_id": m.get("id"), "searches": t["searches"],
                          "usd": round(t["usd"], 5)})
        q = t["q"]
        name = str(m.get("question"))[:60]
        if q is None:
            failed += 0 if t["note"].startswith("not started") else 1
            print(f"  {name}: no forecast -- {t['note'][:200]}")
            continue                                       # not recorded: it is tried again later
        toks = [str(x) for x in rs.as_list(m.get("clobTokenIds"))]
        fr = fee_rate_of(m)
        ay, fy, cy = taker_cost(toks[0], fr)
        an, fn, cn = taker_cost(toks[1], fr)
        mid = mid_price(toks[0])
        topic = m["_topic"]
        open_b = [b for b in bets if b["status"] == "open"]
        dec, pick = "no bet", None
        if mid != "" and not 0.05 <= mid <= 0.95:
            dec = "price outside 5-95%: forecast kept, no bet"
        elif not tight(ay, an):
            dec = "order book emptied out: forecast kept, no bet"
        elif t["searches"] == 0:
            dec = "researcher made no search: forecast kept, no bet"
        elif t["confidence"] not in ("medium", "high"):
            dec = f"confidence {t['confidence']}: forecast kept, no bet"
        elif sum(b["topic"] == topic for b in open_b) >= MAX_PER_TOPIC:
            dec = f"already {MAX_PER_TOPIC} open bets on '{topic}': forecast kept, no bet"
        elif len(open_b) * STAKE + STAKE > MAX_OPEN_USD:
            dec = f"${MAX_OPEN_USD:.0f} already in open bets: forecast kept, no bet"
        else:
            if cy is not None and q - cy >= EDGE:
                pick = ("yes", toks[0], q, ay, fy, cy)
            if cn is not None and (1 - q) - cn >= EDGE and (pick is None or (1 - q) - cn > q - cy):
                pick = ("no", toks[1], 1 - q, an, fn, cn)
            if cy is None and cn is None:
                dec = "book too thin"
        row = {"time": iso(now()), "market_id": m.get("id"), "event": m["_event"], "topic": topic,
               "cid": m.get("conditionId"), "question": m.get("question"), "end": m.get("endDate"),
               "q": round(q, 4), "confidence": t["confidence"],
               "q_yes_side": "" if t["q_yes"] is None else t["q_yes"],
               "q_no_side": "" if t["q_no"] is None else t["q_no"],
               "mid_yes": "" if mid == "" else round(mid, 4), "ask_yes": "" if ay is None else round(ay, 4),
               "ask_no": "" if an is None else round(an, 4), "searches": t["searches"], "facts": t["facts"],
               "rules_read": t["rules_read"], "reason": t["reason"], "rules_warning": t["warn"], "result": "",
               "team": TEAM, "team_errors": t.get("errors", "")}
        if pick:
            side, tok, qq, av, fee, cost = pick
            bets.append({"placed": row["time"], "market_id": m.get("id"), "event": m["_event"], "topic": topic,
                         "cid": m.get("conditionId"), "question": m.get("question"), "end": m.get("endDate"),
                         "side": side, "token": tok, "q": round(qq, 4), "avg_ask": round(av, 4),
                         "fee": round(fee, 5), "cost": round(cost, 4), "shares": round(STAKE / cost, 4),
                         "status": "open", "settled": "", "payout": "", "pnl": ""})
            dec = f"BET {side.upper()} at {cost:.3f} (judge says {qq:.2f})"
        row["decision"] = dec
        fc.append(row)
        print(f"  {name}: judge {q:.2f} ({t['confidence']}, {t['searches']} searches) vs market"
              f" {row['mid_yes']} -> {dec}")
        if t.get("errors"):
            print(f"     team problems: {t['errors']}")
        if t["warn"]:
            print(f"      RULES WARNING: {t['warn']}")
    write(FC, F_FIELDS, fc)
    write(BETS, B_FIELDS, bets)
    write(COST, ["time", "market_id", "searches", "usd"], costs)
    summary(fc, bets)
    if results and failed == len(results):
        print("  UNHEALTHY: every market failed -- check the API key / credit / web search setting")
        sys.exit(1)


def summary(fc=None, bets=None):
    fc = read(FC) if fc is None else fc
    bets = read(BETS) if bets is None else bets
    n_all = len(fc)
    fc = [r for r in fc if r.get("team") == TEAM]
    ok = {r["market_id"] for r in fc}
    bets = [b for b in bets if b["market_id"] in ok]
    if n_all != len(fc):
        print(f"  ({n_all - len(fc)} early forecasts and their bets are not scored: made before the 13 Nov"
              f" fix, when the rules reader and the YES/NO agents often ran out of room and answered nothing)")
    bad = sum(r.get("team_errors", "") != "" for r in fc)
    if bad:
        print(f"  team problems on {bad} of {len(fc)} forecasts (see column team_errors)")
    done = [b for b in bets if b["status"] in ("won", "lost")]
    pnl = sum(float(b["pnl"]) for b in done)
    print(f"\n  #38b RESEARCH TEAM: {len(fc)} forecasts, {len(bets)} paper bets ({len(done)} settled,"
          f" {sum(b['status'] == 'won' for b in done)} won, profit ${pnl:.2f}"
          f"{f', {pnl / (len(done) * STAKE):+.1%} per bet' if done else ''})")
    sc = [r for r in fc if r["result"] != "" and r["mid_yes"] not in ("", None)]
    if sc:
        ba = sum((float(r["q"]) - float(r["result"])) ** 2 for r in sc) / len(sc)
        bm = sum((float(r["mid_yes"]) - float(r["result"])) ** 2 for r in sc) / len(sc)
        print(f"  accuracy on {len(sc)} settled markets (Brier, lower = better): judge {ba:.4f}   market {bm:.4f}")
    print(f"  this month's estimated AI cost (#38b): ${month_cost():.2f}")
    print(f"  first check after 50 settled markets ({len(sc)} so far): PASS only if the judge beats the"
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

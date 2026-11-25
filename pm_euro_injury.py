"""
pm_euro_injury.py  --  #37b: the EuroLeague paper bot + an injury check (runs right after #37)
===============================================================================================
#37 bets when its ratings say a team is underpriced. Its blind spot: it does not know who is
injured, so some "underpriced" teams are really just missing their star. #37b takes every bet
#37 places and does one extra step before agreeing to it:

  1. STATS  (free) -- from the official box scores (euro_players.csv), the team's 3 KEY players:
                      last 10 games, importance = average minutes x share of the team's
                      possessions used (shots + 0.44 x free throws + turnovers). A player who missed
                      the team's last 3 games is NOT counted -- the ratings already reflect that.
  2. NEWS   (AI, ~3-5c) -- Claude searches ONLY trusted basketball sites (BasketNews, Eurohoops,
                      Sportando, the official EuroLeague site) for both teams' key players:
                      OUT / DOUBTFUL / PLAYING / UNKNOWN, with the source link.
  3. DECISION (locked 24 Nov) -- skip the bet if ANY key player of the team we would bet on is OUT.
                      Otherwise #37b makes exactly #37's bet (same price, same $20).
The opponent's injuries are recorded too (no effect on the decision -- data for later).
VERDICT: compare #37b with #37 on the same games once #37 reaches 60 settled bets.
Cost cap: $3 a month (eb_cost.csv). Files: eb_bets.csv, eb_cost.csv.
"""
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pm_euro_boxes as boxes
from pm_hoops_download import same_team

HERE = Path(__file__).resolve().parent
PAPER, PLAYERS, GAMES = HERE / "euro_paper_bets.csv", HERE / "euro_players.csv", HERE / "euro_games.csv"
BETS, COST = HERE / "eb_bets.csv", HERE / "eb_cost.csv"
API = "https://api.anthropic.com/v1/messages"
MODEL, PRICE_IN, PRICE_OUT, SEARCH_USD = "claude-haiku-5-5", 0.10, 0.50, 0.01
MONTH_CAP = 3.0
SITES = ["basketnews.com", "eurohoops.net", "sportando.basketball", "euroleaguebasketball.net"]
B_FIELDS = ["placed", "ko", "game", "cid", "team", "el_team", "el_opp", "cost", "shares", "key_players",
            "opp_key_players", "statuses", "opp_statuses", "sources", "decision", "note", "status", "pnl", "check_usd"]


def now():
    return datetime.now(timezone.utc)


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


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def nice(name):
    """'LARKIN, SHANE' -> 'Shane Larkin'"""
    if "," in str(name):
        last, first = [s.strip() for s in str(name).split(",", 1)]
        return f"{first.title()} {last.title()}"
    return str(name).title()


# ---------------------------------------------------------------------------- 1. STATS
def el_name(poly_team, names):
    best = max(names, key=lambda n: same_team(poly_team, n), default=None)
    return best if best and same_team(poly_team, best) >= 0.6 else None


def key_players(team, players, before_ko):
    rows = [r for r in players if r["team"] == team and int(f(r["ko"])) < before_ko]
    gids = sorted({(int(f(r["ko"])), r["gid"]) for r in rows})[-10:]
    if len(gids) < 3:
        return []
    last3 = {g for _, g in gids[-3:]}
    keep = {g for _, g in gids}
    use, mins, recent, name = {}, {}, {}, {}
    team_use = 0.0
    for r in rows:
        if r["gid"] not in keep:
            continue
        u = f(r["fga2"]) + f(r["fga3"]) + 0.44 * f(r["fta"]) + f(r["tov"])
        pid = r["player_id"] or r["player"]
        name[pid] = r["player"]
        mins[pid] = mins.get(pid, 0.0) + f(r["min"])
        use[pid] = use.get(pid, 0.0) + u
        team_use += u
        if r["gid"] in last3 and f(r["min"]) > 0:
            recent[pid] = True
    n = len(gids)
    score = {p: (mins[p] / n) * (use[p] / team_use if team_use else 0) for p in mins if recent.get(p)}
    top = sorted(score, key=lambda p: -score[p])[:3]
    return [(nice(name[p]), round(mins[p] / n, 1)) for p in top]


# ---------------------------------------------------------------------------- 2. NEWS
def month_cost():
    m = now().strftime("%Y-%m")
    return sum(f(r["usd"]) for r in read(COST) if str(r.get("time", "")).startswith(m))


def ask(key, prompt):
    body = {"model": MODEL, "max_tokens": 2500, "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 2, "allowed_domains": SITES}]}
    msgs, text, usd, waited = body["messages"], "", 0.0, False
    for _ in range(5):
        req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                j = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and not waited:
                waited = True
                time.sleep(30)
                continue
            try:
                msg = json.loads(e.read().decode()).get("error", {}).get("message", "")
            except Exception:
                msg = ""
            return text, usd, f"API error {e.code}: {msg[:150]}"
        except Exception as e:
            return text, usd, f"API error {type(e).__name__}: {str(e)[:80]}"
        u = j.get("usage") or {}
        usd += (int(u.get("input_tokens") or 0) * PRICE_IN + int(u.get("output_tokens") or 0) * PRICE_OUT) / 1e6
        usd += int((u.get("server_tool_use") or {}).get("web_search_requests") or 0) * SEARCH_USD
        text += "".join(c.get("text", "") for c in j.get("content", []) if c.get("type") == "text")
        if j.get("stop_reason") == "pause_turn":
            body["messages"] = msgs + [{"role": "assistant", "content": j.get("content", [])}]
            continue
        return text, usd, "" if text.strip() else "empty reply"
    return text, usd, "empty reply (kept pausing)"


PROMPT = """Today is {today} (UTC). Your training knowledge is older than today: trust dated news you find.
EuroLeague game: {team} vs {opp}, tip-off {tip} UTC.

Check the LATEST injury / availability news (last 3 days) for these players:
{team}: {mine}
{opp}: {theirs}

Search only basketball news (the search is limited to trusted sites). For EACH player write exactly
one line:  PLAYER NAME | STATUS | source link
STATUS must be one of: OUT (ruled out / injured / suspended for this game), DOUBTFUL, PLAYING
(no news of any problem, or confirmed fit), UNKNOWN (you found nothing either way).
Write nothing else after the lines."""


def parse_status(text, names):
    out, src = {}, []
    for line in text.splitlines():
        parts = [p.strip(" *-`") for p in line.split("|")]
        if len(parts) < 2:
            continue
        st = parts[1].upper()
        st = next((s for s in ("OUT", "DOUBTFUL", "PLAYING", "UNKNOWN") if s in st), None)
        if not st:
            continue
        for n in names:
            last = n.split()[-1].lower()
            if last in parts[0].lower() and n not in out:
                out[n] = st
                if len(parts) > 2 and parts[2]:
                    src.append(parts[2][:120])
    return {n: out.get(n, "UNKNOWN") for n in names}, src


# ---------------------------------------------------------------------------- 3. DECISION + record
def settle(eb, paper):
    by = {(b["cid"], b["team"]): b for b in paper}
    for b in eb:
        p = by.get((b["cid"], b["team"]))
        if p and p["status"] in ("won", "lost") and b["status"] == "open":
            b["status"], b["pnl"] = p["status"], (p["pnl"] if b["decision"] == "BET" else "")
    return eb


def report(eb, paper):
    done = [b for b in eb if b["status"] in ("won", "lost")]
    took = [b for b in done if b["decision"] == "BET"]
    skip = [b for b in done if b["decision"] != "BET"]
    by = {(b["cid"], b["team"]): b for b in paper}
    pnl37 = sum(f(by[(b["cid"], b["team"])]["pnl"]) for b in done if (b["cid"], b["team"]) in by)
    print(f"\n#37b INJURY CHECK   checked {len(eb)} of #37's bets   settled {len(done)}")
    if done:
        skip_pnl = sum(f(by[(b["cid"], b["team"])]["pnl"]) for b in skip if (b["cid"], b["team"]) in by)
        print(f"  same games: #37 ${pnl37:+.2f}   #37b ${sum(f(b['pnl']) for b in took):+.2f}")
        print(f"  bets #37b skipped: {len(skip)} -- {sum(b['status'] == 'won' for b in skip)} of them won; "
              f"they made ${skip_pnl:+.2f} for #37 (negative = skipping them helped)")
    print(f"  injury-check cost this month: ${month_cost():.2f} (cap ${MONTH_CAP:.0f})")


def main():
    boxes.RUN_SECONDS = 90                                 # top up new box scores (usually 0-4 games)
    try:
        boxes.main()
    except Exception as e:
        print(f"  box score top-up skipped: {type(e).__name__}")
    paper, eb = read(PAPER), read(BETS)
    eb = settle(eb, paper)
    done = {(b["cid"], b["team"]) for b in eb}
    todo = [b for b in paper if (b["cid"], b["team"]) not in done and b["status"] == "open"
            and int(f(b["ko"])) > time.time()]
    players = read(PLAYERS)
    names = sorted({r["team"] for r in players})
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()   # 25 Nov fix: the secret ends with a line break
    for b in todo:
        home, away = [s.strip() for s in b["game"].split(" vs. ")] if " vs. " in b["game"] else (b["team"], "")
        opp_poly = away if same_team(b["team"], home) >= same_team(b["team"], away) else home
        me, opp = el_name(b["team"], names), el_name(opp_poly, names)
        mine = key_players(me, players, int(f(b["ko"]))) if me else []
        theirs = key_players(opp, players, int(f(b["ko"]))) if opp else []
        row = {"placed": now().isoformat(timespec="seconds"), "ko": b["ko"], "game": b["game"], "cid": b["cid"],
               "team": b["team"], "el_team": me or "?", "el_opp": opp or "?", "cost": b["cost"], "shares": b["shares"],
               "key_players": "; ".join(f"{n} ({m} min)" for n, m in mine),
               "opp_key_players": "; ".join(f"{n} ({m} min)" for n, m in theirs), "status": "open", "pnl": ""}
        if not mine:
            row.update(decision="BET", note="no player data -- same as #37", check_usd=0)
        elif not key:
            row.update(decision="BET", note="no API key -- same as #37", check_usd=0)
        elif month_cost() >= MONTH_CAP:
            row.update(decision="BET", note="monthly cap reached -- same as #37", check_usd=0)
        else:
            tip = datetime.fromtimestamp(int(f(b["ko"])), timezone.utc).strftime("%Y-%m-%d %H:%M")
            text, usd, err = ask(key, PROMPT.format(today=now().strftime("%Y-%m-%d"), team=me, opp=opp or opp_poly,
                                                    tip=tip, mine=", ".join(n for n, _ in mine),
                                                    theirs=", ".join(n for n, _ in theirs) or "(no data)"))
            with COST.open("a", newline="") as fh:
                w = csv.writer(fh)
                if fh.tell() == 0:
                    w.writerow(["time", "game", "usd"])
                w.writerow([now().isoformat(timespec="seconds"), b["game"], round(usd, 5)])
            st, src = parse_status(text, [n for n, _ in mine])
            ost, osrc = parse_status(text, [n for n, _ in theirs])
            out = [n for n, s in st.items() if s == "OUT"]
            row.update(statuses="; ".join(f"{n}: {s}" for n, s in st.items()),
                       opp_statuses="; ".join(f"{n}: {s}" for n, s in ost.items()),
                       sources=" ".join(src + osrc)[:400], check_usd=round(usd, 4),
                       decision="SKIP" if out else "BET",
                       note=("key player OUT: " + ", ".join(out)) if out else
                       (f"check failed: {err[:80]} -- same as #37" if err else "no key player out"))
        eb.append(row)
        print(f"  {b['game']}: #37 bets {b['team']} -> #37b {row['decision']} ({row['note']})   "
              f"[{row.get('statuses') or row['key_players']}]")
    write(BETS, B_FIELDS, settle(eb, paper))
    report(eb, paper)


if __name__ == "__main__":
    sys.exit(main())

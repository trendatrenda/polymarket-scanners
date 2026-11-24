"""
pm_euro_boxes.py  --  #37b step 1: detailed EuroLeague box scores (team + every player), no AI, $0
==================================================================================================
For every played game in euro_games.csv, downloads the official box score once and stores:
  euro_teambox.csv  -- one row per team per game: shots (2s, 3s), free throws, offensive and
                       defensive rebounds, assists, turnovers -> the Four Factors can be built
  euro_players.csv  -- one row per player per game: minutes, points, shots, free throws,
                       rebounds, assists, turnovers -> minutes, usage and true shooting % per player
Games already stored are skipped, so it can run again later and only fetch new games.
Run once with the "backtest" button (script: pm_euro_boxes.py); about 10-20 minutes.
"""
import csv
import json
import sys
import time
from pathlib import Path

from pm_hoops_download import EURO_BOX, fetch

HERE = Path(__file__).resolve().parent
GAMES, TEAMBOX, PLAYERS = HERE / "euro_games.csv", HERE / "euro_teambox.csv", HERE / "euro_players.csv"
T_FIELDS = ["gid", "season", "ko", "team", "opp", "home", "pts", "fgm2", "fga2", "fgm3", "fga3", "ftm", "fta",
            "oreb", "dreb", "ast", "tov", "stl", "blk", "pf"]
P_FIELDS = ["gid", "season", "ko", "team", "player_id", "player", "starter", "min", "pts", "fgm2", "fga2",
            "fgm3", "fga3", "ftm", "fta", "oreb", "dreb", "ast", "tov", "stl", "blk"]
KEYS = {"pts": "Points", "fgm2": "FieldGoalsMade2", "fga2": "FieldGoalsAttempted2", "fgm3": "FieldGoalsMade3",
        "fga3": "FieldGoalsAttempted3", "ftm": "FreeThrowsMade", "fta": "FreeThrowsAttempted",
        "oreb": "OffensiveRebounds", "dreb": "DefensiveRebounds", "ast": "Assistances", "tov": "Turnovers",
        "stl": "Steals", "blk": "BlocksFavour", "pf": "FoulsCommited"}
RUN_SECONDS = 100 * 60


def read(p):
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def append(p, fields, rows):
    new = not p.exists()
    with p.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def minutes(x):
    """'25:13' -> 25.22 ; 'DNP' / '' -> 0"""
    s = str(x or "").strip()
    if ":" in s:
        m, sec = s.split(":")[:2]
        return round(num(m) + num(sec) / 60, 2)
    return round(num(s), 2)


def parse(txt, g):
    """Returns (team rows, player rows) or None. Teams are matched to home/away by final points."""
    data = json.loads(txt)
    stats = data.get("Stats") or []
    if len(stats) != 2:
        return None
    hp, ap = int(num(g["home_pts"])), int(num(g["away_pts"]))
    trows, prows = [], []
    sides = []
    for t in stats:
        pts = int(num((t.get("totr") or {}).get("Points")))
        side = "home" if pts == hp else "away" if pts == ap else None
        sides.append(side)
    if sorted(s for s in sides if s) != ["away", "home"]:
        return None
    for t, side in zip(stats, sides):
        team, opp = (g["home"], g["away"]) if side == "home" else (g["away"], g["home"])
        tot = t.get("totr") or {}
        trows.append({"gid": g["gid"], "season": g["season"], "ko": g["ko"], "team": team, "opp": opp,
                      "home": int(side == "home"), **{k: num(tot.get(v)) for k, v in KEYS.items()}})
        for p in t.get("PlayersStats") or []:
            mn = minutes(p.get("Minutes"))
            prows.append({"gid": g["gid"], "season": g["season"], "ko": g["ko"], "team": team,
                          "player_id": str(p.get("Player_ID") or "").strip(), "player": p.get("Player"),
                          "starter": int(num(p.get("IsStarter"))), "min": mn,
                          **{k: num(p.get(v)) for k, v in KEYS.items() if k in P_FIELDS}})
    return trows, prows


def main():
    stop = time.time() + RUN_SECONDS
    games = [g for g in read(GAMES) if str(g.get("played")) == "1"]
    have = {r["gid"] for r in read(TEAMBOX)}
    need = [g for g in games if g["gid"] not in have]
    print(f"played games: {len(games)}   already stored: {len(have)}   to download: {len(need)}")
    ok = bad = 0
    for i, g in enumerate(need):
        if time.time() > stop:
            print("  (time budget used -- run again to continue)")
            break
        txt = fetch(EURO_BOX.format(g=g["code"], s=g["season"]), pause=0.3)
        try:
            res = parse(txt, g) if txt else None
        except Exception as e:
            print(f"  {g['gid']}: could not read ({type(e).__name__})")
            res = None
        if not res:
            bad += 1
            continue
        append(TEAMBOX, T_FIELDS, res[0])
        append(PLAYERS, P_FIELDS, res[1])
        ok += 1
        if ok % 50 == 0:
            print(f"  ... {ok} stored", flush=True)
    print(f"stored this run: {ok}   failed: {bad}")
    pl = read(PLAYERS)
    tb = read(TEAMBOX)
    print(f"totals: {len(tb)} team rows, {len(pl)} player rows")
    if pl:
        top = sorted(pl, key=lambda r: -num(r["min"]))[:3]
        print("  sample (most minutes in one game):", [(r["player"], r["team"], r["min"], r["pts"]) for r in top])
    return 0 if ok or not need else 1


if __name__ == "__main__":
    sys.exit(main())

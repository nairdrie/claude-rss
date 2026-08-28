#!/usr/bin/env python3
"""Fetch Nick's Sleeper fantasy football state -> state/fantasy.json.

Read-only Sleeper API (no key). Discovers the league from the username, finds
this week's matchup, resolves the starting lineup + injuries against the player
dictionary, and pulls trending waiver adds. Projections/win-odds are best-effort
(Sleeper's projections are unofficial) and simply omitted if unavailable.

Every network step is wrapped so a partial failure still yields a valid (if
thinner) fantasy.json rather than crashing the daily run. If egress is blocked
(the routine sandbox), it exits 0 without writing — same posture as the other
fetchers; the GitHub Action is where this actually runs.

Usage:
    python scripts/fetch_fantasy.py            # needs internet
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

try:
    from scripts import _common as C
    from scripts import _http as H
except ImportError:
    import _common as C
    import _http as H

API = "https://api.sleeper.app/v1"
FANTASY_OUT = C.STATE_DIR / "fantasy.json"
PLAYERS_CACHE = C.STATE_DIR / "players_nfl.json"

POS_LABEL = {"SUPER_FLEX": "SF", "WRRB_FLEX": "FLX", "REC_FLEX": "FLX",
             "FLEX": "FLEX", "IDP_FLEX": "IDP"}
INJURY = {
    "Questionable": ("q", "Q"), "Doubtful": ("d", "D"), "Out": ("o", "OUT"),
    "IR": ("o", "IR"), "PUP": ("o", "PUP"), "Sus": ("o", "SUS"), "NA": ("o", "NA"),
    "COV": ("q", "COV"), "DNR": ("d", "DNR"),
}
BENCH_SLOTS = {"BN", "IR", "TAXI"}


def _cfg() -> dict:
    try:
        data = C.load_interests()  # not used; kept for parity
    except Exception:
        pass
    import yaml
    path = C.REPO_ROOT / "config" / "dashboard.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("fantasy", {}) or {}


def _get(url: str):
    return H.get_json(url, timeout=25)


def load_players(season_hint: str) -> dict:
    """Player dictionary (~5MB), cached for the day."""
    if PLAYERS_CACHE.exists() and (time.time() - PLAYERS_CACHE.stat().st_mtime) < 20 * 3600:
        try:
            return json.loads(PLAYERS_CACHE.read_text())
        except Exception:
            pass
    players = _get(f"{API}/players/nfl")
    try:
        C.STATE_DIR.mkdir(parents=True, exist_ok=True)
        PLAYERS_CACHE.write_text(json.dumps(players))
    except OSError:
        pass
    return players


def projections(season: str, week: int) -> dict:
    """Best-effort {player_id: projected_ppr_points}. Empty on any failure."""
    for host in ("https://api.sleeper.com", "https://api.sleeper.app"):
        try:
            data = H.get_json(
                f"{host}/projections/nfl/{season}/{week}?season_type=regular", timeout=25)
        except Exception:
            continue
        out = {}
        rows = data if isinstance(data, list) else data.values() if isinstance(data, dict) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = str(row.get("player_id") or "")
            pts = (row.get("stats") or {}).get("pts_ppr")
            if pid and isinstance(pts, (int, float)):
                out[pid] = round(float(pts), 1)
        if out:
            return out
    return {}


def nfl_opponents(week: int) -> dict:
    """Best-effort {TEAM_ABBR: 'vs OPP' / '@ OPP'} for the regular-season week (ESPN)."""
    try:
        data = H.get_json("https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
                          f"scoreboard?week={week}&seasontype=2", timeout=15)
    except Exception:
        return {}
    out = {}
    for ev in data.get("events", []):
        for comp in ev.get("competitions", []):
            tm = comp.get("competitors", [])
            if len(tm) != 2:
                continue
            a = (tm[0].get("team") or {}).get("abbreviation", "")
            b = (tm[1].get("team") or {}).get("abbreviation", "")
            if not a or not b:
                continue
            a_home = tm[0].get("homeAway") == "home"
            out[a] = f"{'vs' if a_home else '@'} {b}"
            out[b] = f"{'@' if a_home else 'vs'} {a}"
    return out


def player_view(pid: str, players: dict, proj: dict) -> dict:
    """Resolve one starter slot's player into a display row."""
    p = players.get(pid) or {}
    if not p and pid and pid.isalpha() and pid.isupper():   # DEF slot ("BUF")
        return {"name": f"{pid} D/ST", "team": pid, "pos": "DEF",
                "status": None, "note": "", "proj": proj.get(pid)}
    name = p.get("full_name") or " ".join(
        x for x in (p.get("first_name"), p.get("last_name")) if x) or pid
    inj = (p.get("injury_status") or "").strip()
    status = None
    if inj and inj not in ("Healthy", "Active"):
        k, txt = INJURY.get(inj, ("news", inj[:3].upper()))
        status = {"k": k, "txt": txt}
    return {"name": name, "team": p.get("team") or "FA", "pos": p.get("position") or "",
            "status": status, "note": "", "proj": proj.get(pid)}


def starting_slots(roster_positions: list) -> list:
    return [rp for rp in (roster_positions or []) if rp not in BENCH_SLOTS]


def build_lineup(starters: list, slots: list, players: dict, proj: dict, opp_map: dict) -> list:
    out = []
    for i, pid in enumerate(starters or []):
        if not pid or pid == "0":
            continue
        slot = slots[i] if i < len(slots) else ""
        pv = player_view(str(pid), players, proj)
        out.append({
            "pos": POS_LABEL.get(slot, slot or pv["pos"]),
            "name": pv["name"], "team": pv["team"], "opp": opp_map.get(pv["team"], ""),
            "proj": pv["proj"], "status": pv["status"], "note": pv["note"],
        })
    return out


def roster_alerts(player_ids: list, players: dict) -> list:
    alerts = []
    for pid in player_ids or []:
        p = players.get(str(pid)) or {}
        inj = (p.get("injury_status") or "").strip()
        if not inj or inj in ("Healthy", "Active"):
            continue
        k, txt = INJURY.get(inj, ("news", inj[:4].upper()))
        body = (p.get("injury_body_part") or "").strip()
        notes = (p.get("injury_notes") or "").strip()
        detail = notes or (f"{inj}" + (f" — {body.lower()}" if body else ""))
        name = p.get("full_name") or str(pid)
        alerts.append({"name": name, "tag": k, "tagTxt": txt,
                       "txt": detail[:140], "src": "Sleeper"})
    # Out/Doubtful first, then Questionable
    order = {"o": 0, "d": 1, "q": 2, "news": 3}
    alerts.sort(key=lambda a: order.get(a["tag"], 9))
    return alerts[:4]


def waiver_watch(players: dict, my_ids: set) -> list:
    try:
        trend = _get(f"{API}/players/nfl/trending/add?lookback_hours=24&limit=40")
    except Exception:
        return []
    out = []
    for row in trend or []:
        pid = str(row.get("player_id") or "")
        if not pid or pid in my_ids:
            continue
        p = players.get(pid) or {}
        pos = p.get("position") or ""
        if pos not in ("QB", "RB", "WR", "TE"):
            continue
        cnt = int(row.get("count") or 0)
        trend_s = f"+{cnt/1000:.1f}k" if cnt >= 1000 else f"+{cnt}"
        out.append({"name": p.get("full_name") or pid,
                    "sub": f"{pos} · {p.get('team') or 'FA'} · trending add",
                    "trend": trend_s, "note": "hot add"})
        if len(out) >= 3:
            break
    return out


def win_pct(mine, theirs) -> tuple[int | None, int | None]:
    if mine is None or theirs is None:
        return None, None
    p = 1.0 / (1.0 + math.exp(-(mine - theirs) / 9.0))
    a = max(1, min(99, round(p * 100)))
    return a, 100 - a


def team_name(users_by_id: dict, roster) -> str:
    owner = roster.get("owner_id")
    u = users_by_id.get(owner, {})
    meta = u.get("metadata") or {}
    return (meta.get("team_name") or u.get("display_name") or "Team").strip()


def record_str(roster) -> str:
    s = roster.get("settings") or {}
    w, l, t = s.get("wins", 0), s.get("losses", 0), s.get("ties", 0)
    return f"{w}-{l}" + (f"-{t}" if t else "")


def main() -> None:
    cfg = _cfg()
    username = (cfg.get("username") or "").strip()
    if not username:
        print("ERROR: config/dashboard.yaml fantasy.username is required.", file=sys.stderr)
        sys.exit(1)

    try:
        state = _get(f"{API}/state/nfl")
        season = str(cfg.get("season") or state.get("season") or "2026")
        season_type = (state.get("season_type") or "regular").lower()
        # During the preseason, state.week (and display_week) are the PRESEASON
        # week (1-4) — showing that as "Week 3" is wrong when the regular season
        # hasn't kicked off. Force the upcoming regular Week 1 in that case.
        preseason = season_type not in ("regular", "post")
        if cfg.get("week"):
            week = int(cfg["week"])
        elif preseason:
            week = 1
        else:
            week = int(state.get("week") or state.get("display_week") or 1) or 1
        print(f"  Sleeper state: season_type={season_type} week={state.get('week')} "
              f"display_week={state.get('display_week')} leg={state.get('leg')} -> using week {week}",
              file=sys.stderr)

        user = _get(f"{API}/user/{username}")
        user_id = str(user.get("user_id") or "")
        if not user_id:
            raise ValueError(f"Sleeper user '{username}' not found")

        leagues = _get(f"{API}/user/{user_id}/leagues/nfl/{season}") or []
        if not leagues:
            raise ValueError(f"no NFL leagues for {username} in {season}")
        want = str(cfg.get("league_id") or "").strip()
        want_name = (cfg.get("league_name") or "").strip().lower()
        league = next((l for l in leagues if str(l.get("league_id")) == want), None) \
            or next((l for l in leagues if (l.get("name") or "").lower() == want_name), None) \
            or leagues[0]
        lid = str(league["league_id"])

        rosters = _get(f"{API}/league/{lid}/rosters") or []
        users = _get(f"{API}/league/{lid}/users") or []
        users_by_id = {str(u.get("user_id")): u for u in users}
        my_roster = next((r for r in rosters if str(r.get("owner_id")) == user_id), None)
        if my_roster is None:
            raise ValueError("couldn't find your roster in the league")
        my_rid = my_roster.get("roster_id")

        players = load_players(season)
        proj = projections(season, week)
        opp_map = nfl_opponents(week)
        slots = starting_slots(league.get("roster_positions"))

        # Matchup: find opponent via shared matchup_id
        opp_roster, my_pts, opp_pts, my_starters, opp_starters = None, None, None, [], []
        try:
            matchups = _get(f"{API}/league/{lid}/matchups/{week}") or []
            mine = next((m for m in matchups if m.get("roster_id") == my_rid), None)
            if mine:
                my_pts = mine.get("points")
                my_starters = mine.get("starters") or my_roster.get("starters") or []
                opp = next((m for m in matchups if m.get("matchup_id") == mine.get("matchup_id")
                            and m.get("roster_id") != my_rid), None)
                if opp:
                    opp_pts = opp.get("points")
                    opp_starters = opp.get("starters") or []
                    opp_roster = next((r for r in rosters
                                       if r.get("roster_id") == opp.get("roster_id")), None)
        except Exception as exc:
            print(f"  (matchup lookup failed: {exc})", file=sys.stderr)
        if not my_starters:
            my_starters = my_roster.get("starters") or []

        lineup = build_lineup(my_starters, slots, players, proj, opp_map)
        my_proj = round(sum(s["proj"] for s in lineup if s["proj"]), 1) if proj else None
        opp_proj = None
        if proj and opp_starters:
            opp_proj = round(sum(proj.get(str(p), 0) for p in opp_starters), 1) or None
        my_wp, opp_wp = win_pct(my_proj, opp_proj)

        rec = league.get("scoring_settings", {}).get("rec", 0) or 0
        fmt = f"{league.get('total_rosters', len(rosters))}-team · " + \
              ("PPR" if rec >= 1 else "Half-PPR" if rec >= 0.5 else "Standard")

        fantasy = {
            "league": league.get("name") or "League",
            "format": fmt,
            "week": week,
            "meta": (f"live · Week {week}" if (not preseason and (my_pts or 0) > 0)
                     else f"Week {week} preview"),
            "you": {"name": team_name(users_by_id, my_roster), "record": record_str(my_roster),
                    "proj": my_proj, "winPct": my_wp},
            "opp": {"name": team_name(users_by_id, opp_roster) if opp_roster else "TBD",
                    "record": record_str(opp_roster) if opp_roster else "",
                    "proj": opp_proj, "winPct": opp_wp},
            "starters": lineup,
            "alerts": roster_alerts(my_roster.get("players"), players),
            "waiver": waiver_watch(players, {str(p) for p in (my_roster.get("players") or [])}),
        }
    except H.EgressBlocked:
        print("Fantasy: egress blocked — run on the Action, not the sandbox. "
              "Left fantasy.json untouched.", file=sys.stderr)
        sys.exit(0)
    except Exception as exc:
        print(f"ERROR building fantasy state: {exc}", file=sys.stderr)
        sys.exit(1)

    FANTASY_OUT.write_text(json.dumps(fantasy, indent=2, ensure_ascii=False))
    print(f"Fantasy: {fantasy['league']} · Week {week} · "
          f"{fantasy['you']['name']} ({fantasy['you']['record']}) vs {fantasy['opp']['name']} · "
          f"{len(fantasy['starters'])} starters, {len(fantasy['alerts'])} alerts, "
          f"proj={'yes' if proj else 'n/a'}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Assemble the Morning Line dashboard from the per-module state files + live
sports/markets/Pokémon-GO lookups, inject it into the HTML templates, and
render the cover PNG.

Inputs : state/portfolio.json (fetch_portfolio.py), state/fantasy.json
         (fetch_fantasy.py), config/dashboard.yaml, templates/*.html
Outputs: state/dashboard.json  (the assembled DATA, for debugging)
         state/dashboard.html   (template + injected DATA)
         state/cover.html + state/cover.png (thumbnail)

Every live lookup degrades gracefully — a failed module leaves a safe
placeholder rather than crashing the run. Runs on the GitHub Action (open
internet); in the sandbox the sports/markets lookups are egress-blocked and
fall back, which is fine for a local render test.
"""
from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from scripts import _common as C
    from scripts import _http as H
except ImportError:
    import _common as C
    import _http as H

TEMPLATES = C.REPO_ROOT / "templates"
DASH_OUT = C.STATE_DIR / "dashboard.html"
COVER_OUT = C.STATE_DIR / "cover.html"
COVER_PNG = C.STATE_DIR / "cover.png"
DATA_OUT = C.STATE_DIR / "dashboard.json"


def cfg() -> dict:
    import yaml
    with open(C.REPO_ROOT / "config" / "dashboard.yaml", "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Markets (Yahoo Finance)
# ---------------------------------------------------------------------------
def yahoo_quote(symbol: str) -> tuple[float, float]:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(symbol)}?range=5d&interval=1d")
    meta = H.get_json(url)["chart"]["result"][0]["meta"]
    price = float(meta["regularMarketPrice"])
    prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or price)
    return price, prev


def fmt_market(symbol: str, price: float) -> str:
    if symbol.startswith("^"):
        return f"{price:,.0f}"
    if symbol == "BTC-USD":
        return f"${price/1000:,.1f}K"
    if symbol.endswith("=F"):
        return f"${price:,.0f}"
    if symbol == "CAD=X":
        return f"{price:.3f}"
    return f"{price:,.2f}"


def fetch_markets(conf: dict) -> list:
    out = []
    for m in conf.get("markets", []):
        sym = m["symbol"]
        try:
            price, prev = yahoo_quote(sym)
            pct = (price - prev) / prev * 100 if prev else 0.0
            out.append({"name": m["name"], "val": fmt_market(sym, price), "pct": round(pct, 2)})
        except H.EgressBlocked:
            raise
        except Exception as exc:
            print(f"  market {sym} failed: {exc}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Blue Jays (MLB StatsAPI)
# ---------------------------------------------------------------------------
MLB = "https://statsapi.mlb.com/api/v1"


def _mlb_games(team_id: int, start: str, end: str) -> list:
    url = (f"{MLB}/schedule?sportId=1&teamId={team_id}&startDate={start}&endDate={end}"
           f"&hydrate=probablePitcher(note),linescore,team")
    data = H.get_json(url)
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games


def fetch_jays(conf: dict) -> dict:
    team_id = int(conf.get("sports", {}).get("blue_jays", {}).get("mlb_team_id", 141))
    today = C.now_local().date()
    jays = {"last": None, "next": None, "standing": None, "news": None}
    try:
        games = _mlb_games(team_id, (today - timedelta(days=6)).isoformat(),
                           (today + timedelta(days=6)).isoformat())
    except H.EgressBlocked:
        raise
    except Exception as exc:
        print(f"  jays schedule failed: {exc}", file=sys.stderr)
        games = []

    finals = [g for g in games if (g.get("status", {}).get("abstractGameState") == "Final")]
    upcoming = [g for g in games if g.get("status", {}).get("abstractGameState") in ("Preview", "Live")]
    finals.sort(key=lambda g: g.get("gameDate", ""))
    upcoming.sort(key=lambda g: g.get("gameDate", ""))

    def side(game):
        home = game["teams"]["home"]["team"]["id"] == team_id
        us, them = ("home", "away") if home else ("away", "home")
        return us, them, home

    if finals:
        g = finals[-1]
        us, them, home = side(g)
        us_score = g["teams"][us].get("score", 0)
        them_score = g["teams"][them].get("score", 0)
        opp_name = g["teams"][them]["team"].get("abbreviation") or g["teams"][them]["team"].get("name", "")
        res = "W" if us_score > them_score else "L"
        jays["last"] = {
            "res": res,
            "score": f"{us_score}–{them_score}",
            "opp": f"{'vs' if home else '@'} {opp_name}",
            "recap": "", "star": "",
        }

    if upcoming:
        g = upcoming[0]
        us, them, home = side(g)
        opp_name = g["teams"][them]["team"].get("abbreviation") or g["teams"][them]["team"].get("name", "")
        dt = parse_iso(g.get("gameDate", ""))
        when, tstr = "Next", ""
        if dt:
            local = dt.astimezone(C.tz())
            when = "Today" if local.date() == today else local.strftime("%a")
            tstr = local.strftime("%-I:%M %p")
        pp_us = (g["teams"][us].get("probablePitcher") or {}).get("fullName", "")
        pp_them = (g["teams"][them].get("probablePitcher") or {}).get("fullName", "")
        detail = ""
        if pp_us or pp_them:
            detail = f"Probables: <b>{html.escape(pp_us or 'TBD')}</b> vs. <b>{html.escape(pp_them or 'TBD')}</b>"
        jays["next"] = {"when": when, "time": tstr, "opp": f"{'vs' if home else '@'} {opp_name}",
                        "detail": detail}

    # Wild-card standing (AL = leagueId 103)
    try:
        season = C.now_local().year
        st = H.get_json(f"{MLB}/standings?leagueId=103&season={season}"
                        f"&standingsTypes=wildCard&hydrate=team")
        rec = None
        for grp in st.get("records", []):
            for tr in grp.get("teamRecords", []):
                if tr.get("team", {}).get("id") == team_id:
                    rec = tr
                    break
        if rec:
            wc_rank = rec.get("wildCardRank") or rec.get("leagueRank") or ""
            gb = str(rec.get("wildCardGamesBack") or rec.get("gamesBack") or "-").strip()
            holds = gb in ("-", "0", "0.0")
            badge = f"{_ordinal(int(wc_rank))} Wild Card" if str(wc_rank).isdigit() else "AL East"
            jays["standing"] = {
                "sbadge": badge,
                "gb": "" if holds else f"{gb} GB",       # behind reads "3.5 GB", not "+3.5"
                "record": f"{rec.get('wins',0)}-{rec.get('losses',0)}",
                "note": _standing_note(rec),
                "gb_num": 0.0 if holds else _as_float(gb),
            }
    except H.EgressBlocked:
        raise
    except Exception as exc:
        print(f"  jays standings failed: {exc}", file=sys.stderr)

    if jays["standing"] is None:
        jays["standing"] = {"sbadge": "Blue Jays", "gb": "", "record": "", "note": "", "gb_num": 99}
    return jays


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1:'st',2:'nd',3:'rd'}.get(n % 10,'th')}"


def _as_float(s) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 99.0


def _standing_note(rec: dict) -> str:
    gb = rec.get("wildCardGamesBack", "-")
    if gb in ("-", "0", "0.0"):
        return "Holding a wild-card spot"
    try:
        if float(gb) > 0:
            return f"{gb} games back of the wild card"
    except (TypeError, ValueError):
        pass
    return ""


# ---------------------------------------------------------------------------
# Maple Leafs (off-season countdown)
# ---------------------------------------------------------------------------
def leafs_block(conf: dict) -> dict:
    lc = conf.get("sports", {}).get("maple_leafs", {})
    today = C.now_local().date()
    camp = _date(lc.get("camp_opens"))
    opener = _date(lc.get("season_opener"))
    opp = lc.get("opener_opponent", "")
    if opener and today >= opener:
        return {"inSeason": True}
    if camp and today < camp:
        big, lbl = (camp - today).days, "days to camp"
    elif opener:
        big, lbl = (opener - today).days, "days to opener"
    else:
        big, lbl = "—", "offseason"
    cd = []
    if camp:
        cd.append(f"Camp opens {camp.strftime('%b %-d')}")
    if opener:
        cd.append(f"Opener {opener.strftime('%b %-d')}" + (f" vs {opp}" if opp else ""))
    return {"inSeason": False, "offBig": str(big), "offLbl": lbl,
            "countdown": " · ".join(cd) or "Offseason", "news": None}


def _date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Pokémon GO (ScrapedDuck)
# ---------------------------------------------------------------------------
POGO_ICON = {"spotlight-hour": "✨", "raid-hour": "⚔️", "raid-battles": "⚔️",
             "community-day": "🎉", "event": "🎉", "research": "🔬",
             "go-battle-league": "🥊", "max-mondays": "⚡", "elite-raids": "⚔️"}
POGO_PRIORITY = {"spotlight-hour": 0, "community-day": 1, "raid-hour": 2,
                 "raid-battles": 3, "event": 4, "research": 5}


def fetch_pogo(conf: dict) -> list:
    pc = conf.get("pogo", {})
    url = pc.get("events_url")
    if not url:
        return []
    try:
        events = H.get_json(url, timeout=15)
    except H.EgressBlocked:
        raise
    except Exception as exc:
        print(f"  pogo failed: {exc}", file=sys.stderr)
        return []

    now = C.now_local()
    today = now.date()
    picked = []
    for e in events:
        start, end = parse_iso(e.get("start")), parse_iso(e.get("end"))
        if not start:
            continue
        s_local = start.astimezone(C.tz())
        e_local = end.astimezone(C.tz()) if end else s_local
        active = s_local <= now <= e_local
        starts_today = s_local.date() == today
        if not (active or starts_today):
            continue
        etype = e.get("eventType", "")
        name = html.unescape(e.get("name", "")).strip()
        heading = html.unescape(e.get("heading", "")).strip() or etype
        # time hint for short, same-day timed events
        sub = ""
        if end and (e_local - s_local) <= timedelta(hours=6) and s_local.date() == today:
            sub = s_local.strftime("%-I %p").lstrip("0")
        elif e_local.date() != today:
            sub = f"ends {e_local.strftime('%b %-d')}"
        elif active:
            sub = "active now"
        picked.append({
            "_prio": POGO_PRIORITY.get(etype, 8),
            "_start": s_local,
            "icon": POGO_ICON.get(etype, "📅"),
            "tag": heading + (f" · {sub}" if sub and etype in ("spotlight-hour", "raid-hour") else ""),
            "main": (name[:40] + "…") if len(name) > 41 else name,
            "sub": sub if etype not in ("spotlight-hour", "raid-hour") else "",
        })
    picked.sort(key=lambda x: (x["_prio"], x["_start"]))
    out = [{k: v for k, v in p.items() if not k.startswith("_")} for p in picked]
    return out[: int(pc.get("max_items", 4))]


# ---------------------------------------------------------------------------
# Portfolio (redacted for the public dashboard: total + day change + spark)
# ---------------------------------------------------------------------------
def portfolio_block(pj: dict | None) -> dict | None:
    if not pj:
        return None
    dt = parse_iso(pj.get("as_of", ""))
    tstr = dt.astimezone(C.tz()).strftime("%-I:%M %p") if dt else ""
    # Per-holding detail is included so the dashboard's Portfolio card can expand
    # to show each position (Nick opted to keep holdings public).
    holdings = [{
        "tkr": h.get("symbol", ""), "co": h.get("name", ""),
        "shares": h.get("shares", 0), "price": h.get("price", 0),
        "dayPct": h.get("day_pct", 0), "value": h.get("value_cad", 0),
    } for h in pj.get("holdings", [])]
    return {
        "total": pj.get("total", 0),
        "dayChange": pj.get("day_change", 0),
        "dayPct": pj.get("day_pct", 0),
        "week": pj.get("week_series") or [pj.get("total", 0), pj.get("total", 0)],
        "note": f"Updated {tstr}" if tstr else "Updated this morning",
        "holdings": holdings,
    }


def fantasy_block(fj: dict | None) -> dict:
    if fj:
        return fj
    return {"league": "Fantasy", "format": "", "week": "", "meta": "",
            "you": {"name": "—", "record": "", "proj": None, "winPct": None},
            "opp": {"name": "—", "record": "", "proj": None, "winPct": None},
            "starters": [], "alerts": [], "waiver": []}


# ---------------------------------------------------------------------------
# Greeting subline
# ---------------------------------------------------------------------------
def greeting_sub(fantasy, jays, markets) -> str:
    bits = []
    n = len(fantasy.get("alerts", []))
    if n:
        bits.append(f"{n} roster alert{'s' if n != 1 else ''}")
    stand = jays.get("standing") or {}
    gbn = stand.get("gb_num", 99)
    if "Wild Card" in (stand.get("sbadge") or ""):
        if gbn <= 1.0 or "holding" in (stand.get("note") or "").lower():
            bits.append("Jays hold a wild-card spot")
        elif gbn <= 6:
            bits.append(f"Jays {gbn:g} back of a WC spot")
    if markets:
        ups = sum(1 for m in markets if m["pct"] >= 0)
        bits.append("markets green" if ups == len(markets)
                    else "markets red" if ups == 0 else "markets mixed")
    return " · ".join(bits) or "your feed, at a glance"


# ---------------------------------------------------------------------------
# Cover block
# ---------------------------------------------------------------------------
def arrow(v):
    return "▲" if v >= 0 else "▼"


def cover_block(data) -> dict:
    """Bold landscape thumbnail: logo + date, three hero metrics, a stats strip."""
    f, j, p, mk = data["fantasy"], data["jays"], data.get("portfolio"), data["markets"]
    lf, pg = data["leafs"], data["pogo"]
    you, st = f.get("you", {}), j.get("standing") or {}

    metrics = []
    if you.get("winPct") is not None:
        metrics.append({"big": f"{you['winPct']}%", "label": "Fantasy · Win odds", "kind": "accent"})
    elif you.get("proj") is not None:
        metrics.append({"big": f"{you['proj']:.0f}", "label": "Fantasy · Projected", "kind": "accent"})
    if p:
        up = p["dayPct"] >= 0
        metrics.append({"big": ("+" if up else "−") + f"{abs(p['dayPct']):.1f}%",
                        "label": "Portfolio · Today", "kind": "up" if up else "down"})
    last = j.get("last")
    if last and last.get("res"):
        won = last["res"] == "W"
        metrics.append({"big": last.get("score", ""), "label": f"Blue Jays · {'Won' if won else 'Lost'}",
                        "kind": "up" if won else "down"})
    elif st.get("record"):
        metrics.append({"big": st["record"], "label": "Blue Jays · Record", "kind": "ink"})

    footer = []
    by = {m["name"]: m for m in mk}
    for nm in ("S&P 500", "Bitcoin"):
        if nm in by:
            footer.append({"t": ("S&P " if nm == "S&P 500" else "BTC "),
                           "b": ("+" if by[nm]["pct"] >= 0 else "−") + f"{abs(by[nm]['pct']):.1f}%"})
    if not lf.get("inSeason") and lf.get("offBig"):
        footer.append({"t": "Leafs ", "b": f"{lf['offBig']}d to camp"})
    if pg:
        footer.append({"t": "", "b": f"{len(pg)} PoGO events"})

    dl = data["date"].split(",")
    return {"date_line": dl[0], "date_main": dl[1].strip() if len(dl) > 1 else "",
            "metrics": metrics[:3], "footer": footer}


# ---------------------------------------------------------------------------
# Template injection + PNG render
# ---------------------------------------------------------------------------
def inject(template: Path, token: str, obj, out: Path) -> None:
    tpl = template.read_text()
    payload = json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")  # never break </script>
    out.write_text(tpl.replace(token, payload))


def find_chrome() -> str | None:
    for cand in (os.environ.get("CHROME_BIN"), "google-chrome", "google-chrome-stable",
                 "chromium-browser", "chromium",
                 "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"):
        if cand and (shutil.which(cand) or os.path.exists(cand)):
            return shutil.which(cand) or cand
    return None


def render_cover_png() -> bool:
    chrome = find_chrome()
    if not chrome:
        print("  cover: no chrome binary found; skipping PNG.", file=sys.stderr)
        return False
    import tempfile
    cmd = [chrome, "--headless=new", "--no-sandbox", "--hide-scrollbars",
           "--force-device-scale-factor=2", "--window-size=1200,760",
           "--virtual-time-budget=3000", f"--user-data-dir={tempfile.mkdtemp()}",
           f"--screenshot={COVER_PNG}", COVER_OUT.as_uri()]
    try:
        subprocess.run(cmd, timeout=90, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return COVER_PNG.exists()
    except Exception as exc:
        print(f"  cover PNG render failed: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    conf = cfg()
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    now = C.now_local()

    fantasy = fantasy_block(read_json(C.STATE_DIR / "fantasy.json"))
    portfolio = portfolio_block(read_json(C.STATE_DIR / "portfolio.json"))

    def safe(fn, fallback):
        try:
            return fn()
        except H.EgressBlocked:
            print(f"  (egress blocked for {fn.__name__}; using fallback)", file=sys.stderr)
            return fallback
        except Exception as exc:
            print(f"  ({fn.__name__} failed: {exc}; using fallback)", file=sys.stderr)
            return fallback

    markets = safe(lambda: fetch_markets(conf), [])
    jays = safe(lambda: fetch_jays(conf), {"last": None, "next": None,
                "standing": {"sbadge": "Blue Jays", "gb": "", "record": "", "note": "", "gb_num": 99},
                "news": None})
    pogo = safe(lambda: fetch_pogo(conf), [])
    leafs = leafs_block(conf)

    data = {
        "date": now.strftime("%A, %B %-d, %Y"),
        "generatedAt": now.strftime("%-I:%M %p ET"),
        "greetingName": conf.get("greeting_name", "Nick"),
        "fantasy": fantasy, "jays": jays, "leafs": leafs,
        "portfolio": portfolio, "markets": markets, "pogo": pogo,
    }
    data["greetingSub"] = greeting_sub(fantasy, jays, markets)
    cover = cover_block(data)

    DATA_OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    inject(TEMPLATES / "dashboard.html", "__DASHBOARD_JSON__", data, DASH_OUT)
    inject(TEMPLATES / "cover.html", "__COVER_JSON__", cover, COVER_OUT)
    png_ok = render_cover_png()

    print(f"Dashboard: assembled {now:%Y-%m-%d %H:%M} · markets={len(markets)} "
          f"pogo={len(pogo)} portfolio={'yes' if portfolio else 'no'} "
          f"fantasy={'yes' if fantasy.get('starters') else 'thin'} cover_png={'yes' if png_ok else 'no'}")


if __name__ == "__main__":
    main()

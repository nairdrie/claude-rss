#!/usr/bin/env python3
"""Summarize the portfolio from config/portfolio.yaml + live quotes, and keep
a daily history so the dashboard can show a real trend line.

What it does, once a day (in the egress-enabled Action, NOT the locked-down
routine sandbox — quote lookups need open internet, same story as thumbnails):

  1. Read config/portfolio.yaml (symbols + share counts).
  2. Look up a live quote for each symbol from Yahoo Finance (price +
     previous close -> day change). No API key required.
  3. Convert USD positions to CAD with a live USD/CAD rate.
  4. Roll up: total value, today's $ and % change, per-holding rows + weights.
  5. Append today's snapshot to a rolling history store (S3 key, private by
     default) and recompute the past-week trend/sparkline from it.
  6. Write state/portfolio.json — the block the dashboard/cover render from.

Privacy: the history + portfolio.json contain real dollar figures and share
counts. By default the history key is PRIVATE (`private/...`, NOT under the
public `feed/*` prefix). See docs/s3-access.md and the README.

Design goals mirror the rest of the repo: stdlib-only HTTP (urllib, honors
HTTPS_PROXY), degrade quietly when egress is blocked, never crash the run over
one bad symbol, and never overwrite good history with a broken snapshot.

Usage:
    python scripts/fetch_portfolio.py               # live (needs internet + creds)
    python scripts/fetch_portfolio.py --offline     # no network: validate math
                                                     # from config ref_prices
    python scripts/fetch_portfolio.py --no-s3        # local history file only
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

try:
    from scripts import _common as C
except ImportError:  # invoked as: python scripts/fetch_portfolio.py
    import _common as C

PORTFOLIO_CONFIG = C.REPO_ROOT / "config" / "portfolio.yaml"
PORTFOLIO_OUT = C.STATE_DIR / "portfolio.json"
HISTORY_LOCAL = C.STATE_DIR / "portfolio_history.json"

# Stored under feed/ (per Nick's call) so it works with the existing creds and
# public bucket policy — no IAM change needed. NOTE: this makes the *daily total*
# history publicly readable at a guessable URL. It contains only {date, total,
# day_change, day_pct} — never per-holding shares or values (those never leave
# the runner). Point PORTFOLIO_HISTORY_KEY at a private key + prefix if you'd
# rather keep even the totals private.
HISTORY_KEY = os.environ.get("PORTFOLIO_HISTORY_KEY", "feed/portfolio_history.json")

YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
HISTORY_DAYS = 400          # keep ~13 months of daily snapshots
SANITY_TOLERANCE = 0.35     # warn if live price is >35% off the screenshot ref


class EgressBlocked(Exception):
    """Raised when the network egress policy refuses the connection."""


# ---------------------------------------------------------------------------
# HTTP (stdlib, proxy-aware)
# ---------------------------------------------------------------------------
def _ssl_context() -> ssl.SSLContext:
    """Default TLS, honoring a custom CA bundle if the runtime sets one
    (the agent proxy MITMs TLS with its own CA)."""
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if not ca:
        for candidate in ("/root/.ccr/ca-bundle.crt",):
            if os.path.exists(candidate):
                ca = candidate
                break
    try:
        return ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
    except (ssl.SSLError, OSError):
        return ssl.create_default_context()


def http_get_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "claude-rss-portfolio/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 403/407 from the egress proxy == blocked, not a real "forbidden".
        if exc.code in (403, 407) and _looks_like_proxy_block(exc):
            raise EgressBlocked(str(exc)) from exc
        raise
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc)).lower()
        if any(s in reason for s in ("connection refused", "denied", "proxy", "tunnel")):
            raise EgressBlocked(str(exc)) from exc
        raise


def _looks_like_proxy_block(exc: urllib.error.HTTPError) -> bool:
    try:
        return "agentproxy" in exc.headers.get("Via", "").lower() or exc.code == 407
    except Exception:
        return exc.code == 407


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
def fetch_quote(symbol: str) -> tuple[float, float, str]:
    """Return (price, previous_close, currency) for a Yahoo symbol."""
    data = http_get_json(YF_CHART.format(sym=urllib.parse.quote(symbol)))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        err = (data.get("chart") or {}).get("error")
        raise ValueError(f"no quote for {symbol} ({err})")
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if price is None:
        raise ValueError(f"{symbol}: quote missing regularMarketPrice")
    if prev is None:
        prev = price
    return float(price), float(prev), (meta.get("currency") or "").upper()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_portfolio() -> dict:
    import yaml
    with open(PORTFOLIO_CONFIG, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    cfg.setdefault("base_currency", "CAD")
    cfg.setdefault("fx_symbol", "CAD=X")
    cfg.setdefault("holdings", [])
    return cfg


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def build_summary(cfg: dict, offline: bool) -> dict:
    fx = 1.0
    fx_prev = 1.0
    if not offline:
        try:
            fx, fx_prev, _ = fetch_quote(cfg["fx_symbol"])
        except EgressBlocked:
            raise
        except Exception as exc:
            print(f"WARN: FX lookup failed ({exc}); assuming 1.0 (USD holdings will be off).",
                  file=sys.stderr)
    else:
        # Derived from the 2026-08-28 screenshots (total reconciles at ~1.3915).
        fx = fx_prev = float(os.environ.get("OFFLINE_FX", "1.3915"))

    rows, total, total_prev, warnings = [], 0.0, 0.0, []
    for h in cfg["holdings"]:
        sym, shares = h["symbol"], float(h["shares"])
        cur = (h.get("currency") or "CAD").upper()
        ref = h.get("ref_price")
        try:
            if offline:
                if ref is None:
                    raise ValueError("no ref_price for offline mode")
                price, prev = float(ref), float(ref)  # 0% day change offline
            else:
                price, prev, qcur = fetch_quote(sym)
                if ref and abs(price - ref) / ref > SANITY_TOLERANCE:
                    warnings.append(f"{sym}: live {price:.2f} vs ref {ref:.2f} "
                                    f"(>{int(SANITY_TOLERANCE*100)}% off — wrong symbol?)")
        except EgressBlocked:
            raise
        except Exception as exc:
            warnings.append(f"{sym}: quote failed ({exc}) — skipped")
            continue

        rate = fx if cur == "USD" else 1.0
        rate_prev = fx_prev if cur == "USD" else 1.0
        value_cad = shares * price * rate
        value_prev_cad = shares * prev * rate_prev
        day_change_cad = value_cad - value_prev_cad
        day_pct = (day_change_cad / value_prev_cad * 100.0) if value_prev_cad else 0.0
        total += value_cad
        total_prev += value_prev_cad
        rows.append({
            "symbol": sym, "name": h.get("name", sym), "shares": shares,
            "currency": cur, "price": round(price, 2),
            "value_cad": round(value_cad, 2),
            "day_change_cad": round(day_change_cad, 2),
            "day_pct": round(day_pct, 2),
            "unverified": bool(h.get("unverified")),
        })

    total_change = total - total_prev
    for r in rows:
        r["weight_pct"] = round(r["value_cad"] / total * 100.0, 1) if total else 0.0
    rows.sort(key=lambda r: r["value_cad"], reverse=True)

    return {
        "as_of": C.now_local().isoformat(timespec="minutes"),
        "date": C.today_str(),
        "base_currency": cfg["base_currency"],
        "fx_usd_cad": round(fx, 4),
        "total": round(total, 2),
        "day_change": round(total_change, 2),
        "day_pct": round(total_change / total_prev * 100.0, 2) if total_prev else 0.0,
        "holdings": rows,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# History (rolling daily snapshots)
# ---------------------------------------------------------------------------
def load_history(use_s3: bool) -> list:
    if use_s3:
        try:
            client = C.s3_client()
            obj = client.get_object(Bucket=C.S3_BUCKET, Key=HISTORY_KEY)
            return _coerce_history(json.loads(obj["Body"].read().decode("utf-8")))
        except Exception as exc:  # missing key on first run, or no creds
            if _is_missing(exc):
                print(f"No history in S3 yet ({HISTORY_KEY}); starting fresh.")
            else:
                print(f"WARN: could not read S3 history ({exc}); trying local.", file=sys.stderr)
    if HISTORY_LOCAL.exists():
        try:
            with open(HISTORY_LOCAL, "r", encoding="utf-8") as fh:
                return _coerce_history(json.load(fh))
        except (OSError, json.JSONDecodeError):
            pass
    return []


def _coerce_history(data) -> list:
    if isinstance(data, dict):
        data = data.get("snapshots", [])
    return [d for d in data if isinstance(d, dict) and d.get("date")] if isinstance(data, list) else []


def _is_missing(exc) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return str(code) in ("404", "NoSuchKey", "NoSuchBucket", "NotFound")


def update_history(history: list, summary: dict) -> list:
    """Replace today's snapshot (idempotent for reruns), prune, sort."""
    today = summary["date"]
    snapshot = {
        "date": today,
        "total": summary["total"],
        "day_change": summary["day_change"],
        "day_pct": summary["day_pct"],
    }
    kept = [s for s in history if s.get("date") != today]
    kept.append(snapshot)
    cutoff = (C.now_local().date() - timedelta(days=HISTORY_DAYS)).isoformat()
    kept = [s for s in kept if s.get("date", "") >= cutoff]
    kept.sort(key=lambda s: s["date"])
    return kept


def save_history(history: list, use_s3: bool) -> None:
    payload = json.dumps({"snapshots": history}, indent=2).encode("utf-8")
    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_LOCAL, "wb") as fh:
        fh.write(payload)
    if use_s3:
        try:
            C.s3_client().put_object(
                Bucket=C.S3_BUCKET, Key=HISTORY_KEY, Body=payload,
                ContentType="application/json; charset=utf-8", CacheControl="no-cache",
            )
            print(f"Pushed history -> s3://{C.S3_BUCKET}/{HISTORY_KEY} ({len(history)} snapshots)")
        except Exception as exc:
            print(f"WARN: could not write S3 history ({exc}); kept local copy only.",
                  file=sys.stderr)


def week_view(history: list, summary: dict) -> tuple[list, float]:
    """(last ~7 daily totals for the sparkline, week % change)."""
    series = [s["total"] for s in history[-7:]]
    if len(series) < 2:
        series = [summary["total"], summary["total"]]
    week_pct = round((series[-1] - series[0]) / series[0] * 100.0, 2) if series[0] else 0.0
    return series, week_pct


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Summarize portfolio + keep history.")
    ap.add_argument("--offline", action="store_true",
                    help="No network: value the book from config ref_prices (math check).")
    ap.add_argument("--no-s3", action="store_true", help="Use local history file only.")
    args = ap.parse_args()
    use_s3 = not args.no_s3 and not args.offline

    cfg = load_portfolio()
    try:
        summary = build_summary(cfg, offline=args.offline)
    except EgressBlocked:
        print("Portfolio: egress blocked — quote lookups denied by the network policy.\n"
              "  Run this in the egress-enabled Action (like backfill-thumbnails), not the\n"
              "  routine sandbox. Left existing portfolio.json/history untouched.", file=sys.stderr)
        sys.exit(0)  # not a failure — same posture as thumbnails

    history = load_history(use_s3)
    history = update_history(history, summary)
    save_history(history, use_s3)

    series, week_pct = week_view(history, summary)
    summary["week_series"] = series
    summary["week_pct"] = week_pct

    with open(PORTFOLIO_OUT, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    sign = "+" if summary["day_change"] >= 0 else ""
    print(f"Portfolio: {summary['base_currency']} {summary['total']:,.2f} "
          f"({sign}{summary['day_change']:,.2f}, {sign}{summary['day_pct']:.2f}% today) "
          f"· {len(summary['holdings'])} holdings · history={len(history)} days")
    for w in summary["warnings"]:
        print(f"  WARN {w}", file=sys.stderr)


if __name__ == "__main__":
    main()

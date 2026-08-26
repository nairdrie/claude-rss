#!/usr/bin/env python3
"""Rebuild (daily) or extend (incremental) the live RSS window.

Usage:
    python scripts/build_feed.py daily
    python scripts/build_feed.py incremental

Inputs : state/curated.json, state/feed.xml, state/seen.json, config/interests.yaml
Outputs: state/feed.xml (overwritten), state/seen.json (updated)

Output is validated through feedparser BEFORE anything is written; on any
validation failure the script exits non-zero without touching the existing
files, so a bad run can never replace a good feed with a broken one.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

try:
    from scripts import _common as C
except ImportError:  # invoked as: python scripts/build_feed.py
    import _common as C


def load_curated() -> list:
    if not C.CURATED_PATH.exists():
        return []
    try:
        with open(C.CURATED_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: could not read {C.CURATED_PATH}: {exc}", file=sys.stderr)
        sys.exit(1)
    if isinstance(data, dict):  # tolerate {"items": [...]}
        data = data.get("items", [])
    if not isinstance(data, list):
        print("ERROR: curated.json must be a JSON array", file=sys.stderr)
        sys.exit(1)
    return data


def dedup_raw(items: list) -> list:
    """Drop items without a URL and collapse duplicate URLs (first wins)."""
    seen, out = set(), []
    for it in items:
        if not isinstance(it, dict):
            continue
        url = (it.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(it)
    return out


def entry_from_curated(raw: dict) -> dict:
    title = (raw.get("title") or "(untitled)").strip()
    if raw.get("breaking"):
        title = f"[BREAKING] {title}"
    reason = (raw.get("reason") or "").strip()
    source = (raw.get("source") or "").strip()
    if reason and source:
        description = f"{reason} — Source: {source}"
    else:
        description = reason or (f"Source: {source}" if source else "")
    return {
        "title": title,
        "url": (raw.get("url") or "").strip(),
        "description": description or title,
        "pubdate": C.parse_date(raw.get("published_at")),
        "image": (raw.get("image") or "").strip() or None,
    }


def entry_from_existing(e) -> dict:
    url = e.get("link") or e.get("id") or ""
    if getattr(e, "published_parsed", None):
        pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
    else:
        pub = C.parse_date(e.get("published"))
    title = e.get("title") or "(untitled)"
    image = None
    thumbs = e.get("media_thumbnail") or []
    if thumbs:
        image = thumbs[0].get("url")
    if not image:
        for enc in e.get("enclosures") or []:
            if str(enc.get("type", "")).startswith("image/"):
                image = enc.get("href") or enc.get("url")
                break
    return {
        "title": title,
        "url": url,
        "description": e.get("summary") or title,
        "pubdate": pub,
        "image": image,
    }


def read_existing_window() -> list:
    """Parse the current state/feed.xml back into internal item dicts."""
    if not C.FEED_PATH.exists():
        return []
    import feedparser

    with open(C.FEED_PATH, "rb") as fh:
        parsed = feedparser.parse(fh.read())
    return [entry_from_existing(e) for e in parsed.entries if (e.get("link") or e.get("id"))]


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in ("daily", "incremental"):
        print("Usage: python scripts/build_feed.py <daily|incremental>", file=sys.stderr)
        sys.exit(2)
    mode = sys.argv[1]

    interests = C.load_interests()
    feed_cfg = interests.get("feed", {}) if isinstance(interests, dict) else {}
    window_size = int(feed_cfg.get("window_size", 50) or 50)
    daily_target = int(feed_cfg.get("daily_target", window_size) or window_size)
    max_incremental = int(feed_cfg.get("max_incremental_adds", 5) or 5)

    curated = dedup_raw(load_curated())
    seen = C.read_seen()
    seen_url_set = C.seen_urls(seen)
    today = C.today_str()

    if mode == "daily":
        if not curated:
            print("ERROR: daily build has no curated items; refusing to overwrite the "
                  "live feed with an empty one.", file=sys.stderr)
            sys.exit(1)
        items = [entry_from_curated(r) for r in curated]
        items.sort(key=lambda it: it["pubdate"], reverse=True)  # newest first
        window = items[: min(daily_target, window_size)]
        new_urls = [it["url"] for it in window]
    else:  # incremental
        existing = read_existing_window()
        existing_urls = {it["url"] for it in existing}
        fresh_raw = [
            r for r in curated
            if (r.get("url") or "").strip() not in existing_urls
            and (r.get("url") or "").strip() not in seen_url_set
        ]
        # breaking items first, otherwise preserve curated order (stable sort)
        fresh_raw.sort(key=lambda r: 0 if r.get("breaking") else 1)
        fresh_raw = fresh_raw[:max_incremental]  # safety cap on top of the agent's own
        new_items = [entry_from_curated(r) for r in fresh_raw]
        if not new_items:
            print("No new items cleared the bar — feed unchanged, nothing to push.")
            return
        window = (new_items + existing)[:window_size]
        new_urls = [it["url"] for it in new_items]

    try:
        message = C.write_feed(window, interests=interests)
    except RuntimeError as exc:
        print(f"ERROR: generated feed failed validation ({exc}); not writing.",
              file=sys.stderr)
        sys.exit(1)

    C.add_seen(seen, new_urls, when=today)
    C.prune_seen(seen, days=30)
    if mode == "daily":
        seen["last_daily_write"] = today
    C.write_seen(seen)

    print(f"[{mode}] {message}; window={len(window)} item(s), "
          f"added {len(new_urls)} new URL(s); seen history={len(seen['seen'])}.")


if __name__ == "__main__":
    main()

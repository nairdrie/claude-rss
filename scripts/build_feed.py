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
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

try:
    from scripts import _common as C
except ImportError:  # invoked as: python scripts/build_feed.py
    import _common as C


# Spacing between the synthetic, strictly-decreasing pubDates we assign so the
# feed has a clean, orderable timeline instead of a wall of identical midnights
# (see stagger_pubdates). 13 minutes keeps a full 50-item daily window inside
# the last ~11 hours -- recent, plausible, and well within max_item_age_hours.
STAGGER_GAP = timedelta(minutes=13)


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


def filter_fresh(items: list, max_age_hours) -> list:
    """Hard backstop on freshness: drop candidates older than max_age_hours.

    This exists because "roughly N hours" in the research prompt is guidance,
    not a guarantee -- an agent can still reach for the most recent edition of
    a weekly/monthly series and call it fresh. Unset/falsy max_age_hours means
    no cutoff (back-compat for configs that don't set it). Unparseable dates
    fall back to "now" in parse_date(), so they're never wrongly dropped here.
    """
    if not max_age_hours:
        return items
    cutoff = C.now_local() - timedelta(hours=float(max_age_hours))
    fresh, stale = [], []
    for it in items:
        (fresh if C.parse_date(it.get("published_at")) >= cutoff else stale).append(it)
    if stale:
        dropped = ", ".join((it.get("url") or "(no url)") for it in stale)
        print(f"Dropping {len(stale)} candidate(s) older than {max_age_hours}h: {dropped}")
    return fresh


def domain_of(url: str) -> str:
    """Bare registrable-ish host for a URL ('www.' stripped), '' if unparseable."""
    try:
        host = (urlsplit(url).netloc or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def homepage_of(url: str) -> str:
    """`scheme://host/` for a URL, used as the per-item <source> link.

    The RSS <source> element's url attribute is meant to point at the source's
    feed; we don't have that, so the source's homepage is the best stable link
    we can offer. Returns '' when the URL has no host we can parse.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if not parts.netloc:
        return ""
    return f"{parts.scheme or 'https'}://{parts.netloc}/"


def source_fields(url: str, explicit: str = "") -> tuple[str, str]:
    """(display name, homepage link) for an item's per-item <source>.

    Name is the curator-provided `source` when present, else the bare domain
    ('techcrunch.com') so every item still gets a distinct origin instead of
    falling back to the one channel title. Link is the article's homepage.
    Either may be '' when the URL is unparseable and no source was given.
    """
    name = (explicit or "").strip() or domain_of(url)
    return name, homepage_of(url)


def topic_of(raw: dict) -> str:
    """Interleave bucket key: explicit `topic`, else `source`, else url domain.

    The curator is asked to tag each item with a `topic` (see docs/feed-schema.md)
    so the daily build can spread topics across the window instead of emitting a
    block of crypto followed by a block of tech. When it's missing we fall back
    to the source/domain, which still separates most outlets reasonably.
    """
    for key in ("topic", "source"):
        val = (raw.get(key) or "").strip()
        if val:
            return val.lower()
    return domain_of((raw.get("url") or "").strip()) or "misc"


def interleave_by_topic(items: list) -> list:
    """Spread each topic's items evenly across the list so the feed mixes.

    Items are assumed pre-sorted within a topic (newest/most-relevant first).
    Each item is placed at the fractional position (i + 0.5) / n within its
    topic, then everything is sorted by that fraction -- so a topic with 2 items
    lands them near 1/4 and 3/4 of the way down while a topic with 10 items
    fans out evenly, and no single topic clusters. Stable on equal fractions,
    preserving within-topic order.
    """
    buckets = OrderedDict()
    for it in items:
        buckets.setdefault(it.get("topic") or "misc", []).append(it)
    ranked = []
    for bucket in buckets.values():
        n = len(bucket)
        for i, it in enumerate(bucket):
            ranked.append(((i + 0.5) / n, it))
    ranked.sort(key=lambda pair: pair[0])
    return [it for _, it in ranked]


def stagger_pubdates(items: list, anchor: datetime, gap: timedelta = STAGGER_GAP) -> None:
    """Assign strictly-decreasing synthetic pubDates from just below `anchor`.

    Curated items usually carry only a publication *date* -- web research rarely
    surfaces an exact time -- which parse_date resolves to local midnight. Left
    alone, a whole daily window lands on the same 00:00:00 timestamp: readers
    can't order it and the interleaved topic mix collapses under their pubDate
    sort. Freshness is already enforced upstream on the raw published_at, so here
    we're free to overwrite the live feed's timeline with clean, monotonically
    decreasing stamps that preserve the order we chose. Mutates in place.
    """
    when = anchor - gap
    for it in items:
        it["pubdate"] = when
        when -= gap


def stagger_incremental(new_items: list, existing: list, ceiling: datetime) -> None:
    """Time-stamp an incremental trickle strictly above the existing window.

    New items are the fresh additions, so they must outrank everything already
    in the feed -- the pinned chess puzzle included, so each day's incrementals
    visibly bury it. We space them evenly in the open interval between the
    newest existing item's pubDate (the floor) and `ceiling` (now), so no
    matter how close two builds land they never collide with, or sink beneath,
    the live window. Mutates in place.
    """
    if not new_items:
        return
    floor = max((it["pubdate"] for it in existing), default=ceiling - STAGGER_GAP)
    if floor >= ceiling:  # no room (e.g. a build moments ago) -- open a gap
        floor = ceiling - STAGGER_GAP
    step = (ceiling - floor) / (len(new_items) + 1)
    for i, it in enumerate(new_items, start=1):
        it["pubdate"] = ceiling - step * i


def entry_from_curated(raw: dict) -> dict:
    title = (raw.get("title") or "(untitled)").strip()
    if raw.get("breaking"):
        title = f"[BREAKING] {title}"
    reason = (raw.get("reason") or "").strip()
    source = (raw.get("source") or "").strip()
    url = (raw.get("url") or "").strip()
    if reason and source:
        description = f"{reason} — Source: {source}"
    else:
        description = reason or (f"Source: {source}" if source else "")
    source_name, source_url = source_fields(url, source)
    return {
        "title": title,
        "url": url,
        "description": description or title,
        "pubdate": C.parse_date(raw.get("published_at")),
        "image": (raw.get("image") or "").strip() or None,
        "topic": topic_of(raw),
        "source_name": source_name,
        "source_url": source_url,
    }


def entry_from_existing(e) -> dict:
    url = e.get("link") or e.get("id") or ""
    if getattr(e, "published_parsed", None):
        # feedparser normalizes published_parsed to UTC; convert back to the
        # configured local zone so re-emitted items keep the same -0400/-0500
        # display as freshly built ones instead of flipping to +0000 on every
        # incremental rebuild (same instant, but a jarringly mixed feed).
        pub = datetime(*e.published_parsed[:6], tzinfo=timezone.utc).astimezone(C.tz())
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
    # Recover the per-item <source> feedparser exposes as e.source so an
    # incremental rebuild re-emits it instead of stripping every existing
    # item's origin back to the bare channel. Fall back to the domain when a
    # pre-source item is still in the window.
    src = e.get("source") or {}
    src_name, src_url = "", ""
    if isinstance(src, dict):
        src_name = (src.get("title") or "").strip()
        src_url = (src.get("href") or src.get("url") or "").strip()
    if not src_name or not src_url:
        fb_name, fb_url = source_fields(url)
        src_name = src_name or fb_name
        src_url = src_url or fb_url
    guid_val = e.get("id") or url
    return {
        "title": title,
        "url": url,
        "description": e.get("summary") or title,
        "pubdate": pub,
        "image": image,
        "guid": guid_val,
        # Round-trip isPermaLink structurally so a carried-through pinned item
        # (the chess puzzle) keeps isPermaLink="false": a guid that differs from
        # the link is a synthetic, date-keyed guid (the puzzle's `url#YYYY-MM-DD`)
        # and is NOT a permalink; a guid equal to the link is. feedparser's own
        # `guidislink` can't be used here -- it forces False whenever an item has
        # a <link> (always, in this feed), which would wrongly flip every
        # carried-through item to isPermaLink="false".
        "guid_is_permalink": guid_val == url,
        "source_name": src_name,
        "source_url": src_url,
    }


def read_existing_window() -> list:
    """Parse the current state/feed.xml back into internal item dicts."""
    if not C.FEED_PATH.exists():
        return []
    import feedparser

    with open(C.FEED_PATH, "rb") as fh:
        parsed = feedparser.parse(fh.read())
    return [entry_from_existing(e) for e in parsed.entries if (e.get("link") or e.get("id"))]


def build_pinned(interests: dict, today: str) -> list:
    """Build always-on-top items from interests.yaml's `pinned` list.

    Unlike curated items, these never go through research, the freshness
    cutoff, or seen.json dedup -- their url is expected to be stable day to
    day (e.g. chess.com's daily puzzle page) while the underlying content
    changes daily. Each gets a guid keyed to today's date and isPermaLink
    false, so readers treat each day's instance as a new item even though
    the link itself never changes.
    """
    pinned_cfg = (interests or {}).get("pinned") or []
    if isinstance(pinned_cfg, dict):
        pinned_cfg = [pinned_cfg]
    out = []
    for p in pinned_cfg:
        if not isinstance(p, dict):
            continue
        url = (p.get("url") or "").strip()
        if not url:
            continue
        title = (p.get("title") or "(untitled)").strip()
        reason = (p.get("reason") or "").strip()
        source = (p.get("source") or "").strip()
        if reason and source:
            description = f"{reason} — Source: {source}"
        else:
            description = reason or (f"Source: {source}" if source else title)
        source_name, source_url = source_fields(url, source)
        out.append({
            "title": f"{title} — {today}",
            "url": url,
            "description": description,
            "pubdate": C.now_local(),
            "image": (p.get("image") or "").strip() or None,
            "guid": f"{url}#{today}",
            "guid_is_permalink": False,
            "source_name": source_name,
            "source_url": source_url,
        })
    return out


def pin_to_top(items: list, pinned: list, window_size: int) -> list:
    """Prepend pinned items, evicting any stale copy of them by url, then trim."""
    if not pinned:
        return items[:window_size]
    pinned_urls = {p["url"] for p in pinned}
    rest = [it for it in items if it["url"] not in pinned_urls]
    return (pinned + rest)[:window_size]


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

    curated = filter_fresh(dedup_raw(load_curated()), feed_cfg.get("max_item_age_hours"))
    seen = C.read_seen()
    seen_url_set = C.seen_urls(seen)
    today = C.today_str()

    if mode == "daily":
        pinned = build_pinned(interests, today)
        if not curated:
            print("ERROR: daily build has no curated items; refusing to overwrite the "
                  "live feed with an empty one.", file=sys.stderr)
            sys.exit(1)
        items = [entry_from_curated(r) for r in curated]
        # Pick the top set by recency/curation first, then interleave topics so
        # the window reads as a mix rather than a block per topic, then lay a
        # clean descending timeline over it (see stagger_pubdates) so a reader's
        # pubDate sort preserves that mix instead of collapsing on midnight.
        items.sort(key=lambda it: it["pubdate"], reverse=True)  # newest first
        selected = items[: min(daily_target, window_size)]
        selected = interleave_by_topic(selected)
        stagger_pubdates(selected, anchor=C.now_local())
        window = pin_to_top(selected, pinned, window_size)
        new_urls = [it["url"] for it in selected]
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
        # Stamp the trickle above the ENTIRE existing window (breaking-first
        # order preserved) so fresh items sit on top and never inherit a
        # midnight tie -- including above the pinned chess puzzle. Incremental
        # deliberately does NOT re-pin the puzzle: it keeps the pubDate the
        # morning's daily build gave it and sinks down the feed as the day's
        # additions land above it (build_pinned/pin_to_top run in daily mode
        # only). The existing window is carried through as-is, so the puzzle's
        # date-keyed guid rides along; the next daily rebuild puts a fresh
        # puzzle back on top.
        stagger_incremental(new_items, existing, C.now_local())
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

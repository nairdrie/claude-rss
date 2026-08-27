"""Shared configuration, paths, and helpers for the feed-curator scripts.

Import-safe by design: importing this module never touches the network and
never requires boto3. The only heavy/optional dependency (boto3) is imported
lazily inside :func:`s3_client`, so ``build_feed.py`` — which never talks to
S3 — imports and runs even when boto3 is not installed.

RSS is written with the Python standard library (``xml.etree.ElementTree``) so
there is no compiled/build-from-source dependency (feedgen -> lxml) to fail on a
minimal runtime; ``feedparser`` (a pure-Python wheel) validates the output and
parses the existing feed.

Every deployment specific (bucket, keys, region, timezone) has a baked-in
default from the bootstrap plus an environment-variable override, so real
values stay out of the code while the scripts keep working defaults. See
``docs/s3-access.md`` for the full list of env vars.
"""
from __future__ import annotations

import json
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import yaml

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None


# ---------------------------------------------------------------------------
# Paths (resolved from this file, so the scripts work from any cwd)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "interests.yaml"
STATE_DIR = REPO_ROOT / "state"
FEED_PATH = STATE_DIR / "feed.xml"
SEEN_PATH = STATE_DIR / "seen.json"
CURATED_PATH = STATE_DIR / "curated.json"

ATOM_NS = "http://www.w3.org/2005/Atom"
MEDIA_NS = "http://search.yahoo.com/mrss/"
DC_NS = "http://purl.org/dc/elements/1.1/"


# ---------------------------------------------------------------------------
# S3 coordinates (env-overridable; defaults chosen at bootstrap)
# ---------------------------------------------------------------------------
S3_BUCKET = os.environ.get("RSS_S3_BUCKET", "nairdrie-rss-feed")
FEED_KEY = os.environ.get("RSS_FEED_KEY", "feed/merged.xml")
STATE_KEY = os.environ.get("RSS_STATE_KEY", "feed/seen.json")
AWS_REGION = (
    os.environ.get("RSS_S3_REGION")
    or os.environ.get("AWS_REGION")
    or os.environ.get("AWS_DEFAULT_REGION")
    or "us-east-1"
)

# The nairdrie-rss-feed bucket is public via a bucket *policy* scoped to
# feed/*, so by default we send NO per-object ACL. Sending "public-read" would
# FAIL on a bucket that has ACLs disabled ("bucket owner enforced" — the
# modern default). Set RSS_FEED_ACL=public-read only if your bucket instead
# grants read via object ACLs.
FEED_ACL = os.environ.get("RSS_FEED_ACL") or None

# Cache lifetime (seconds) for the feed object — short, since it changes during
# the day. Override with RSS_FEED_CACHE_SECONDS.
FEED_CACHE_SECONDS = int(os.environ.get("RSS_FEED_CACHE_SECONDS", "300"))


# ---------------------------------------------------------------------------
# Timezone / dates
# ---------------------------------------------------------------------------
TIMEZONE = os.environ.get("RSS_TIMEZONE", "America/Toronto")


def tz():
    """Return the configured tzinfo, falling back to UTC if unavailable."""
    if ZoneInfo is not None:
        try:
            return ZoneInfo(TIMEZONE)
        except Exception:  # pragma: no cover - missing tzdata
            pass
    return timezone.utc


def now_local() -> datetime:
    """Timezone-aware 'now' in the configured timezone (DST handled)."""
    return datetime.now(tz())


def today_str() -> str:
    """Today's date as YYYY-MM-DD in the configured timezone."""
    return now_local().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_interests() -> dict:
    """Load config/interests.yaml (returns {} if empty)."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# Date parsing / formatting
# ---------------------------------------------------------------------------
def parse_date(value) -> datetime:
    """Parse an ISO-8601 or RFC-822 date into an aware datetime.

    Naive inputs are assumed to be in the configured timezone. Anything
    unparseable falls back to 'now' so one bad date never crashes a build.
    """
    if not value:
        return now_local()
    text = str(value).strip()

    # ISO 8601 (Python 3.11's fromisoformat is lenient; normalize a trailing Z)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz())
        return dt
    except ValueError:
        pass

    # RFC 822 (e.g. "Tue, 26 Aug 2026 13:00:00 -0400")
    try:
        dt = parsedate_to_datetime(text)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except (TypeError, ValueError):
        pass

    return now_local()


def to_rfc822(dt: datetime) -> str:
    """Format an aware datetime as an RFC 822 pubDate string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz())
    return format_datetime(dt)


# ---------------------------------------------------------------------------
# seen.json
# ---------------------------------------------------------------------------
def default_seen() -> dict:
    return {"last_daily_write": None, "seen": []}


def read_seen() -> dict:
    """Read state/seen.json, normalizing shapes. Missing/corrupt -> default."""
    data = {}
    if SEEN_PATH.exists():
        try:
            with open(SEEN_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            data = {}
    if not isinstance(data, dict):
        data = {}

    data.setdefault("last_daily_write", None)
    normalized = []
    for entry in data.get("seen") or []:
        if isinstance(entry, dict) and entry.get("url"):
            normalized.append(
                {"url": entry["url"], "first_seen": entry.get("first_seen") or today_str()}
            )
        elif isinstance(entry, str):
            normalized.append({"url": entry, "first_seen": today_str()})
    data["seen"] = normalized
    return data


def write_seen(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(SEEN_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def seen_urls(seen: dict) -> set:
    return {e["url"] for e in seen.get("seen", []) if isinstance(e, dict) and e.get("url")}


def add_seen(seen: dict, urls, when: str | None = None) -> dict:
    """Record URLs (first_seen = today unless given), preserving existing dates."""
    when = when or today_str()
    known = {e["url"]: e for e in seen.get("seen", []) if isinstance(e, dict) and e.get("url")}
    for url in urls:
        if url and url not in known:
            known[url] = {"url": url, "first_seen": when}
    seen["seen"] = list(known.values())
    return seen


def prune_seen(seen: dict, days: int = 30) -> dict:
    """Drop seen entries whose first_seen is older than ``days`` days."""
    cutoff = now_local().date() - timedelta(days=days)
    kept = []
    for entry in seen.get("seen", []):
        try:
            when = datetime.strptime(entry.get("first_seen", ""), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            when = now_local().date()  # keep undated entries rather than lose them
        if when >= cutoff:
            kept.append(entry)
    seen["seen"] = kept
    return seen


# ---------------------------------------------------------------------------
# RSS construction (stdlib) + validation (feedparser)
# ---------------------------------------------------------------------------
def _feed_meta(interests: dict | None) -> tuple[str, str, str]:
    feed_cfg = (interests or {}).get("feed", {}) if interests else {}
    title = feed_cfg.get("title") or "Nick's Curated Feed"
    description = feed_cfg.get("description") or "AI-curated personal feed"
    link = feed_cfg.get("link") or "https://nairdrie-rss-feed.s3.us-east-1.amazonaws.com/feed/merged.xml"
    return title, description, link


def build_rss_bytes(interests: dict | None, items: list) -> bytes:
    """Build a pretty-printed RSS 2.0 document from channel meta + item dicts.

    Each item dict: {title, url, description, pubdate(aware datetime),
    image(optional thumbnail URL or None), guid(optional, defaults to url),
    guid_is_permalink(optional bool, defaults to True),
    source_name/source_url(optional)}. guid/guid_is_permalink
    exist for pinned items whose url is stable but whose content changes daily
    (e.g. the chess puzzle) -- giving each day's instance a distinct,
    non-permalink guid is what makes readers treat it as a new item instead of
    silently ignoring the "same" link. source_name/source_url give each item its
    own originating "channel" so the single feed reads as an aggregation of many:
    source_name is emitted both as the RSS 2.0 <source> element (the spec's
    per-item channel-of-origin) and as <dc:creator> (the byline most readers
    actually render per item). Items are emitted in the given order
    (index 0 == top of feed). ElementTree escapes all text/attribute content
    automatically.
    """
    title, description, link = _feed_meta(interests)

    rss = ET.Element("rss", {"version": "2.0", "xmlns:atom": ATOM_NS,
                             "xmlns:media": MEDIA_NS, "xmlns:dc": DC_NS})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = title
    ET.SubElement(channel, "link").text = link
    ET.SubElement(channel, "description").text = description
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "generator").text = "claude-rss feed curator"
    ET.SubElement(channel, "docs").text = "https://www.rssboard.org/rss-specification"
    ET.SubElement(channel, "lastBuildDate").text = to_rfc822(now_local())
    # atom:self link (literal prefix; xmlns:atom declared on the root above)
    ET.SubElement(channel, "atom:link", {"href": link, "rel": "self",
                                         "type": "application/rss+xml"})

    for it in items:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = it["title"]
        ET.SubElement(item, "link").text = it["url"]
        is_permalink = it.get("guid_is_permalink", True)
        guid = ET.SubElement(item, "guid", {"isPermaLink": "true" if is_permalink else "false"})
        guid.text = it.get("guid") or it["url"]
        ET.SubElement(item, "description").text = it["description"]
        ET.SubElement(item, "pubDate").text = to_rfc822(it["pubdate"])
        # Per-item source ("originating channel") so the single feed reads as an
        # aggregation of distinct sources rather than one monolithic channel.
        # <source> is the RSS 2.0 element for exactly this; <dc:creator> carries
        # the same name because it's the byline most readers surface per item.
        source_name = (it.get("source_name") or "").strip()
        if source_name:
            # <source>'s url attribute is required; fall back to the item link.
            source_url = (it.get("source_url") or "").strip() or it["url"]
            ET.SubElement(item, "source", {"url": source_url}).text = source_name
            ET.SubElement(item, "dc:creator").text = source_name
        image = it.get("image")
        if image:
            mime, _ = mimetypes.guess_type(image)
            if not mime or not mime.startswith("image/"):
                mime = "image/jpeg"
            # media:thumbnail is what most readers (Feedly, Inoreader, NetNewsWire)
            # look for; the enclosure is a widely-supported fallback for the rest.
            ET.SubElement(item, "media:thumbnail", {"url": image})
            ET.SubElement(item, "enclosure", {"url": image, "type": mime, "length": "0"})

    ET.indent(rss, space="  ")
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def validate_rss(xml_bytes) -> tuple[bool, str]:
    """Round-trip produced XML through feedparser. Returns (ok, message)."""
    import feedparser

    parsed = feedparser.parse(xml_bytes)
    version = parsed.get("version", "") or ""
    if not version.startswith("rss"):
        return False, f"not recognized as RSS (version={version!r})"
    if parsed.get("bozo"):
        return False, f"malformed feed: {parsed.get('bozo_exception')}"
    if not parsed.feed.get("title"):
        return False, "channel is missing a <title>"
    return True, f"valid {version} with {len(parsed.entries)} item(s)"


def write_feed(items: list, interests: dict | None = None, path: Path | None = None) -> str:
    """Build + validate + write a feed. Raises on validation failure (no write)."""
    path = path or FEED_PATH
    xml = build_rss_bytes(interests, items)
    ok, message = validate_rss(xml)
    if not ok:
        raise RuntimeError(message)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(xml)
    return message


def write_empty_feed(path: Path | None = None, interests: dict | None = None) -> None:
    """Write a valid, item-less RSS 2.0 feed (used to seed the first run)."""
    if interests is None:
        try:
            interests = load_interests()
        except OSError:
            interests = {}
    write_feed([], interests=interests, path=path)


# ---------------------------------------------------------------------------
# S3 client (boto3 imported lazily so non-S3 scripts stay import-clean)
# ---------------------------------------------------------------------------
def s3_client():
    import boto3

    return boto3.client("s3", region_name=AWS_REGION)

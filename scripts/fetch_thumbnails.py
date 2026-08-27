#!/usr/bin/env python3
"""Best-effort og:image scraper for state/curated.json.

Fetches each curated item's article page and looks for a thumbnail via (in
priority order) og:image:secure_url, og:image, twitter:image meta tags, then
writes an "image" field back onto items that don't already have one.

Stdlib-only (urllib + a small regex) so this doesn't pull in a new
dependency just to read a handful of <meta> tags. A failure on any single
item (timeout, 403, no matching tag, unparseable HTML, ...) just leaves that
item without an image -- it never aborts the run or touches the other items.

Usage:
    python scripts/fetch_thumbnails.py
"""
from __future__ import annotations

import json
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

try:
    from scripts import _common as C
except ImportError:  # invoked as: python scripts/fetch_thumbnails.py
    import _common as C

TIMEOUT_SECONDS = 8
MAX_BYTES = 200_000  # meta tags live in <head>; no need to read the whole page
USER_AGENT = (
    "Mozilla/5.0 (compatible; claude-rss-curator/1.0; "
    "+https://github.com/nairdrie/claude-rss)"
)

META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
ATTR_RE = re.compile(r'(\w[\w:.-]*)\s*=\s*"([^"]*)"|(\w[\w:.-]*)\s*=\s*\'([^\']*)\'')

# Priority order: prefer the most reliable/highest-res source first.
IMAGE_META_KEYS = ("og:image:secure_url", "og:image", "twitter:image")


def _parse_attrs(tag: str) -> dict:
    attrs = {}
    for m in ATTR_RE.finditer(tag):
        if m.group(1) is not None:
            attrs[m.group(1).lower()] = m.group(2)
        else:
            attrs[m.group(3).lower()] = m.group(4)
    return attrs


def find_thumbnail(html: str, page_url: str) -> str | None:
    candidates = {}
    for tag in META_TAG_RE.findall(html):
        attrs = _parse_attrs(tag)
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = (attrs.get("content") or "").strip()
        if key in IMAGE_META_KEYS and content:
            candidates.setdefault(key, content)
    for key in IMAGE_META_KEYS:
        if key in candidates:
            return urljoin(page_url, candidates[key])
    return None


def fetch_thumbnail(url: str) -> str | None:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read(MAX_BYTES)
    except (HTTPError, URLError, TimeoutError, OSError):
        return None
    html = raw.decode("utf-8", errors="replace")
    return find_thumbnail(html, url)


def main() -> None:
    if not C.CURATED_PATH.exists():
        print(f"No {C.CURATED_PATH}; nothing to enrich.")
        return
    with open(C.CURATED_PATH, "r", encoding="utf-8") as fh:
        try:
            items = json.load(fh)
        except json.JSONDecodeError as exc:
            print(f"ERROR: could not parse {C.CURATED_PATH}: {exc}", file=sys.stderr)
            sys.exit(1)
    if not isinstance(items, list):
        print("ERROR: curated.json must be a JSON array", file=sys.stderr)
        sys.exit(1)

    found = 0
    for item in items:
        if not isinstance(item, dict) or item.get("image"):
            continue  # already has one (e.g. supplied by a research agent)
        url = (item.get("url") or "").strip()
        if not url:
            continue
        image = fetch_thumbnail(url)
        if image:
            item["image"] = image
            found += 1

    with open(C.CURATED_PATH, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(f"Thumbnails: found {found}/{len(items)} item(s).")


if __name__ == "__main__":
    main()

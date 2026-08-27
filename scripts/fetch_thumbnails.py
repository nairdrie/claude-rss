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

import gzip
import html as html_lib
import json
import re
import sys
import zlib
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

try:
    from scripts import _common as C
except ImportError:  # invoked as: python scripts/fetch_thumbnails.py
    import _common as C

TIMEOUT_SECONDS = 8
MAX_BYTES = 300_000  # meta tags live in <head>; no need to read the whole page
# A real browser UA: many outlets 403 unknown bot agents, and og:image scraping
# is exactly what a link-preview/social crawler does, so we present as one.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
ATTR_RE = re.compile(r'(\w[\w:.-]*)\s*=\s*"([^"]*)"|(\w[\w:.-]*)\s*=\s*\'([^\']*)\'')

# Priority order: prefer the most reliable/highest-res source first.
IMAGE_META_KEYS = (
    "og:image:secure_url",
    "og:image:url",
    "og:image",
    "twitter:image",
    "twitter:image:src",
)


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
            # Meta content is HTML-escaped, so a query string arrives as
            # ...?a=1&amp;b=2 -- unescape it or the image URL 404s in readers.
            return urljoin(page_url, html_lib.unescape(candidates[key]))
    # Last resort: the older <link rel="image_src" href="..."> preview hint.
    for tag in LINK_TAG_RE.findall(html):
        attrs = _parse_attrs(tag)
        if "image_src" in (attrs.get("rel") or "").lower():
            href = (attrs.get("href") or "").strip()
            if href:
                return urljoin(page_url, html_lib.unescape(href))
    return None


def _decode_body(raw: bytes, encoding: str) -> str:
    """Decode a (possibly compressed) response body to text, best-effort."""
    enc = (encoding or "").lower()
    try:
        if "gzip" in enc:
            raw = gzip.decompress(raw)
        elif "deflate" in enc:
            raw = zlib.decompress(raw)
    except (OSError, zlib.error, EOFError):
        # A truncated compressed stream (we only read MAX_BYTES) can't be fully
        # inflated; fall through and let the replace-decode salvage what it can.
        pass
    return raw.decode("utf-8", errors="replace")


def fetch_thumbnail(url: str) -> tuple[str | None, str | None]:
    """Return (image_url, error). error is None on a successful fetch (even when
    no og:image was found), else a short classification for the run summary."""
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            # Ask for uncompressed HTML; we only read the <head>, and a partial
            # gzip stream can't be inflated. _decode_body still handles the case
            # where a CDN compresses anyway and the body fits in MAX_BYTES.
            "Accept-Encoding": "identity",
        },
    )
    try:
        with urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read(MAX_BYTES)
            content_encoding = resp.headers.get("Content-Encoding", "")
    except HTTPError as exc:
        return None, f"http-{exc.code}"
    except (TimeoutError, URLError, OSError) as exc:
        return None, _classify_error(exc)
    html = _decode_body(raw, content_encoding)
    return find_thumbnail(html, url), None


def _classify_error(exc: Exception) -> str:
    """Bucket a fetch exception into a stable label for the run summary.

    A policy proxy denies a domain by refusing the HTTPS CONNECT, which urllib
    surfaces as URLError(OSError('Tunnel connection failed: 403 Forbidden')) --
    not an HTTPError -- so we sniff the message to flag egress blocks distinctly
    from ordinary timeouts or DNS errors.
    """
    reason = getattr(exc, "reason", exc)
    text = str(reason).lower()
    if "tunnel connection failed" in text or "403" in text or "407" in text:
        return "egress-blocked"
    if isinstance(reason, TimeoutError) or "timed out" in text:
        return "timeout"
    return f"fetch-error ({type(reason).__name__})"


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

    found = already = attempted = no_tag = 0
    errors: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("image"):
            already += 1
            continue  # already has one (e.g. supplied by a research agent)
        url = (item.get("url") or "").strip()
        if not url:
            continue
        attempted += 1
        image, error = fetch_thumbnail(url)
        if image:
            item["image"] = image
            found += 1
        elif error:
            errors[error] = errors.get(error, 0) + 1
        else:
            no_tag += 1

    with open(C.CURATED_PATH, "w", encoding="utf-8") as fh:
        json.dump(items, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    summary = (
        f"Thumbnails: {found} scraped, {already} already had one, "
        f"{no_tag} fetched without an og:image, {sum(errors.values())} fetch "
        f"failures (of {attempted} attempted) across {len(items)} item(s)."
    )
    if errors:
        breakdown = ", ".join(f"{k}×{v}" for k, v in sorted(errors.items()))
        summary += f"\n  fetch failures: {breakdown}"
        blocked = sum(
            v for k, v in errors.items() if k in ("egress-blocked", "http-403", "http-407")
        )
        if blocked and blocked >= attempted / 2:
            summary += (
                "\n  NOTE: most fetches were denied — the environment's network "
                "egress policy is blocking these domains, so og:image scraping "
                "can't reach them. Broaden egress for the feed environment "
                "(see docs/s3-access.md → Network egress) to enable thumbnails."
            )
    print(summary)


if __name__ == "__main__":
    main()

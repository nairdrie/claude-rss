#!/usr/bin/env python3
"""Publish the Morning Line: upload the rendered page + cover to S3 and pin the
item to the top of the live feed.

Steps:
  1. Upload state/dashboard.html -> feed/apps/latest/dashboard.html (+ dated archive)
     and state/cover.png -> feed/apps/latest/cover.png (+ dated archive).
  2. Download the live feed, drop any previous Morning Line item, prepend a fresh
     one (link -> dashboard, media:thumbnail -> date-busted cover), validate, and
     conditional-write it back (an in-flight routine build wins, never clobbered).

The item uses a date-keyed, non-permalink guid so readers treat each morning as a
new item even though the URL is stable — the same trick build_feed.py uses for the
chess puzzle. Adding the Morning Line to interests.yaml's `pinned` list keeps it
durable across the routine's daily rebuilds; this script makes it appear/refresh
immediately between those rebuilds.

Usage:
    python scripts/publish_dashboard.py            # upload + inject (needs creds)
    python scripts/publish_dashboard.py --dry-run  # build the item, don't write
"""
from __future__ import annotations

import argparse
import sys
from xml.etree import ElementTree as ET

try:
    from scripts import _common as C
except ImportError:
    import _common as C

DASH_HTML = C.STATE_DIR / "dashboard.html"
COVER_PNG = C.STATE_DIR / "cover.png"


def _cfg() -> dict:
    import yaml
    with open(C.REPO_ROOT / "config" / "dashboard.yaml", "r", encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("feed_item", {}) or {}


def _put(client, key: str, body: bytes, content_type: str) -> None:
    extra = {"ContentType": content_type, "CacheControl": "no-cache"}
    if C.FEED_ACL:
        extra["ACL"] = C.FEED_ACL
    client.put_object(Bucket=C.S3_BUCKET, Key=key, Body=body, **extra)
    print(f"  uploaded s3://{C.S3_BUCKET}/{key} ({len(body)} bytes)")


def build_item(fi: dict, today: str) -> ET.Element:
    base = fi["public_base"].rstrip("/")
    dash_url = f"{base}/{fi['dashboard_key']}"
    cover_url = f"{base}/{fi['cover_key']}?d={today}"   # date-bust the thumbnail
    source = fi.get("source", "The Morning Line")

    item = ET.Element("item")
    ET.SubElement(item, "title").text = f"{fi.get('title', 'The Morning Line')} — {today}"
    ET.SubElement(item, "link").text = dash_url
    guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
    guid.text = f"{dash_url}#{today}"
    ET.SubElement(item, "description").text = (
        "Your morning dashboard — fantasy, Blue Jays, portfolio, markets, and Pokémon GO, "
        "refreshed each morning. — Source: " + source)
    ET.SubElement(item, "pubDate").text = C.to_rfc822(C.now_local())
    ET.SubElement(item, "source", {"url": base + "/"}).text = source
    ET.SubElement(item, f"{{{C.DC_NS}}}creator").text = source
    ET.SubElement(item, f"{{{C.MEDIA_NS}}}thumbnail", {"url": cover_url})
    ET.SubElement(item, "enclosure", {"url": cover_url, "type": "image/png", "length": "0"})
    return item


def inject_item(xml_bytes: bytes, item: ET.Element, dash_url: str) -> bytes:
    for prefix, ns in (("media", C.MEDIA_NS), ("atom", C.ATOM_NS), ("dc", C.DC_NS)):
        ET.register_namespace(prefix, ns)
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("feed has no <channel>")

    # Drop any prior Morning Line item (match by link, ignoring the guid's #date).
    for existing in list(channel.findall("item")):
        if (existing.findtext("link") or "").strip() == dash_url:
            channel.remove(existing)

    # Insert as the first <item> (after channel metadata).
    items = channel.findall("item")
    idx = list(channel).index(items[0]) if items else len(list(channel))
    channel.insert(idx, item)

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Publish + pin the Morning Line.")
    ap.add_argument("--dry-run", action="store_true", help="Build the item; upload nothing.")
    args = ap.parse_args()

    for prefix, ns in (("media", C.MEDIA_NS), ("atom", C.ATOM_NS), ("dc", C.DC_NS)):
        ET.register_namespace(prefix, ns)

    fi = _cfg()
    today = C.today_str()
    base = fi["public_base"].rstrip("/")
    dash_url = f"{base}/{fi['dashboard_key']}"

    if not DASH_HTML.exists():
        print(f"ERROR: {DASH_HTML} missing — run build_dashboard.py first.", file=sys.stderr)
        sys.exit(1)

    item = build_item(fi, today)
    item_xml = ET.tostring(item, encoding="unicode")
    if args.dry_run:
        print("Dry run — Morning Line item that would be pinned:\n")
        print(item_xml)
        return

    from botocore.exceptions import ClientError
    client = C.s3_client()

    # 1. Upload page + cover (stable 'latest' + dated archive).
    html_bytes = DASH_HTML.read_bytes()
    _put(client, fi["dashboard_key"], html_bytes, "text/html; charset=utf-8")
    _put(client, f"feed/apps/{today}/dashboard.html", html_bytes, "text/html; charset=utf-8")
    if COVER_PNG.exists():
        png = COVER_PNG.read_bytes()
        _put(client, fi["cover_key"], png, "image/png")
        _put(client, f"feed/apps/{today}/cover.png", png, "image/png")
    else:
        print("  WARN: no cover.png; item will pin without a thumbnail.", file=sys.stderr)

    # 2. Inject into the live feed (conditional on ETag, like backfill_thumbnails).
    try:
        resp = client.get_object(Bucket=C.S3_BUCKET, Key=C.FEED_KEY)
        feed_xml, etag = resp["Body"].read(), resp.get("ETag", "")
    except ClientError as exc:
        print(f"ERROR: could not read live feed ({exc}).", file=sys.stderr)
        sys.exit(1)

    new_xml = inject_item(feed_xml, item, dash_url)
    ok, msg = C.validate_rss(new_xml)
    if not ok:
        print(f"ERROR: feed with Morning Line failed validation ({msg}); not writing.",
              file=sys.stderr)
        sys.exit(1)

    extra = {"ContentType": "application/rss+xml; charset=utf-8",
             "CacheControl": f"max-age={C.FEED_CACHE_SECONDS}"}
    if C.FEED_ACL:
        extra["ACL"] = C.FEED_ACL
    if etag:
        extra["IfMatch"] = etag
    try:
        client.put_object(Bucket=C.S3_BUCKET, Key=C.FEED_KEY, Body=new_xml, **extra)
        print(f"Pinned Morning Line to the top of the feed ({msg}).")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("PreconditionFailed", "ConditionalRequestConflict") or status in (409, 412):
            print("Feed changed under us (routine build); skipping this write — "
                  "the pinned entry in interests.yaml keeps it in the feed.")
        else:
            raise


if __name__ == "__main__":
    main()

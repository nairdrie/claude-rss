#!/usr/bin/env python3
"""Backfill og:image thumbnails into the *published* feed, out-of-band.

Why this exists separately from ``fetch_thumbnails.py``: that script runs inside
the Claude routine, whose sandbox has no outbound access to article domains (the
egress proxy denies them), so it almost never finds an image. This script does
the same og:image scrape but is meant to run **somewhere with real internet**
(GitHub Actions, your laptop, any cron box) — decoupled from the routine.

Flow (default, S3 in place):
    1. Download the live feed:  s3://<bucket>/<FEED_KEY>
    2. For every <item> that has no <media:thumbnail>, fetch the article and
       scrape its og:image (reusing fetch_thumbnails' hardened scraper).
    3. Inject <media:thumbnail> + <enclosure> into just those items — every
       other byte of the feed is left exactly as the routine wrote it.
    4. Validate the result as RSS 2.0 and, only if something changed and it's
       valid, upload it back to the same key.

It never rewrites pubDates, guids, ordering, or seen.json — it only adds images
to items missing them. A failed or empty scrape leaves the live feed untouched.
Thumbnails it adds survive the routine's incremental rebuilds (which preserve
existing <media:thumbnail>s); a daily rebuild starts a fresh window, so schedule
this to run periodically (e.g. hourly) and it self-heals.

Usage:
    python scripts/backfill_thumbnails.py            # S3 in place (needs AWS creds)
    python scripts/backfill_thumbnails.py --dry-run  # scrape + report, no upload
    python scripts/backfill_thumbnails.py --file state/feed.xml   # local file in place

Credentials/region come from the standard boto3 chain and the same env vars as
the rest of the pipeline (see docs/s3-access.md).
"""
from __future__ import annotations

import argparse
import mimetypes
import sys
from xml.etree import ElementTree as ET

try:
    from scripts import _common as C
    from scripts.fetch_thumbnails import fetch_thumbnail
except ImportError:  # invoked as: python scripts/backfill_thumbnails.py
    import _common as C
    from fetch_thumbnails import fetch_thumbnail

MEDIA_THUMB = f"{{{C.MEDIA_NS}}}thumbnail"


def _enclosure_mime(image_url: str) -> str:
    mime, _ = mimetypes.guess_type(image_url)
    if not mime or not mime.startswith("image/"):
        mime = "image/jpeg"
    return mime


def enrich_xml(xml_bytes: bytes) -> tuple[bytes | None, dict]:
    """Scrape + inject thumbnails into a feed document.

    Returns (new_xml_or_None, stats). new_xml is None when nothing changed.
    Registers the media/atom prefixes so re-serialization keeps the same
    ``media:thumbnail`` / ``atom:link`` form the builder emits.
    """
    ET.register_namespace("media", C.MEDIA_NS)
    ET.register_namespace("atom", C.ATOM_NS)
    root = ET.fromstring(xml_bytes)

    items = root.findall("./channel/item")
    stats = {"items": len(items), "already": 0, "added": 0, "no_tag": 0, "errors": {}}
    for item in items:
        if item.find(MEDIA_THUMB) is not None:
            stats["already"] += 1
            continue
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        image, error = fetch_thumbnail(link)
        if not image:
            if error:
                stats["errors"][error] = stats["errors"].get(error, 0) + 1
            else:
                stats["no_tag"] += 1
            continue
        ET.SubElement(item, MEDIA_THUMB, {"url": image})
        ET.SubElement(
            item, "enclosure", {"url": image, "type": _enclosure_mime(image), "length": "0"}
        )
        stats["added"] += 1

    if not stats["added"]:
        return None, stats
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), stats


def _print_summary(stats: dict, *, uploaded: bool, dry_run: bool) -> None:
    errs = stats["errors"]
    line = (
        f"Backfill: {stats['added']} added, {stats['already']} already had one, "
        f"{stats['no_tag']} fetched without an og:image, {sum(errs.values())} fetch "
        f"failures across {stats['items']} item(s)."
    )
    if errs:
        line += "\n  fetch failures: " + ", ".join(f"{k}×{v}" for k, v in sorted(errs.items()))
        blocked = sum(v for k, v in errs.items() if k in ("egress-blocked", "http-403", "http-407"))
        if blocked and blocked >= stats["items"] / 2:
            line += (
                "\n  NOTE: most fetches were denied — whatever is running this script "
                "has no outbound access to the article domains. Run it somewhere with "
                "open internet (GitHub Actions, a laptop, a cron box), not inside the "
                "Claude routine's sandbox."
            )
    if dry_run and stats["added"]:
        line += "\n  (dry run — not uploading)"
    elif uploaded:
        line += "\n  uploaded enriched feed."
    elif not stats["added"]:
        line += "\n  nothing to add; leaving the feed as-is."
    print(line)


def _load_local(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _save_local(path: str, xml: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(xml)


def _load_s3() -> tuple[bytes, str]:
    client = C.s3_client()
    resp = client.get_object(Bucket=C.S3_BUCKET, Key=C.FEED_KEY)
    return resp["Body"].read(), resp.get("ETag", "")


def _save_s3(xml: bytes, if_match: str = "") -> bool:
    """Upload the enriched feed. If `if_match` is set, the write is conditional
    on the object still having that ETag -- so a routine build that rewrote the
    feed between our read and write wins instead of being clobbered. Returns
    True on success, False if the feed changed underneath us."""
    from botocore.exceptions import ClientError

    client = C.s3_client()
    extra = {
        "ContentType": "application/rss+xml; charset=utf-8",
        "CacheControl": f"max-age={C.FEED_CACHE_SECONDS}",
    }
    if C.FEED_ACL:
        extra["ACL"] = C.FEED_ACL
    if if_match:
        extra["IfMatch"] = if_match
    try:
        client.put_object(Bucket=C.S3_BUCKET, Key=C.FEED_KEY, Body=xml, **extra)
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("PreconditionFailed", "ConditionalRequestConflict") or status in (409, 412):
            return False  # the feed moved under us; a later run re-enriches it
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill og:image thumbnails into the feed.")
    parser.add_argument("--file", help="Operate on a local feed file instead of S3.")
    parser.add_argument("--dry-run", action="store_true", help="Scrape and report; do not write.")
    args = parser.parse_args()

    etag = ""
    try:
        if args.file:
            xml_bytes = _load_local(args.file)
        else:
            xml_bytes, etag = _load_s3()
    except Exception as exc:  # noqa: BLE001 - surface any load failure cleanly
        print(f"ERROR: could not read feed ({exc}).", file=sys.stderr)
        sys.exit(1)

    try:
        new_xml, stats = enrich_xml(xml_bytes)
    except ET.ParseError as exc:
        print(f"ERROR: feed is not well-formed XML ({exc}); refusing to touch it.",
              file=sys.stderr)
        sys.exit(1)

    uploaded = False
    if new_xml and not args.dry_run:
        # Never replace a good live feed with a broken one.
        ok, message = C.validate_rss(new_xml)
        if not ok:
            print(f"ERROR: enriched feed failed validation ({message}); not writing.",
                  file=sys.stderr)
            sys.exit(1)
        if args.file:
            _save_local(args.file, new_xml)
            uploaded = True
        elif _save_s3(new_xml, if_match=etag):
            uploaded = True
        else:
            print("Feed changed in S3 while scraping; skipping this write "
                  "(a later run will re-enrich).")

    _print_summary(stats, uploaded=uploaded, dry_run=args.dry_run)


if __name__ == "__main__":
    main()

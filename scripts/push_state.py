#!/usr/bin/env python3
"""Upload state/feed.xml and state/seen.json back to S3.

Uploads:
    state/feed.xml  -> s3://<bucket>/<FEED_KEY>   (Content-Type application/rss+xml)
    state/seen.json -> s3://<bucket>/<STATE_KEY>  (Content-Type application/json)

Refuses to upload a missing, invalid, or empty feed, so a failed build can
never replace the good live feed with a broken or empty one.
"""
from __future__ import annotations

import json
import sys

try:
    from scripts import _common as C
except ImportError:  # invoked as: python scripts/push_state.py
    import _common as C


def _guard() -> None:
    """Validate local state before touching S3 (defense in depth)."""
    if not C.FEED_PATH.exists():
        print(f"ERROR: {C.FEED_PATH} does not exist; nothing to push.", file=sys.stderr)
        sys.exit(1)

    with open(C.FEED_PATH, "rb") as fh:
        xml = fh.read()

    ok, message = C.validate_rss(xml)
    if not ok:
        print(f"ERROR: {C.FEED_PATH} is not a valid feed ({message}); refusing to push.",
              file=sys.stderr)
        sys.exit(1)

    import feedparser
    if not feedparser.parse(xml).entries:
        print("ERROR: feed has zero items; refusing to overwrite the live feed with an "
              "empty one.", file=sys.stderr)
        sys.exit(1)

    if C.SEEN_PATH.exists():
        try:
            with open(C.SEEN_PATH, "r", encoding="utf-8") as fh:
                json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"ERROR: {C.SEEN_PATH} is not valid JSON ({exc}); refusing to push.",
                  file=sys.stderr)
            sys.exit(1)


def main() -> None:
    from botocore.exceptions import BotoCoreError, ClientError

    _guard()
    client = C.s3_client()

    feed_args = {
        "ContentType": "application/rss+xml; charset=utf-8",
        "CacheControl": f"max-age={C.FEED_CACHE_SECONDS}",
    }
    state_args = {
        "ContentType": "application/json; charset=utf-8",
        "CacheControl": "no-cache",
    }
    if C.FEED_ACL:  # only when the bucket uses object ACLs instead of a policy
        feed_args["ACL"] = C.FEED_ACL
        state_args["ACL"] = C.FEED_ACL

    try:
        client.upload_file(str(C.FEED_PATH), C.S3_BUCKET, C.FEED_KEY, ExtraArgs=feed_args)
        print(f"Pushed {C.FEED_PATH} -> s3://{C.S3_BUCKET}/{C.FEED_KEY}")
        if C.SEEN_PATH.exists():
            client.upload_file(str(C.SEEN_PATH), C.S3_BUCKET, C.STATE_KEY, ExtraArgs=state_args)
            print(f"Pushed {C.SEEN_PATH} -> s3://{C.S3_BUCKET}/{C.STATE_KEY}")
    except (BotoCoreError, ClientError) as exc:
        print(f"ERROR pushing to S3: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

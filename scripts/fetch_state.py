#!/usr/bin/env python3
"""Pull the live feed + state from S3 into state/ (ephemeral scratch).

Downloads:
    s3://<bucket>/<FEED_KEY>  -> state/feed.xml
    s3://<bucket>/<STATE_KEY> -> state/seen.json

On the very first run (either object missing in S3) it creates valid empty
local versions instead of failing, so the daily build can seed the feed.
"""
from __future__ import annotations

import sys

try:
    from scripts import _common as C
except ImportError:  # invoked as: python scripts/fetch_state.py
    import _common as C


def _is_missing(exc) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return str(code) in ("404", "NoSuchKey", "NoSuchBucket", "NotFound")


def main() -> None:
    from botocore.exceptions import BotoCoreError, ClientError

    C.STATE_DIR.mkdir(parents=True, exist_ok=True)
    client = C.s3_client()

    # --- feed.xml ---
    try:
        client.download_file(C.S3_BUCKET, C.FEED_KEY, str(C.FEED_PATH))
        print(f"Fetched s3://{C.S3_BUCKET}/{C.FEED_KEY} -> {C.FEED_PATH}")
    except ClientError as exc:
        if _is_missing(exc):
            C.write_empty_feed(C.FEED_PATH)
            print(f"No feed in S3 yet; wrote empty local feed to {C.FEED_PATH}")
        else:
            print(f"ERROR fetching feed: {exc}", file=sys.stderr)
            sys.exit(1)
    except BotoCoreError as exc:
        print(f"ERROR fetching feed: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- seen.json ---
    try:
        client.download_file(C.S3_BUCKET, C.STATE_KEY, str(C.SEEN_PATH))
        print(f"Fetched s3://{C.S3_BUCKET}/{C.STATE_KEY} -> {C.SEEN_PATH}")
    except ClientError as exc:
        if _is_missing(exc):
            C.write_seen(C.default_seen())
            print(f"No state in S3 yet; wrote fresh {C.SEEN_PATH}")
        else:
            print(f"ERROR fetching state: {exc}", file=sys.stderr)
            sys.exit(1)
    except BotoCoreError as exc:
        print(f"ERROR fetching state: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

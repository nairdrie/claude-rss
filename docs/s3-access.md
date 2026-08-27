# S3 access

The feed's source of truth lives in S3. This repo only holds instructions,
scripts, and config — the actual feed state (the live XML + the dedup store) is
read from and written back to these two objects on every run.

## Coordinates

| Thing        | Value                                                                  |
| ------------ | ----------------------------------------------------------------------- |
| Bucket       | `nairdrie-rss-feed`                                                    |
| Feed key     | `feed/merged.xml`                                                      |
| State key    | `feed/seen.json`                                                       |
| Region       | `us-east-1`                                                            |
| Public URL   | `https://nairdrie-rss-feed.s3.us-east-1.amazonaws.com/feed/merged.xml` |

`nairdrie-rss-feed` is a small dedicated bucket holding nothing but the feed.
It is **not** the `nairdrie.com` bucket that backs the AWS Amplify-hosted
website — that was the original design, but Amplify Hosting's CloudFront
distribution only serves objects that went through its own build/deploy
pipeline. An object written directly via the S3 API (as this feed is, on
every run) is invisible to it and 404s, regardless of bucket policy. A
separate bucket with its own public-read bucket policy sidesteps that
entirely — the feed is served straight from S3's own HTTPS endpoint, no
CloudFront/Amplify involved. The scripts **only ever touch the two keys
above** (both under the `feed/` prefix) — they never list, read, or write
anything else in the bucket.

> **Note:** because the whole bucket is public, `feed/seen.json` is publicly
> readable too. That's harmless — it contains only article URLs and first-seen
> dates, no secrets. If you'd rather keep it private, point `RSS_STATE_KEY` (or
> the whole state file) at a separate private bucket and give the runtime
> credentials access to both.

## Credentials — from the runtime, never the repo

**AWS credentials are not stored in this repo and must never be committed.** The
scripts use the standard boto3 credential chain, so provide credentials to the
runtime one of these ways:

- **Environment variables:** `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
  (plus `AWS_SESSION_TOKEN` if they're temporary credentials).
- **An IAM role** attached to the compute the routine runs on (nothing to set).
- **A named profile:** set `AWS_PROFILE` and mount `~/.aws/` into the runtime.

Set the region too, either with `AWS_REGION=us-east-1` (or `AWS_DEFAULT_REGION`)
or leave it — the scripts default to `us-east-1`.

The `state/` directory, `.env`, `*.pem`, and `.aws/` are all gitignored.

### Minimum IAM permissions

The credentials need get/put on the two feed objects, plus `ListBucket`
scoped to the `feed/` prefix (without it, a request for an object that
doesn't yet exist comes back as an ambiguous 403 instead of a clean 404,
which `fetch_state.py`'s first-run bootstrap relies on to tell "missing"
apart from "actually forbidden"):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadWriteFeedObjects",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::nairdrie-rss-feed/feed/*"
    },
    {
      "Sid": "ListFeedPrefix",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::nairdrie-rss-feed",
      "Condition": { "StringLike": { "s3:prefix": "feed/*" } }
    }
  ]
}
```

## Public-read / serving setup

`nairdrie-rss-feed` has **Block all public access** turned off and a
bucket policy granting anonymous `s3:GetObject` scoped to `feed/*` only —
see the bucket's own policy in the console for the exact statement. Objects
under `feed/` inherit public read from that policy, so the scripts send
**no per-object ACL** by default. This is the right choice for buckets with
ACLs disabled ("bucket owner enforced" — the modern S3 default), where sending
`ACL=public-read` would actually error.

- If your bucket instead grants public read via **object ACLs**, set
  `RSS_FEED_ACL=public-read` and the scripts will attach it.
- Confirm `feed/merged.xml` is reachable at
  `https://nairdrie-rss-feed.s3.us-east-1.amazonaws.com/feed/merged.xml`
  after the first real run.

The feed object is written with `Content-Type: application/rss+xml` and a short
`Cache-Control: max-age=300` (tune via `RSS_FEED_CACHE_SECONDS`).

## Environment variables

Required for real runs:

| Variable                                       | Purpose                          |
| ---------------------------------------------- | -------------------------------- |
| `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY`  | Credentials (or use a role/profile) |
| `AWS_SESSION_TOKEN`                            | Only for temporary credentials   |
| `AWS_REGION` (or `AWS_DEFAULT_REGION`)         | `us-east-1` (optional; that's the default) |

Optional overrides (all have working defaults baked into `scripts/_common.py`):

| Variable                 | Default             | Meaning                                    |
| ------------------------ | ------------------- | ------------------------------------------ |
| `RSS_S3_BUCKET`          | `nairdrie-rss-feed` | Bucket name                                |
| `RSS_FEED_KEY`           | `feed/merged.xml`   | Feed object key                            |
| `RSS_STATE_KEY`          | `feed/seen.json`    | State/dedup object key                     |
| `RSS_S3_REGION`          | `us-east-1`         | Overrides `AWS_REGION` for S3 only         |
| `RSS_FEED_ACL`           | *(unset)*           | Set to `public-read` only for ACL buckets  |
| `RSS_FEED_CACHE_SECONDS` | `300`               | `Cache-Control: max-age` on the feed       |
| `RSS_TIMEZONE`           | `America/Toronto`   | Timezone for mode logic and dates          |

## Network egress (thumbnails)

Most of the pipeline only needs the two S3 objects above and the web-search
tools the curator agent uses — none of which require broad outbound network
access. **Thumbnails are the exception.** `scripts/fetch_thumbnails.py` fetches
each curated item's *article page directly* (plain `urllib`, through whatever
proxy the runtime sets) to read its `og:image`. That means it needs outbound
HTTPS to arbitrary news/sports/finance/etc. domains.

If the feed environment runs under a restrictive network policy, the egress
proxy refuses the connection to those domains (an HTTPS `CONNECT` denied with
`403`), every scrape fails, and the feed renders with no images. This is *not* a
code bug — `fetch_thumbnails.py` degrades quietly and prints a summary like:

```
Thumbnails: 0 scraped, … 48 fetch failures (of 48 attempted) …
  fetch failures: egress-blocked×48
  NOTE: most fetches were denied — the environment's network egress policy is
  blocking these domains …
```

To enable thumbnails, broaden the **feed environment's network access** so
outbound HTTPS to article domains is allowed:

- On Claude Code on the web, pick a more permissive network policy when
  creating/editing the environment the routine runs in (see
  <https://code.claude.com/docs/en/claude-code-on-the-web> → network access).
  A full-egress policy works; an allowlist works too, but the set of domains a
  curated feed links to is open-ended, so a broad policy is the practical
  choice.
- Self-hosted/other runtimes: allow outbound 443 (or point `HTTPS_PROXY` at a
  proxy that permits these hosts).

The curator agent's web-search results are unaffected either way — only the
direct `og:image` scrape depends on this. When egress is blocked, everything
else (research, dedup, build, push) still works; you just get a text-only feed.

## Command reference

The scripts do all of this with boto3, but here are the equivalent CLI commands.

**Read (what `fetch_state.py` does):**

```bash
aws s3 cp s3://nairdrie-rss-feed/feed/merged.xml state/feed.xml
aws s3 cp s3://nairdrie-rss-feed/feed/seen.json  state/seen.json
```

```python
import boto3
s3 = boto3.client("s3", region_name="us-east-1")
s3.download_file("nairdrie-rss-feed", "feed/merged.xml", "state/feed.xml")
s3.download_file("nairdrie-rss-feed", "feed/seen.json",  "state/seen.json")
```

**Write (what `push_state.py` does):**

```bash
aws s3 cp state/feed.xml  s3://nairdrie-rss-feed/feed/merged.xml \
  --content-type "application/rss+xml; charset=utf-8" --cache-control "max-age=300"
aws s3 cp state/seen.json s3://nairdrie-rss-feed/feed/seen.json \
  --content-type "application/json; charset=utf-8" --cache-control "no-cache"
```

```python
import boto3
s3 = boto3.client("s3", region_name="us-east-1")
s3.upload_file(
    "state/feed.xml", "nairdrie-rss-feed", "feed/merged.xml",
    ExtraArgs={"ContentType": "application/rss+xml; charset=utf-8",
               "CacheControl": "max-age=300"},
)
s3.upload_file(
    "state/seen.json", "nairdrie-rss-feed", "feed/seen.json",
    ExtraArgs={"ContentType": "application/json; charset=utf-8",
               "CacheControl": "no-cache"},
)
```

> Add `"ACL": "public-read"` to `ExtraArgs` (or `--acl public-read` to the CLI)
> **only** if your bucket uses object ACLs. On a policy-public bucket with ACLs
> disabled, that flag errors — which is why the scripts omit it by default.

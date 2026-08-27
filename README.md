# claude-rss — an AI-curated personal RSS feed

This repo powers a personal, AI-curated RSS feed. A scheduled **Claude Code**
routine wakes an instance of Claude roughly hourly. On wake, Claude's context is
this repo and `CLAUDE.md` auto-loads as its runbook. There is **no external
script that "calls Claude" and no Anthropic API key** — Claude itself is the
agent: it reads the runbook, does its own web research, decides what belongs in
the feed, rebuilds the feed, pushes it to S3, and exits.

The live feed: **https://nairdrie.com/feed/merged.xml**

## Lifecycle of one wake

```
fetch state  →  research  →  decide  →  build  →  push  →  exit
(from S3)       (web search) (dedup)   (locally) (to S3)
```

1. Read `CLAUDE.md` (auto-loaded), which routes by time of day.
2. Read `config/interests.yaml`.
3. `python scripts/fetch_state.py` — pull the current `feed.xml` + `seen.json`
   from S3 into `state/`.
4. Web research (Claude's own search tools), scoped by mode + interests.
5. Decide what to add, deduping against `seen.json`.
6. Write decisions to `state/curated.json`.
7. `python scripts/build_feed.py <daily|incremental>` — produce a valid RSS 2.0
   `state/feed.xml` and update `state/seen.json`.
8. `python scripts/push_state.py` — upload both back to S3.
9. Exit. On a quiet incremental run with nothing new, skip the push.

### Daily vs. incremental

- **DAILY** (the ~6am wake): rebuild the whole window — 15–25 candidates
  researched across all topics, best ~`daily_target` selected.
- **INCREMENTAL** (every other wake): conservatively trickle in at most
  `max_incremental_adds` high-signal items; **adding nothing is a valid
  outcome.** Breaking news is always admitted and gets a `[BREAKING]` prefix.

Mode is decided in `CLAUDE.md` Step 0 from `seen.json.last_daily_write` and the
local hour in `America/Toronto`.

## Persistence model — three tiers

This is the key mental model; every design choice follows from it.

| Tier          | Where            | Lifetime                              | Holds                              |
| ------------- | ---------------- | ------------------------------------- | ---------------------------------- |
| Instructions  | **git** (this repo) | Permanent, versioned               | `CLAUDE.md`, `scripts/`, `config/` |
| Feed state    | **S3**           | The source of truth for "what's in the feed" | `feed/merged.xml`, `feed/seen.json` |
| Run scratch   | **`state/`**     | Ephemeral — synced from S3 each run, gitignored | local `feed.xml`, `seen.json`, `curated.json` |

So `state/` is disposable working space. Nothing in it is authoritative; S3 is.

## Repository layout

```
CLAUDE.md               # the runbook Claude auto-loads on wake
config/interests.yaml   # topics, weights, excludes, feed sizing (edit this)
scripts/
  fetch_state.py        # S3 -> state/  (creates empty valid files on first run)
  build_feed.py         # curated.json + state -> feed.xml + seen.json
  push_state.py         # state/ -> S3  (refuses to push a broken/empty feed)
  fetch_thumbnails.py   # scrape og:image into curated.json (in-routine step)
  backfill_thumbnails.py# scrape og:image into the LIVE feed, out-of-band (see Thumbnails)
  _common.py            # shared config, paths, S3 client, feed helpers
.github/workflows/
  backfill-thumbnails.yml # scheduled thumbnail scrape on GitHub's runners
docs/
  s3-access.md          # bucket, keys, credentials, exact commands
  feed-schema.md        # RSS item format, guid/dedup, [BREAKING] convention
requirements.txt
```

## Editing your interests

`config/interests.yaml` is the dial. Hand-tune it any time:

- **`topics`** — add/remove topics; each has a `detail` (what you actually care
  about) and a `weight` of `high` / `medium` / `low`. Weight drives how much
  research effort each topic gets; `high`-weight topics are the only ones scanned
  on incremental wakes.
- **`exclude`** — kinds of content to drop (SEO farms, contentless PRs, hard
  paywalls).
- **`feed`** — sizing: `window_size` (max live items), `daily_target` (morning
  build size), `max_incremental_adds` (cap per non-daily run).
- **`founder_context`** — extra standing context folded into `ai-dev-tools`.

Changes take effect on the next wake — no redeploy.

## How the feed is served

The feed object is written to `s3://nairdrie.com/feed/merged.xml` and served
publicly by the same bucket that hosts nairdrie.com, so readers subscribe at:

```
https://nairdrie.com/feed/merged.xml
```

Content-Type is `application/rss+xml` with a short cache. See
[`docs/s3-access.md`](docs/s3-access.md) for the public-read setup to confirm.

## Thumbnails (`<media:thumbnail>`)

Feed items can carry a thumbnail scraped from the article's `og:image`. Getting
one requires **fetching the article page**, and here's the catch: the Claude
curator routine runs in a sandbox whose egress policy blocks arbitrary article
domains (news, sports, finance, …). So the in-routine step,
`scripts/fetch_thumbnails.py`, only ever succeeds for the handful of allowlisted
domains — everything else comes back `egress-blocked`. This is a property of the
run environment, **not** a bug in the scraper.

There are two ways to actually get thumbnails; pick one:

1. **Backfill from a runner that has internet (recommended).**
   `scripts/backfill_thumbnails.py` pulls the live feed from S3, adds a
   `<media:thumbnail>` to every item missing one, and writes it back — touching
   nothing else (pubDates, guids, ordering, `seen.json` are all left as-is). It
   refuses to upload an invalid feed and uses a conditional write so it never
   clobbers a routine build that landed underneath it.

   ```bash
   # anywhere with open internet + AWS creds (your laptop, a cron box, CI):
   python -m pip install -r requirements.txt
   python scripts/backfill_thumbnails.py            # enrich the live S3 feed
   python scripts/backfill_thumbnails.py --dry-run  # scrape + report, write nothing
   python scripts/backfill_thumbnails.py --file state/feed.xml  # a local feed file
   ```

   To automate it, [`.github/workflows/backfill-thumbnails.yml`](.github/workflows/backfill-thumbnails.yml)
   runs this hourly on GitHub's runners (which have full internet). One-time
   setup: add `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (and optionally
   `AWS_REGION`) as repo **Actions secrets**, scoped to the two feed objects per
   `docs/s3-access.md`. Thumbnails it adds persist through the routine's
   incremental builds; a daily rebuild starts a fresh window, so the hourly run
   refills it.

   On top of that schedule, the curator routine **dispatches this workflow
   itself** (`workflow_dispatch`) at the end of any wake that pushed a changed
   feed — see [`CLAUDE.md`](CLAUDE.md) → Step 7 — so freshly curated items get
   thumbnails within a minute or two instead of waiting up to an hour for the
   next scheduled run. The hourly cron stays as a backstop. This is what
   removes the need to click **Run workflow** by hand.

2. **Open the routine's egress instead.** If you'd rather keep it in the
   routine, broaden the feed environment's network policy so
   `fetch_thumbnails.py` can reach article domains — no separate job needed. See
   [`docs/s3-access.md`](docs/s3-access.md) → Network egress.

## What the runtime needs (AWS credentials + env vars)

Provide AWS credentials to **each session woken in this repo** — via env vars, an
IAM role, or a named profile. Nothing AWS-related is stored in git.

**Required (real runs):**

- `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (+ `AWS_SESSION_TOKEN` if
  temporary) — or an attached IAM role, or `AWS_PROFILE` with `~/.aws/` mounted.
- `AWS_REGION=us-east-1` (optional — that's the default).

The credentials need only `s3:GetObject` + `s3:PutObject` on
`arn:aws:s3:::nairdrie.com/feed/*` (policy in `docs/s3-access.md`).

**Optional overrides** (all default sensibly, see `docs/s3-access.md`):
`RSS_S3_BUCKET`, `RSS_FEED_KEY`, `RSS_STATE_KEY`, `RSS_S3_REGION`,
`RSS_FEED_ACL`, `RSS_FEED_CACHE_SECONDS`, `RSS_TIMEZONE`.

## Local setup / manual run

```bash
python -m pip install -r requirements.txt
# with AWS creds in the environment:
python scripts/fetch_state.py
# ...write state/curated.json (see docs/feed-schema.md)...
python scripts/build_feed.py incremental
python scripts/push_state.py
```

`build_feed.py` works fully offline (no AWS needed) — handy for testing against a
hand-made `state/curated.json`.

## Wiring the scheduled routine

Set up a Claude Code scheduled routine (a Routine / cron trigger) that wakes in
**this repo** roughly **hourly**. The important constraint:

> **Ensure at least one wake lands in the 06:00–07:00 America/Toronto window** so
> the daily full rebuild fires.

`CLAUDE.md` has a safety net — if the 6am wake is missed or DST-shifted, the
first wake after 6am that sees `last_daily_write` isn't today will run the daily
build instead — but an hourly cadence with a wake at the top of the 6am hour is
the intended setup. An hourly schedule (`0 * * * *`) satisfies both the hourly
cadence and the 6am requirement.

Each woken session must have the AWS credentials above attached to its
environment.

## Safety guarantees

- **Never push a broken or empty feed over a good one.** `build_feed.py`
  validates via `feedparser` before writing and exits non-zero (leaving files
  untouched) on failure; `push_state.py` re-validates and refuses to upload a
  missing, invalid, or zero-item feed.
- **Curate to interests, not volume.** An empty incremental run is correct.
- **No secrets in git.** `state/`, `.env`, `*.pem`, and AWS credential files are
  gitignored.

# Feed curator — runbook

You have been woken by a scheduled routine. Your context is this repo. Your job
is to update Nick's personal RSS feed according to the time of day, then exit.
Do not ask questions — there is no human in this session. Work through the steps
and stop.

## Step 0 — Determine mode

- Get the current time in **America/Toronto** (timezone-aware; this makes DST
  automatic).
- Fetch state first (Step 1) so you can read `seen.json`'s `last_daily_write`
  date.
- **DAILY mode** if BOTH: (a) `last_daily_write` is not today's date in
  America/Toronto, AND (b) the local hour is >= 6. (Primary intent: the
  06:00–06:59 wake writes the day's feed. The "not yet done today" guard is a
  safety net so a missed or DST-shifted 6am wake still triggers the daily write
  on the next wake after 6am, instead of silently skipping a day.)
- **INCREMENTAL mode** otherwise (this is the common case — most wakes).

## Step 1 — Load config and state

- Read `config/interests.yaml` (topics, weights, excludes, feed sizing).
- Run `python scripts/fetch_state.py`. This populates `state/feed.xml` (current
  live window) and `state/seen.json` (recent item URLs + `last_daily_write`). If
  either doesn't exist in S3 yet (first ever run), the script creates empty valid
  versions locally — proceed normally.

## Step 2 — Research (use your web search tools)

- **DAILY mode:** search across every topic in `interests.yaml`, allocating
  effort by `weight` (more queries/results for `high`, fewer for `low`). Aim
  for up to `daily_target` (see `interests.yaml` — currently 50) genuinely new
  candidate items **published within the last 24 hours** (today or yesterday
  in America/Toronto) — search each topic thoroughly enough to plausibly
  reach that count on a busy news day, especially the `high`-weight ones.
  A quieter day legitimately producing fewer is correct, not a failure —
  never reach for an older item just because it's the most recent available
  (e.g. a weekly recurring roundup) to pad the count. `build_feed.py` also
  hard-drops anything older than `max_item_age_hours` (`config/interests.yaml`)
  as a backstop, so treat the 24-hour window as the real target, not a rough
  guideline.
- **INCREMENTAL mode:** search only `high`-weight topics plus a quick scan for
  anything breaking across the others. Be conservative — you are trickling in
  updates through the day, not refilling the feed.

## Step 3 — Decide what goes in

- **Dedup:** drop any candidate whose URL is already in `seen.json`.
- **Exclude:** drop anything matching the `exclude` rules in `interests.yaml`
  (SEO/listicle slop, contentless press releases, hard paywalls).
- **DAILY mode:** select the best ~`daily_target` items → this becomes a freshly
  rebuilt window.
- **INCREMENTAL mode:** select AT MOST `max_incremental_adds` items, and only
  ones that clear a real relevance bar. **If nothing qualifies, add nothing** —
  an empty result is correct, not a failure. Never pad the feed to look busy.
- **Breaking news** (genuinely time-sensitive, high-signal): always admit it in
  incremental mode, and set `"breaking": true` so it gets a `[BREAKING]` title
  prefix. Use this sparingly and honestly — a routine software release is not
  breaking.

## Step 4 — Emit decisions

Write `state/curated.json` as a JSON array; each item:
`{ "title", "url", "source", "topic", "published_at" (ISO 8601 or RFC 822), "reason" (one line), "breaking" (bool) }`

- **`topic`** — the `interests.yaml` topic the item belongs to (`crypto`,
  `dev-tools`, `sports`, …). Set it whenever you can: the daily build interleaves
  the window by topic so it reads as a mix instead of a wall of crypto followed
  by a wall of tech. Omitting it falls back to the source/domain, which mixes
  less cleanly.
- **`published_at`** — record a real **time of day** when the research gives you
  one (a full `2026-08-26T14:30:00-04:00` timestamp), not just the date. A bare
  date resolves to local midnight; the build then has to synthesize a time for
  it, so the truer the input, the truer the per-topic ordering. (The build
  always lays a clean staggered timeline over the window regardless — you'll
  never get a wall of identical midnights — but real times still order items
  within a topic. See `docs/feed-schema.md`.)

You never need to curate the daily chess puzzle yourself — `build_feed.py`
always injects it at the top of the feed from `interests.yaml`'s `pinned`
list, in both daily and incremental builds, so it self-heals on the next
wake if it's ever missing or stale.

## Step 4.5 — Thumbnails

- Run `python scripts/fetch_thumbnails.py`. It scrapes each curated item's
  `og:image` (best-effort, stdlib-only) and adds an `"image"` field to
  `curated.json` where found.
- This is never a reason to stop the run — a missing thumbnail just means
  that item renders without one. Don't chase failures here.
- Scraping needs outbound network access to each article's domain. This
  routine's sandbox usually blocks that, so the step prints an `egress-blocked`
  summary and moves on — that's expected, not an error to fix from this session.
  Thumbnails are normally filled in **out-of-band** by
  `scripts/backfill_thumbnails.py`, which runs on a runner that has internet
  (e.g. the `backfill-thumbnails` GitHub Action) and enriches the live feed
  after you push it. See the README → Thumbnails and `docs/s3-access.md` →
  Network egress. If a research agent already put an `image` on an item, this
  step leaves it as-is, so you can supply one directly when you happen to have it.

## Step 5 — Build

- Run `python scripts/build_feed.py daily` or
  `python scripts/build_feed.py incremental`.
- The script merges/rebuilds `state/feed.xml`, updates `state/seen.json` (adds
  new URLs, prunes entries older than ~30 days, sets `last_daily_write` to today
  when mode is daily), and validates the output as RSS 2.0. If it exits
  non-zero, STOP — do not push. Report what failed.

## Step 6 — Push (only if something changed)

- If the build produced changes, run `python scripts/push_state.py` to upload
  `state/feed.xml` and `state/seen.json` to S3.
- If incremental mode added nothing, skip the push and exit cleanly. (The build
  script prints a "feed unchanged" line and leaves the files untouched in that
  case.)

## Non-negotiables

- **Never push a broken or empty feed over a good one.** If research, parsing, or
  S3 fails, exit without destroying the existing feed in S3. (`build_feed.py` and
  `push_state.py` both refuse to write/upload an invalid or empty feed, but hold
  to this yourself too.)
- **Curate to Nick's interests, not to volume.** Prefer primary sources (project
  blogs, official releases, reputable reporting) over aggregators and rewrites.
- No questions, no chat — this is an unattended run. Do the work and stop.

Read `docs/s3-access.md` for bucket/credential details and `docs/feed-schema.md`
for the item format and `[BREAKING]` convention.

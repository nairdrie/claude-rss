# Feed schema

The live feed is a valid **RSS 2.0** document at
`s3://nairdrie.com/feed/merged.xml`, served at
`https://nairdrie.com/feed/merged.xml`.

## Minimal valid document

```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:atom="http://www.w3.org/2005/Atom" version="2.0">
  <channel>
    <title>Nick's Curated Feed</title>
    <link>https://nairdrie.com/feed/merged.xml</link>
    <description>AI-curated personal feed across tech, dev, and hobbies</description>
    <language>en</language>
    <atom:link href="https://nairdrie.com/feed/merged.xml" rel="self" type="application/rss+xml"/>
    <!-- zero or more <item> elements, newest first -->
  </channel>
</rss>
```

A channel with **no items is still valid** — that's exactly what
`fetch_state.py` writes to seed the very first run.

## One `<item>` block

```xml
<item>
  <title>Anthropic ships MCP connector registry</title>
  <link>https://www.anthropic.com/news/mcp-registry</link>
  <guid isPermaLink="true">https://www.anthropic.com/news/mcp-registry</guid>
  <pubDate>Tue, 26 Aug 2026 13:20:00 -0400</pubDate>
  <description>First-party MCP server discovery — relevant to the Claude-orchestrated pipeline. — Source: Anthropic</description>
  <source url="https://www.anthropic.com/">Anthropic</source>
  <dc:creator>Anthropic</dc:creator>
  <media:thumbnail url="https://www.anthropic.com/images/mcp-registry-og.png"/>
  <enclosure url="https://www.anthropic.com/images/mcp-registry-og.png" type="image/png" length="0"/>
</item>
```

### Field rules

| Element              | Rule                                                                                   |
| --------------------- | -------------------------------------------------------------------------------------- |
| `<guid>`              | **The article URL.** This is the dedup key — it must match the `url` recorded in `seen.json`. Emitted with `isPermaLink="true"`. |
| `<link>`              | The article URL (same value as the guid).                                              |
| `<pubDate>`           | **RFC 822**, timezone-aware (e.g. `Tue, 26 Aug 2026 13:20:00 -0400`), always in the configured local zone. The build assigns the live feed a **staggered, strictly-decreasing timeline** (see the ordering note below) rather than echoing `published_at` verbatim — otherwise a whole daily window of date-only items would collapse onto a single local-midnight timestamp. `published_at` still drives the freshness cutoff and per-topic recency ordering. |
| `<title>`             | The headline. **Breaking items are prefixed with `[BREAKING] `** (see below).          |
| `<description>`       | The one-line curation `reason`, with `— Source: <source>` appended when a source is given. |
| `<source>`            | **Per-item channel of origin** — the RSS 2.0 element whose text is "the name of the RSS channel that the item came from." This is what makes the single feed read as an *aggregation of distinct sources* instead of one monolithic "Nick's Curated Feed" channel. Text is the curated `source` (`Anthropic`, `CoinDesk`, `Chess.com`); when an item has no `source`, it falls back to the article's bare domain (`sportsnet.ca`) so every item still gets a distinct origin. The required `url` attribute points at the source's homepage (`scheme://host/`) — the best stable link we have without knowing the source's real feed URL. |
| `<dc:creator>`        | The same source name as `<source>`, emitted under the Dublin Core namespace (`xmlns:dc` on the root). `<source>` is the spec-correct "origin" element but many readers don't surface it in a single-subscription view; `<dc:creator>` is the byline element those readers (Feedly, Inoreader, NetNewsWire, Miniflux, Reeder) *do* render per item, so the per-source attribution is actually visible. |
| `<media:thumbnail>`   | **Optional.** Emitted only when the item has an `image`. This is what most readers (Feedly, Inoreader, NetNewsWire) look for. |
| `<enclosure>`         | **Optional**, alongside `media:thumbnail` — a fallback for readers that only check enclosures. `type` is guessed from the image URL's extension (defaults to `image/jpeg`); `length` is always `0` since the real byte size isn't known at scrape time. |

## Pinned items

`interests.yaml`'s `pinned` list (currently just the daily chess puzzle) is
injected by `build_feed.py` — never sourced from research, never subject to
the freshness cutoff, and never deduped against `seen.json`. A pinned item's
`url` is expected to stay the same day to day while its real content changes
server-side (e.g. `chess.com/daily` always shows *today's* puzzle) —
build_feed.py works around this by giving each day's instance a distinct
`<guid isPermaLink="false">url#YYYY-MM-DD</guid>`, so readers treat each
day's puzzle as a new item even though the `<link>` itself never changes.

**The daily build pins it to the very top** of the freshly rebuilt window.
**Incremental builds do not re-pin it** — they carry the existing window
through as-is and stamp the new trickle *above* it, so the pinned puzzle keeps
the pubDate the morning gave it and sinks down the feed as the day's additions
land on top. The next morning's daily rebuild introduces that day's puzzle
back at the top. (Earlier every build re-pinned it, gluing it to position 1;
it now behaves like a normal top-of-morning item that ages down through the
day. A consequence: on a very busy day it can eventually be trimmed out of the
`window_size` tail before the next daily — that's the burial working as
intended, and the morning rebuild restores it.)

## `[BREAKING]` convention

When a curated item has `"breaking": true`, `build_feed.py` prefixes its
`<title>` with `[BREAKING] ` and, in incremental mode, orders breaking items
ahead of the other new additions. Use it sparingly and honestly — a routine
software release is not breaking.

## Rolling window vs. history

- The live feed is a **rolling window of the `window_size` newest items**
  (default 50). In daily mode the window is selected by recency/curation, then
  **interleaved across topics** so it reads as a mix, then given a clean
  **staggered timeline**: strictly-decreasing synthetic `pubDate`s spaced a few
  minutes apart, anchored just below "now". This keeps a reader's pubDate sort
  from collapsing the date-only items (which would otherwise all be local
  midnight) and preserves the interleaved order. Within a topic, items with a
  real `published_at` time still sort newest-first before the stagger is laid
  down. In incremental mode new items are stamped just above the existing
  window (breaking first) and the tail is trimmed back to `window_size`. The
  pinned puzzle is part of that existing window — it's placed on top only by
  the daily build, then sinks as later incrementals stamp above it.
- Items that age out of the window **disappear from `feed.xml`** but their URLs
  **remain in `seen.json`** so they're never re-added. `seen.json` entries are
  pruned after ~30 days, at which point a still-relevant story could resurface.

## `curated.json` (build input)

`build_feed.py` reads `state/curated.json` — a JSON array the woken agent
writes. Each item:

```json
{
  "title": "Anthropic ships MCP connector registry",
  "url": "https://www.anthropic.com/news/mcp-registry",
  "source": "Anthropic",
  "topic": "dev-tools",
  "published_at": "2026-08-26T13:20:00-04:00",
  "reason": "First-party MCP server discovery — relevant to the pipeline.",
  "breaking": false,
  "image": "https://www.anthropic.com/images/mcp-registry-og.png"
}
```

`url` is required (items without one are skipped). `published_at` accepts ISO
8601 or RFC 822; anything unparseable falls back to the current time. **Prefer a
full timestamp with a real time of day** (`2026-08-26T13:20:00-04:00`) when the
research surfaces one — a bare date (`2026-08-26`) resolves to local midnight,
and see the pubDate/ordering note below for how the build treats that.

`topic` is optional but recommended: set it to the item's `interests.yaml`
topic (`crypto`, `dev-tools`, `sports`, …). The daily build uses it to
interleave the window so it reads as a topic mix rather than a block of crypto
followed by a block of tech. When absent, the build falls back to the `source`
(then the URL's domain) as the interleave key.

`image` is optional and usually absent at this stage — `scripts/fetch_thumbnails.py`
fills it in afterward by scraping each item's `og:image` (see Step 4.5 in
`CLAUDE.md`). An item that already has an `image` (e.g. a research agent
found one directly) is left untouched by that step. Scraping needs outbound
network access to the article's domain; if the feed environment's egress policy
blocks it, thumbnails stay empty and the step logs an egress note (see
`docs/s3-access.md` → Network egress).

## `seen.json` (state)

```json
{
  "last_daily_write": "2026-08-26",
  "seen": [
    { "url": "https://www.anthropic.com/news/mcp-registry", "first_seen": "2026-08-26" }
  ]
}
```

- `last_daily_write` — the date (America/Toronto) of the last full daily
  rebuild. Drives DAILY-vs-INCREMENTAL mode selection.
- `seen` — every URL put into the feed in roughly the last 30 days, with its
  first-seen date. The dedup memory.

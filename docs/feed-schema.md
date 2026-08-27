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
  <media:thumbnail url="https://www.anthropic.com/images/mcp-registry-og.png"/>
  <enclosure url="https://www.anthropic.com/images/mcp-registry-og.png" type="image/png" length="0"/>
</item>
```

### Field rules

| Element              | Rule                                                                                   |
| --------------------- | -------------------------------------------------------------------------------------- |
| `<guid>`              | **The article URL.** This is the dedup key — it must match the `url` recorded in `seen.json`. Emitted with `isPermaLink="true"`. |
| `<link>`              | The article URL (same value as the guid).                                              |
| `<pubDate>`           | **RFC 822**, timezone-aware (e.g. `Tue, 26 Aug 2026 13:20:00 -0400`). Parsed from the curated item's `published_at`, which may be ISO 8601 or RFC 822 on input. |
| `<title>`             | The headline. **Breaking items are prefixed with `[BREAKING] `** (see below).          |
| `<description>`       | The one-line curation `reason`, with `— Source: <source>` appended when a source is given. |
| `<media:thumbnail>`   | **Optional.** Emitted only when the item has an `image`. This is what most readers (Feedly, Inoreader, NetNewsWire) look for. |
| `<enclosure>`         | **Optional**, alongside `media:thumbnail` — a fallback for readers that only check enclosures. `type` is guessed from the image URL's extension (defaults to `image/jpeg`); `length` is always `0` since the real byte size isn't known at scrape time. |

## Pinned items

`interests.yaml`'s `pinned` list (currently just the daily chess puzzle) is
always injected at the very top of the feed by `build_feed.py`, in both
daily and incremental builds — it's never sourced from research, never
subject to the freshness cutoff, and never deduped against `seen.json`. A
pinned item's `url` is expected to stay the same day to day while its real
content changes server-side (e.g. `chess.com/daily` always shows *today's*
puzzle) — build_feed.py works around this by giving each day's instance a
distinct `<guid isPermaLink="false">url#YYYY-MM-DD</guid>`, so readers treat
each day's puzzle as a new item even though the `<link>` itself never
changes. Every build re-evicts any existing item with the pinned url and
re-inserts the freshly-built one, so a stale or missing pinned item
self-heals on the very next run (daily or incremental) rather than waiting
for tomorrow's rebuild.

## `[BREAKING]` convention

When a curated item has `"breaking": true`, `build_feed.py` prefixes its
`<title>` with `[BREAKING] ` and, in incremental mode, orders breaking items
ahead of the other new additions. Use it sparingly and honestly — a routine
software release is not breaking.

## Rolling window vs. history

- The live feed is a **rolling window of the `window_size` newest items**
  (default 50). In daily mode the window is rebuilt newest-by-`pubDate`; in
  incremental mode new items are prepended (breaking first) and the tail is
  trimmed back to `window_size`.
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
  "published_at": "2026-08-26T13:20:00-04:00",
  "reason": "First-party MCP server discovery — relevant to the pipeline.",
  "breaking": false,
  "image": "https://www.anthropic.com/images/mcp-registry-og.png"
}
```

`url` is required (items without one are skipped). `published_at` accepts ISO
8601 or RFC 822; anything unparseable falls back to the current time. `image`
is optional and usually absent at this stage — `scripts/fetch_thumbnails.py`
fills it in afterward by scraping each item's `og:image` (see Step 4.5 in
`CLAUDE.md`). An item that already has an `image` (e.g. a research agent
found one directly) is left untouched by that step.

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

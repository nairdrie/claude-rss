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
</item>
```

### Field rules

| Element         | Rule                                                                                   |
| --------------- | -------------------------------------------------------------------------------------- |
| `<guid>`        | **The article URL.** This is the dedup key — it must match the `url` recorded in `seen.json`. Emitted with `isPermaLink="true"`. |
| `<link>`        | The article URL (same value as the guid).                                              |
| `<pubDate>`     | **RFC 822**, timezone-aware (e.g. `Tue, 26 Aug 2026 13:20:00 -0400`). Parsed from the curated item's `published_at`, which may be ISO 8601 or RFC 822 on input. |
| `<title>`       | The headline. **Breaking items are prefixed with `[BREAKING] `** (see below).          |
| `<description>` | The one-line curation `reason`, with `— Source: <source>` appended when a source is given. |

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
  "breaking": false
}
```

`url` is required (items without one are skipped). `published_at` accepts ISO
8601 or RFC 822; anything unparseable falls back to the current time.

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

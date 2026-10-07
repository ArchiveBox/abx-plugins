# parse_sitemap_urls

Discover URLs from `sitemap.xml` (urlset and sitemapindex documents, gzipped
sitemaps, `robots.txt` `Sitemap:` directives, and the Google image / video /
news extensions) and emit one `Snapshot` JSONL record per discovered URL.

This plugin closes the gap that motivated [ArchiveBox#191][issue-191]: a
single seed URL can expand into a full-site crawl without an external
crawler in the loop. The host (ArchiveBox / `abx-dl`) keeps ownership of
the crawl frontier, depth cap, and dedup; this hook only feeds it URLs.

[sitemap index]: https://www.sitemaps.org/protocol.html#index
[issue-191]: https://github.com/ArchiveBox/ArchiveBox/issues/191

## What it does

Given a seed URL the hook tries, in order:

1. **`*.xml` / `*.xml.gz`** — parse directly as a sitemap or sitemap-index.
2. **`*/robots.txt`** — read every `Sitemap:` line and walk each one.
3. **Anything else** (treated as a site root):
   1. Probe `<root>/robots.txt` for `Sitemap:` directives.
   2. If none found, fall back to the paths in
      `PARSE_SITEMAP_URLS_FALLBACK_PATHS`
      (default: `/sitemap.xml`, `/sitemap_index.xml`,
      `/sitemap-index.xml`, `/wp-sitemap.xml`, `/sitemap.xml.gz`).

For each `<urlset>` document the hook emits a `Snapshot` record per
`<loc>`, preserving the optional `<lastmod>` value as `bookmarked_at`
and recording `<priority>` / `<changefreq>` for filtering. For each
`<sitemapindex>` document it recurses into the child sitemaps up to
`PARSE_SITEMAP_URLS_MAX_SITEMAP_DEPTH`.

Gzipped sitemaps (detected by the `1f 8b` magic bytes, a `.gz` suffix,
or a `Content-Encoding: gzip` response header) are transparently
decompressed under hard size / ratio caps. UTF-8, UTF-16 LE, and
UTF-16 BE byte-order marks are stripped before parsing. Fragments are
stripped from emitted URLs so `#anchor` variants do not produce
duplicate snapshots.

### Optional sitemap extensions

| Extension | Config | Behavior |
| --- | --- | --- |
| [Image][img] | `PARSE_SITEMAP_URLS_EMIT_IMAGE_URLS=true` | Emits each `<image:loc>` as an extra `Snapshot` with `tags=sitemap-media`. |
| [Video][vid] | `PARSE_SITEMAP_URLS_EMIT_VIDEO_URLS=true` | Emits each `<video:content_loc>` and `<video:player_loc>` similarly. |
| [News][news] | `PARSE_SITEMAP_URLS_EMIT_NEWS_TAG=true` | Emits a `Tag` record per unique `<news:publication><news:name>`. |

[img]: https://developers.google.com/search/docs/crawling-indexing/sitemaps/image-sitemaps
[vid]: https://developers.google.com/search/docs/crawling-indexing/sitemaps/video-sitemaps
[news]: https://developers.google.com/search/docs/crawling-indexing/sitemaps/news-sitemap

## Security posture

Sitemaps come from untrusted servers. The hook applies the following
defenses by default:

- **XML hardening.** Parsing goes through `defusedxml`, which rejects
  DTDs, internal/external entities, and external-resource resolution.
  Billion-laughs and XXE payloads fail-closed.
- **Response size cap.** Each HTTP response is bounded to
  `PARSE_SITEMAP_URLS_MAX_RESPONSE_BYTES` (default 50 MiB) before any
  parsing happens.
- **Decompression cap.** Gzipped responses are bounded to
  `PARSE_SITEMAP_URLS_MAX_DECOMPRESSED_BYTES` (default 200 MiB) and the
  decompressed/compressed ratio is bounded to
  `PARSE_SITEMAP_URLS_GZIP_MAX_RATIO` (default 100×). Gzip bombs fail
  with `status=failed`.
- **Scheme allowlist.** Only `http` and `https` are accepted as
  page-URL schemes; `javascript:`, `data:`, `ftp:`, and similar are
  refused. `file://` is allowed only when the seed itself is `file://`
  or when `PARSE_SITEMAP_URLS_ALLOW_FILE_URLS=true` is set.
- **Bounded, validated redirects.** Redirects are capped by
  `PARSE_SITEMAP_URLS_HTTP_MAX_REDIRECTS` and rejected when the target
  uses a non-HTTP scheme or resolves to a loopback / RFC1918 /
  link-local / multicast address (unless
  `PARSE_SITEMAP_URLS_ALLOW_PRIVATE_HOSTS=true`).
- **Per-emit regex scan length cap.** `INCLUDE_REGEX` / `EXCLUDE_REGEX`
  scan only the first `PARSE_SITEMAP_URLS_REGEX_INPUT_CAP` characters
  of each URL, blunting catastrophic-backtracking risk on long URLs.
- **Sitemap attempt cap.** `PARSE_SITEMAP_URLS_MAX_SITEMAPS` caps the
  number of sitemap fetch *attempts* (default 100), so an adversarial
  sitemap-index pointing at thousands of 404 / timeout / refused
  children cannot trigger that many outbound requests.

The seed URL is also subject to the scheme + private-host gates, so a
crafted `archivebox add file:///etc/passwd` does not produce a
disclosable record unless the operator explicitly opts in.

**DNS-rebinding caveat.** The private-host check resolves the
hostname at policy time, but `urllib` resolves it again at connect
time. A rebinding DNS record could return a public IP to the first
lookup and a private IP to the second. This plugin does not pin the
resolved IP through to the socket connect; if your threat model
includes DNS rebinding, run behind an outbound firewall that blocks
RFC1918 / loopback targets at the network layer.

## Configuration

| Env var | Default | Description |
| --- | --- | --- |
| `PARSE_SITEMAP_URLS_ENABLED` (`USE_PARSE_SITEMAP_URLS`, `SAVE_SITEMAP_URLS`) | `true` | Toggle the plugin. |
| `PARSE_SITEMAP_URLS_MAX_URLS` | `5000` | Hard cap on emitted `Snapshot` records per invocation. |
| `PARSE_SITEMAP_URLS_MAX_SITEMAP_DEPTH` | `5` | Max recursion depth when following sitemap-index documents. `0` walks only the seed; `1` walks seed plus one level of children. |
| `PARSE_SITEMAP_URLS_MAX_SITEMAPS` | `100` | Max number of sitemap fetch attempts across the entire walk (defense against adversarial sitemap-indexes pointing at thousands of empty / broken children). `0` disables the cap. |
| `PARSE_SITEMAP_URLS_TIMEOUT` (fallback: `TIMEOUT`) | `60` | Network timeout per fetch, in seconds. |
| `PARSE_SITEMAP_URLS_USER_AGENT` (fallback: `USER_AGENT`) | shared default | User-Agent for HTTP requests. |
| `PARSE_SITEMAP_URLS_INCLUDE_REGEX` | `""` | Only URLs matching this regex are emitted (scanned up to `REGEX_INPUT_CAP` chars). |
| `PARSE_SITEMAP_URLS_EXCLUDE_REGEX` | `""` | URLs matching this regex are skipped. |
| `PARSE_SITEMAP_URLS_REGEX_INPUT_CAP` | `8192` | Maximum URL prefix length scanned by the regex filters. |
| `PARSE_SITEMAP_URLS_SAME_HOST_ONLY` | `false` | Skip URLs whose host differs from the seed URL's host. |
| `PARSE_SITEMAP_URLS_DISCOVER_FROM_ROBOTS` | `true` | Probe `robots.txt` for `Sitemap:` directives. |
| `PARSE_SITEMAP_URLS_FALLBACK_PATHS` | `[/sitemap.xml, /sitemap_index.xml, /sitemap-index.xml, /wp-sitemap.xml, /sitemap.xml.gz]` | Paths to probe when no robots.txt sitemap was found. |
| `PARSE_SITEMAP_URLS_HTTP_RETRIES` | `2` | Retries on transient failures (408, 429, 5xx, network errors). |
| `PARSE_SITEMAP_URLS_HTTP_BACKOFF_SECONDS` | `1.0` | Base delay for exponential backoff between retries. |
| `PARSE_SITEMAP_URLS_HTTP_MAX_REDIRECTS` | `5` | Max HTTP redirects per fetch. The custom redirect handler rejects non-HTTP schemes and private hosts. |
| `PARSE_SITEMAP_URLS_MAX_RESPONSE_BYTES` | `52428800` | Maximum on-the-wire response size (50 MiB). |
| `PARSE_SITEMAP_URLS_MAX_DECOMPRESSED_BYTES` | `209715200` | Maximum size after gzip decompression (200 MiB). |
| `PARSE_SITEMAP_URLS_GZIP_MAX_RATIO` | `100` | Maximum decompressed/compressed ratio (gzip bomb guard); `0` disables. |
| `PARSE_SITEMAP_URLS_ALLOW_PRIVATE_HOSTS` | `false` | Allow fetches and redirects to loopback / RFC1918 / link-local / multicast addresses. |
| `PARSE_SITEMAP_URLS_ALLOW_FILE_URLS` | `false` | Allow `file://` URLs in fetched sitemaps when the seed is not `file://`. |
| `PARSE_SITEMAP_URLS_VERIFY_TLS` (fallback: `CHECK_SSL_VALIDITY`) | `true` | Verify TLS certificates on HTTPS fetches. |
| `PARSE_SITEMAP_URLS_ACCEPT_LANGUAGE` | `""` | Optional `Accept-Language` header value. |
| `PARSE_SITEMAP_URLS_EMIT_IMAGE_URLS` | `false` | Emit URLs from `<image:loc>` (Sitemap image extension). Subject to the same URL policy as page URLs. |
| `PARSE_SITEMAP_URLS_EMIT_VIDEO_URLS` | `false` | Emit URLs from `<video:content_loc>` / `<video:player_loc>`. |
| `PARSE_SITEMAP_URLS_EMIT_NEWS_TAG` | `false` | Emit `Tag` records for `<news:publication><news:name>`. |
| `PARSE_SITEMAP_URLS_PRIORITY_MIN` | `0.0` | Drop URLs whose `<priority>` is below this threshold (`0.0` disables). Entries without `<priority>` pass through unless `REQUIRE_PRIORITY=true`. |
| `PARSE_SITEMAP_URLS_REQUIRE_PRIORITY` | `false` | When `PRIORITY_MIN > 0`, also drop URLs with no `<priority>` tag. |
| `PARSE_SITEMAP_URLS_CHANGEFREQ_ALLOWED` | `[]` | When non-empty, only emit URLs whose `<changefreq>` appears in this list. |
| `PARSE_SITEMAP_URLS_SORT_BY` | `url` | `url` (alpha) / `lastmod` (newest first) / `priority` (highest first) / `order` (preserve sitemap order). |
| `PARSE_SITEMAP_URLS_VERBOSE` | `false` | Emit one `fetching sitemap …` line per fetch to stderr. |

The plugin also honours the shared `USER_AGENT`, `TIMEOUT`,
`CHECK_SSL_VALIDITY`, and `SNAP_DIR` env vars from `base/config.json`.

## Outputs

- **stdout** — one JSONL record per line:
  - 0+ `Tag` records (when the news extension is enabled).
  - 0+ `Snapshot` records (one per discovered URL, with
    `depth = parent + 1`). Media extras carry `tags=sitemap-media`.
  - Exactly one terminal `ArchiveResult` record.
- **`SNAP_DIR/parse_sitemap_urls/urls.jsonl`** — same `Snapshot` records,
  persisted for the host's crawl frontier. Written atomically and
  removed on `noresults` / `failed`.
- **stderr** — discovery / fetch error lines and the human summary of
  the `ArchiveResult`.

`ArchiveResult.status` follows the abx contract:

| status | meaning |
| --- | --- |
| `succeeded` | At least one URL emitted. |
| `noresults` | No URLs (empty sitemap, or every URL filtered out). |
| `skipped` | `PARSE_SITEMAP_URLS_ENABLED=false`. |
| `failed` | Every candidate sitemap failed to fetch or parse, or a security guard tripped. |

The summary string carries counters so logs make it obvious why nothing
emitted, e.g.
`0 URLs parsed (visited 1 sitemap(s); skipped_filter=3 skipped_host=0 skipped_priority=2 skipped_changefreq=0 skipped_scheme=1 skipped_extras=0)`.

## Examples

```bash
# Just give it a site root.
./on_Snapshot__76_parse_sitemap_urls.py --url=https://example.com

# Point directly at a known sitemap.
./on_Snapshot__76_parse_sitemap_urls.py --url=https://example.com/sitemap.xml

# Point at robots.txt (reads all Sitemap: lines).
./on_Snapshot__76_parse_sitemap_urls.py --url=https://example.com/robots.txt

# Restrict to a subtree of a large site.
PARSE_SITEMAP_URLS_INCLUDE_REGEX="^https://example\\.com/blog/" \
    ./on_Snapshot__76_parse_sitemap_urls.py --url=https://example.com

# Skip product pages while crawling marketing pages.
PARSE_SITEMAP_URLS_EXCLUDE_REGEX="/products/" \
    ./on_Snapshot__76_parse_sitemap_urls.py --url=https://example.com

# Lock the crawl to the seed host (skip CDN / asset hosts).
PARSE_SITEMAP_URLS_SAME_HOST_ONLY=true \
    ./on_Snapshot__76_parse_sitemap_urls.py --url=https://example.com

# Only crawl high-priority, daily-refreshed pages, newest first.
PARSE_SITEMAP_URLS_PRIORITY_MIN=0.7 \
PARSE_SITEMAP_URLS_CHANGEFREQ_ALLOWED='["daily","hourly"]' \
PARSE_SITEMAP_URLS_SORT_BY=lastmod \
    ./on_Snapshot__76_parse_sitemap_urls.py --url=https://example.com

# Aggressive HTTP retries against a flaky server.
PARSE_SITEMAP_URLS_HTTP_RETRIES=5 \
PARSE_SITEMAP_URLS_HTTP_BACKOFF_SECONDS=2.0 \
    ./on_Snapshot__76_parse_sitemap_urls.py --url=https://example.com/sitemap.xml

# Pull image URLs out of an image sitemap as additional Snapshots.
PARSE_SITEMAP_URLS_EMIT_IMAGE_URLS=true \
    ./on_Snapshot__76_parse_sitemap_urls.py --url=https://example.com/image-sitemap.xml

# Self-hosted intranet sitemap — explicitly allow private IPs.
PARSE_SITEMAP_URLS_ALLOW_PRIVATE_HOSTS=true \
    ./on_Snapshot__76_parse_sitemap_urls.py --url=https://intranet.local/sitemap.xml
```

## Running with ArchiveBox / abx-dl

The hook follows the standard `on_Snapshot__*` contract:

- File name `on_Snapshot__76_parse_sitemap_urls.py` places it after
  `parse_dom_outlinks (75)` and before any later snapshot work.
- It depends only on the Python standard library plus `rich_click`,
  `defusedxml`, and `abx_plugins.plugins.base.utils`. No binary
  preflight and no `required_plugins`.
- It emits `Snapshot` records the host consumes via its normal crawl
  frontier; the host applies its own `max_depth` / `max_urls` ceiling
  on top of the plugin-level caps documented above.

## Notes and non-goals

- **JS-rendered links are out of scope.** Pair with
  [`parse_dom_outlinks`](../parse_dom_outlinks/) for SPAs that don't
  publish a complete sitemap.
- **Politeness is the host's job.** This hook fetches at most one
  document per visited sitemap node and never crawls page content; the
  host applies rate-limiting when it later fetches each discovered URL.
- **No HTTP caching between runs.** Reruns re-fetch sitemaps so updates
  propagate; existing `urls.jsonl` is overwritten atomically.

## License

MIT — same as the parent `abx-plugins` package.

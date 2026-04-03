# Event-Driven Hook Migration Plan

## Goal

Replace the current mix of:

- fg/bg ordering barriers
- numeric filename ordering used as dependency encoding
- marker files like `cdp_url.txt`, `target_id.txt`, `extensions.json`, `navigation.json`, `prenav.json`
- sibling file/log polling

with a smaller model:

- hooks subscribe only via `on_<EventName>__...`
- hooks emit real facts on stdout as JSONL
- `abx-dl` routes those facts dynamically through `abxbus`
- `abx-dl` auto-emits `After<EventName>` as a synthetic settle barrier
- `abx-dl` reduces all prior scoped events into a derived key/value context
- `abx-dl` mirrors crawl-scoped context to `CRAWL_DIR/derived.env`
- `abx-dl` mirrors snapshot-scoped context to `SNAP_DIR/derived.env` by overlaying snapshot state on top of the current crawl context
- hooks receive the full reduced context as env vars
- hooks receive only the triggering event payload as CLI args

`Chrome` remains the stable namespace for all Chrome-like providers.

## Core Model

### 1. One hook subscription form

Use only:

- `on_<EventName>__...`

Examples:

- `on_SnapshotChromeTabReady__21_consolelog.daemon.bg.js`
- `on_AfterSnapshotChromeTabReady__30_chrome_navigate.js`
- `on_SnapshotChromeTabNavigated__51_screenshot.js`
- `on_AfterSnapshot__70_parse_html_urls.py`

There is no separate `after_*` hook syntax.

### 2. Two kinds of events

#### Real domain facts

Emitted by hooks when they create new meaning or new payload.

Examples:

- `CrawlChromeBrowserReady`
- `CrawlChromeExtensionsReady`
- `SnapshotChromeBrowserReady`
- `SnapshotChromeExtensionsReady`
- `SnapshotChromeTabReady`
- `SnapshotChromeTabNavigated`
- `SnapshotChromeTabNavigationFailed`
- `SnapshotChromeMainResponseSaved`
- `SnapshotChromeStaticFileHandled`
- `SnapshotPdfOcrComplete`
- `UrlDiscovered`

#### Synthetic settle barriers

Emitted only by `abx-dl`.

Examples:

- `AfterSnapshot`
- `AfterSnapshotChromeTabReady`
- `AfterSnapshotChromeTabNavigated`
- `AfterSnapshotPdfOcrComplete`

Meaning:

- `After<Event>` = the full subtree rooted at `<Event>` has settled

`After<Event>` is for ordering only. It does not introduce new semantics.

### 3. Use real events vs `After<Event>` consistently

Use a real event when:

- a hook creates a meaningful state transition
- a hook creates new payload another hook may consume
- a hook wants to update the shared derived context

Use `After<Event>` when:

- a later hook only needs barrier/ordering semantics
- no new domain payload is needed

Examples:

- `chrome_navigate` should run on `AfterSnapshotChromeTabReady` once pre-navigation bg settle semantics are defined precisely
- `screenshot` should run on `SnapshotChromeTabNavigated`, not `AfterSnapshotChromeTabReady`
- late parsers/indexers should run on `AfterSnapshot`
- OCR should emit a real event like `SnapshotPdfOcrComplete`
- late OCR consumers can run on `AfterSnapshotPdfOcrComplete`

### 4. `abx-dl` stays abstract

`abx-dl` must not know Chrome-specific meanings.

It only needs to know:

- how to route events by string name
- how to launch hooks for `on_<Event>`
- how to synthesize `After<Event>`
- how to route both real events and synthetic `After<Event>` barriers through the same generic dispatch path
- how to reduce scoped prior events into current key/value context
- how to mirror reduced context into `derived.env`
- how to pass env vars and event payload args into hooks
- how to wait for the full root `Snapshot` tree before cleanup

No plugin-specific event classes or Chrome-specific scheduling logic should be added to `abx-dl`.

## Event Format

Hooks emit JSONL records with:

- required top-level `type`
- optional normal event payload fields
- optional reserved `env` patch for durable aggregate state

Example:

```json
{
  "type": "SnapshotChromeTabReady",
  "url": "https://example.com",
  "env": {
    "CDP_URL": "ws://127.0.0.1:9222/devtools/browser/...",
    "TARGET_ID": "ABC123",
    "EXTENSIONS": [
      {"id": "ublock", "name": "uBlock Origin"}
    ]
  }
}
```

### Reserved `env` patch semantics

`env` is the only generic state-update lane.

Rules:

- scalar values: last write wins
- `null`: unset/remove key
- arrays: append
- objects: deep-merge
- type changes: latest value replaces incompatible earlier type

Implications:

- multiple hooks can contribute to `EXTENSIONS`
- later hooks can correct stale `TARGET_ID` / `CDP_URL`
- later hooks launched by `abx-dl` will automatically see corrected values

For `derived.env` mirroring:

- scalars are written as plain env values
- arrays/objects are serialized as JSON strings

Examples:

- `CDP_URL=ws://...`
- `TARGET_ID=ABC123`
- `EXTENSIONS=[{"id":"ublock","name":"uBlock Origin"}]`

Only `abx-dl` writes `derived.env`.

## Reduced Context

### Source of truth

The event log is canonical.

### Derived projection

`abx-dl` should maintain scope directionality explicitly:

- crawl events reduce into one crawl context mirrored to `CRAWL_DIR/derived.env`
- each snapshot starts from the current crawl context
- snapshot events then overlay snapshot-local state on top of that base
- the resulting per-snapshot view is mirrored to `SNAP_DIR/derived.env`
- snapshot-local reduction must never write back into `CRAWL_DIR/derived.env`

`derived.env` is:

- generated
- for observability and crash inspection
- useful for manual standalone hook invocation
- not the canonical source of truth

This one-way flow is required because multiple snapshots may run in parallel within a single crawl.

### What should live in reduced context

Only durable/current-state values that later hooks may need fresh:

- `URL`
- `CRAWL_ID`
- `SNAPSHOT_ID`
- `CRAWL_DIR`
- `SNAP_DIR`
- `CDP_URL`
- `TARGET_ID`
- `FINAL_URL`
- `HTTP_STATUS`
- `EXTENSIONS`
- `MAIN_RESPONSE_PATH`
- `MAIN_RESPONSE_MIME`
- `STATICFILE_HANDLED`
- `STATICFILE_PATH`

Avoid stuffing transient/debug-only values into reduced context.

## Hook Input Contract

### Environment variables

Every hook gets the full reduced context as env vars.

Examples:

- `URL`
- `CRAWL_ID`
- `SNAPSHOT_ID`
- `SNAP_DIR`
- `CDP_URL`
- `TARGET_ID`
- `FINAL_URL`
- `HTTP_STATUS`
- `EXTENSIONS`

### CLI args

Every hook also gets the triggering event payload as `--kebab-case=value` CLI args.

Important split:

- reduced context is passed via env vars
- current event payload is passed via CLI args
- only fields on the triggering event become CLI args
- reduced context values should not also be sprayed onto the CLI unless they are part of the triggering event payload

Example:

- env:
  - `URL=...`
  - `CDP_URL=...`
  - `TARGET_ID=...`
- CLI:
  - `--url=...`
  - `--final-url=...`
  - `--http-status=200`

### `EXTRA_CONTEXT`

`EXTRA_CONTEXT` stays opaque pass-through metadata only.

- hooks should not depend on it for runtime behavior
- hooks should consume explicit env vars / CLI args instead
- emitted `ArchiveResult`, `Snapshot`, and `UrlDiscovered` records may still merge it forward for lineage

## `abx-dl` Runtime Changes

### `../abx-dl/abx_dl/events.py`

- make root lifecycle event types line up with hook filenames:
  - `CrawlSetup`
  - `Snapshot`

### `../abx-dl/abx_dl/models.py`

- keep `parse_hook_filename()` as the source of truth for `on_<Event>__...`
- no whitelist of supported event names
- store the raw trigger event string exactly as parsed from the filename

### `../abx-dl/abx_dl/services/crawl_service.py`

- keep the existing service
- dispatch all crawl-tree hooks dynamically by event string, including synthetic `AfterCrawl*` events if/when used
- convert hook stdout records with `type` beginning with `Crawl` into `BaseEvent(event_type=...)`
- reduce prior crawl-scoped events into current crawl context
- mirror reduced crawl context to `CRAWL_DIR/derived.env`
- pass reduced crawl context as env vars plus current event payload as CLI args
- synthesize `After<Event>` for crawl events

### `../abx-dl/abx_dl/services/snapshot_service.py`

- keep the existing service
- dispatch all snapshot-tree hooks dynamically by event string, including synthetic `AfterSnapshot*` events
- convert hook stdout records with `type` beginning with `Snapshot` into `BaseEvent(event_type=...)`
- route non-reserved emitted events like `UrlDiscovered` as normal bus events under the current snapshot tree instead of hardcoding exact `type == "Snapshot"`
- start from the current crawl context as a base
- reduce prior snapshot-scoped events into a snapshot-local overlay on top of that base
- mirror reduced snapshot context to `SNAP_DIR/derived.env`
- pass reduced snapshot context as env vars plus current event payload as CLI args
- auto-emit `After<Event>` after the routed event subtree settles
- emit `SnapshotCleanupEvent` only after the full root `Snapshot` tree settles

### `../abx-dl/abx_dl/services/archive_result_service.py`

- expand fallback `ArchiveResult` synthesis beyond hooks whose names start with `on_Snapshot`
- `on_AfterSnapshot__...` hooks must also get the same fallback result behavior
- more generally, fallback result synthesis should apply to hooks whose process is a descendant of a root snapshot lifecycle event, not only to one filename prefix

### Synthetic `After<Event>` semantics

Rules:

1. `After<Event>` is emitted only by `abx-dl`
2. hooks must never emit `After*` themselves
3. `After<Event>` is emitted as a child of `<Event>`, before `<Event>` is considered complete
4. do not auto-generate `AfterAfter<Event>`
5. `SnapshotCleanup` waits for the full root `Snapshot` tree, including all nested `After<Event>` branches
6. `AfterSnapshot` is solid because it is rooted in full snapshot-tree settlement
7. `AfterSnapshotChromeTabReady` must not be treated as implemented until bg pre-navigation hook settle semantics are defined precisely; otherwise it can fire either too early or too late under the current bg process model

That is what makes multi-stage settled pipelines work.

## Event Taxonomy

### Root lifecycle facts

- `CrawlSetup`
- `Snapshot`

### Generic discovery fact

- `UrlDiscovered`

`UrlDiscovered` should replace hooks emitting new `Snapshot` records for crawl discovery.

Payload should carry all useful metadata in one record, e.g.:

- `url`
- `title`
- `tags`
- `bookmarked_at`
- parser/feed/bookmark metadata

### Chrome facts

#### Crawl phase only

- `CrawlChromeBrowserReady`
- `CrawlChromeExtensionsReady`

#### Snapshot phase only

- `SnapshotChromeBrowserReady`
- `SnapshotChromeExtensionsReady`
- `SnapshotChromeTabReady`
- `SnapshotChromeTabNavigated`
- `SnapshotChromeTabNavigationFailed`
- `SnapshotChromeTabClosed`

### Optional Chrome capture facts

Only add these if they materially simplify orchestration:

- `SnapshotChromeMainResponseSaved`
- `SnapshotChromeStaticFileHandled`
- `SnapshotChromeHeadersCaptured`
- `SnapshotChromeRedirectsCaptured`

Do not add a trivial event for every implementation detail.

## Phase / Isolation Rules

- only emit `CrawlChrome*` facts during crawl setup
- only emit `SnapshotChrome*` facts during snapshot execution
- never synthesize snapshot aliases from crawl browser facts

If `CHROME_ISOLATION=crawl`:

- crawl launch emits `CrawlChromeBrowserReady` and `CrawlChromeExtensionsReady`
- snapshot phase starts at `SnapshotChromeTabReady`
- there is no `SnapshotChromeBrowserReady` alias

If `CHROME_ISOLATION=snapshot`:

- snapshot launch emits `SnapshotChromeBrowserReady` and `SnapshotChromeExtensionsReady`
- snapshot phase then proceeds to `SnapshotChromeTabReady`

The first snapshot-scoped Chrome event guaranteed in both modes is:

- `SnapshotChromeTabReady`

## Target Flow

### Crawl setup

1. `CrawlSetup`
2. `on_CrawlSetup__89_chrome_kill_zombies.js`
3. `on_CrawlSetup__90_chrome_launch.daemon.bg.js` in crawl isolation
   - emits `CrawlChromeBrowserReady`
   - emits `CrawlChromeExtensionsReady`
   - patches `env` with browser-level values like `CDP_URL`, `EXTENSIONS`
4. crawl-scoped extension config hooks subscribe to `CrawlChromeExtensionsReady`
   - `abx_plugins/plugins/twocaptcha/on_CrawlSetup__95_twocaptcha_config.js`
   - `abx_plugins/plugins/claudechrome/on_CrawlSetup__96_claudechrome_config.js`

### Root snapshot

1. `Snapshot`
2. root non-Chrome downloaders stay on `Snapshot`, e.g.:
   - `abx_plugins/plugins/ytdlp/on_Snapshot__02_ytdlp.finite.bg.py`
   - `abx_plugins/plugins/gallerydl/on_Snapshot__03_gallerydl.finite.bg.py`
   - `abx_plugins/plugins/forumdl/on_Snapshot__04_forumdl.finite.bg.py`
   - `abx_plugins/plugins/git/on_Snapshot__05_git.finite.bg.py`
   - `abx_plugins/plugins/wget/on_Snapshot__06_wget.finite.bg.py`
   - `abx_plugins/plugins/archivedotorg/on_Snapshot__08_archivedotorg.finite.bg.py`
   - `abx_plugins/plugins/favicon/on_Snapshot__11_favicon.finite.bg.py`
   - `abx_plugins/plugins/papersdl/on_Snapshot__66_papersdl.finite.bg.py`
3. Chrome branch:
   - crawl isolation: `chrome_tab` subscribes directly to `Snapshot`
   - snapshot isolation: launch emits `SnapshotChromeBrowserReady` and `SnapshotChromeExtensionsReady`, then `chrome_tab` runs
4. `chrome_tab` emits `SnapshotChromeTabReady`
   - patches `env` with `TARGET_ID` and any corrected tab-level state
5. pre-navigation listeners run on `SnapshotChromeTabReady`
6. once the pre-navigation bg settle semantics are defined correctly, `chrome_navigate` can run on `AfterSnapshotChromeTabReady`
7. `chrome_navigate` emits `SnapshotChromeTabNavigated` or `SnapshotChromeTabNavigationFailed`
   - patches `env` with corrected `FINAL_URL`, `HTTP_STATUS`, `TARGET_ID` if needed
8. post-navigation extractors run on `SnapshotChromeTabNavigated`
9. once the entire root `Snapshot` tree settles, `abx-dl` emits `AfterSnapshot`
10. late parsers / OCR / indexers / cleanup / hashes run on `AfterSnapshot`
11. if any of those late hooks emit new real events, `abx-dl` may synthesize `After<ThoseEvents>` and the tree continues
12. `SnapshotCleanup` runs only after the full root `Snapshot` tree finishes

### Why `AfterSnapshot` matters

`AfterSnapshot` solves these generically:

- Chrome-disabled runs still work
- URL parsers can run after all snapshot outputs exist
- later stages like OCR can emit real follow-on events without special scheduler code

## High-Value Plugin Migrations

### 1. Delete pure barrier hooks

Delete entirely:

- `abx_plugins/plugins/chrome/on_CrawlSetup__91_chrome_wait.js`
- `abx_plugins/plugins/chrome/on_Snapshot__11_chrome_wait.js`

### 2. Chrome provider hooks

#### `abx_plugins/plugins/chrome/on_CrawlSetup__90_chrome_launch.daemon.bg.js`

- emit `CrawlChromeBrowserReady`
- emit `CrawlChromeExtensionsReady`
- patch `env` with browser-level state

#### `abx_plugins/plugins/chrome/on_Snapshot__09_chrome_launch.daemon.bg.js`

- only run in snapshot isolation
- emit `SnapshotChromeBrowserReady`
- emit `SnapshotChromeExtensionsReady`
- patch `env` with browser-level state
- remove current crawl-isolation no-op behavior

#### `abx_plugins/plugins/chrome/on_Snapshot__10_chrome_tab.daemon.bg.js`

- in crawl isolation, subscribe to `Snapshot`
- in snapshot isolation, subscribe to the relevant snapshot browser-ready event
- emit `SnapshotChromeTabReady`
- patch `env` with fresh tab state
- stop being the publisher of runtime marker files used for orchestration
- split or rewrite helper usage so fresh reduced env state can replace the current session-dir marker contract over time

#### `abx_plugins/plugins/chrome/on_Snapshot__30_chrome_navigate.js`

- move to `on_AfterSnapshotChromeTabReady__30_chrome_navigate.js` once that settle barrier is well-defined for bg pre-navigation hooks
- remove `prenav.json` polling and marker-based gating
- emit `SnapshotChromeTabNavigated` or `SnapshotChromeTabNavigationFailed`
- patch `env` with latest navigation state

### 3. Pre-navigation listener hooks

Move to `on_SnapshotChromeTabReady__...`:

- `abx_plugins/plugins/consolelog/on_Snapshot__21_consolelog.daemon.bg.js`
- `abx_plugins/plugins/dns/on_Snapshot__22_dns.daemon.bg.js`
- `abx_plugins/plugins/sslcerts/on_Snapshot__23_sslcerts.daemon.bg.js`
- `abx_plugins/plugins/responses/on_Snapshot__24_responses.daemon.bg.js`
- `abx_plugins/plugins/redirects/on_Snapshot__25_redirects.daemon.bg.js`
- `abx_plugins/plugins/staticfile/on_Snapshot__26_staticfile.daemon.bg.js`
- `abx_plugins/plugins/headers/on_Snapshot__27_headers.daemon.bg.js`
- `abx_plugins/plugins/ublock/on_Snapshot__12_ublock.daemon.bg.js`
- `abx_plugins/plugins/istilldontcareaboutcookies/on_Snapshot__13_istilldontcareaboutcookies.daemon.bg.js`
- `abx_plugins/plugins/twocaptcha/on_Snapshot__14_twocaptcha.daemon.bg.js`
- `abx_plugins/plugins/modalcloser/on_Snapshot__15_modalcloser.daemon.bg.js`

Remove:

- readiness file creation
- numeric “must run before navigate” comments
- `waitForNavigationComplete(...)` used as control-plane gating

Important note:

- this stage is still the main unresolved runtime detail in the plan
- background pre-navigation hooks currently launch without a precise runtime notion of “settled enough for `AfterSnapshotChromeTabReady`”
- do not move `chrome_navigate` until that settle barrier is defined correctly

### 4. Post-navigation extractors

Move to `on_SnapshotChromeTabNavigated__...`:

- `abx_plugins/plugins/seo/on_Snapshot__38_seo.js`
- `abx_plugins/plugins/accessibility/on_Snapshot__39_accessibility.js`
- `abx_plugins/plugins/infiniscroll/on_Snapshot__45_infiniscroll.js`
- `abx_plugins/plugins/claudechrome/on_Snapshot__47_claudechrome.js`
- `abx_plugins/plugins/singlefile/on_Snapshot__50_singlefile.py`
- `abx_plugins/plugins/singlefile/singlefile_extension_save.js`
- `abx_plugins/plugins/screenshot/on_Snapshot__51_screenshot.js`
- `abx_plugins/plugins/pdf/on_Snapshot__52_pdf.js`
- `abx_plugins/plugins/dom/on_Snapshot__53_dom.js`
- `abx_plugins/plugins/title/on_Snapshot__54_title.js`
- `abx_plugins/plugins/parse_dom_outlinks/on_Snapshot__75_parse_dom_outlinks.js`

### 5. Late settled-stage hooks

Move to `on_AfterSnapshot__...` if they should run after all snapshot outputs exist and are actually intended to consume generated snapshot artifacts rather than the original input URL directly:

- `abx_plugins/plugins/readability/on_Snapshot__56_readability.py`
- `abx_plugins/plugins/mercury/on_Snapshot__57_mercury.py`
- `abx_plugins/plugins/defuddle/on_Snapshot__57_defuddle.py`
- `abx_plugins/plugins/htmltotext/on_Snapshot__58_htmltotext.py`
- `abx_plugins/plugins/claudecodeextract/on_Snapshot__58_claudecodeextract.py`
- `abx_plugins/plugins/trafilatura/on_Snapshot__59_trafilatura.py`
- `abx_plugins/plugins/opendataloader/on_Snapshot__60_opendataloader.py`
- `abx_plugins/plugins/liteparse/on_Snapshot__61_liteparse.py`
- `abx_plugins/plugins/parse_html_urls/on_Snapshot__70_parse_html_urls.py`
- `abx_plugins/plugins/search_backend_sqlite/on_Snapshot__90_index_sqlite.py`
- `abx_plugins/plugins/search_backend_sonic/on_Snapshot__91_index_sonic.py`
- `abx_plugins/plugins/claudecodecleanup/on_Snapshot__92_claudecodecleanup.py`
- `abx_plugins/plugins/hashes/on_Snapshot__93_hashes.py`

Review needed for:

- `abx_plugins/plugins/parse_txt_urls/on_Snapshot__71_parse_txt_urls.py`
- `abx_plugins/plugins/parse_rss_urls/on_Snapshot__72_parse_rss_urls.py`
- `abx_plugins/plugins/parse_netscape_urls/on_Snapshot__73_parse_netscape_urls.py`
- `abx_plugins/plugins/parse_jsonl_urls/on_Snapshot__74_parse_jsonl_urls.py`

These currently parse their direct input source, not sibling generated outputs, so moving them to `AfterSnapshot` is not just a scheduling change. They must either:

- stay on `Snapshot` as direct-source parsers

or:

- be rewritten to scan generated snapshot outputs first, then moved to `AfterSnapshot`

### 6. Browser-wide extension config hooks

- `abx_plugins/plugins/twocaptcha/on_CrawlSetup__95_twocaptcha_config.js`
- `abx_plugins/plugins/claudechrome/on_CrawlSetup__96_claudechrome_config.js`

Use:

- `CrawlChromeExtensionsReady` in crawl isolation
- `SnapshotChromeExtensionsReady` in snapshot isolation via thin wrappers if needed

Do not use `.configured` marker files as orchestration state.

## Marker Files / File-Based Protocols To Remove

### Delete entirely

- `abx_plugins/plugins/redirects/prenav.json`
- `CRAWL_DIR/chrome/.twocaptcha_configured`
- `CRAWL_DIR/chrome/.claudechrome_configured`
- `SNAP_DIR/chrome/url.txt`

### Remove from the runtime contract

These may remain as optional debug/manual-run artifacts, but must not be required for orchestration:

- `CRAWL_DIR/chrome/cdp_url.txt`
- `CRAWL_DIR/chrome/chrome.pid`
- `CRAWL_DIR/chrome/extensions.json`
- `SNAP_DIR/chrome/cdp_url.txt`
- `SNAP_DIR/chrome/target_id.txt`
- `SNAP_DIR/chrome/navigation.json`
- `SNAP_DIR/chrome/extensions.json`

Replace with event payload fields plus reduced `env` state on:

- `CrawlChromeBrowserReady`
- `CrawlChromeExtensionsReady`
- `SnapshotChromeBrowserReady`
- `SnapshotChromeExtensionsReady`
- `SnapshotChromeTabReady`
- `SnapshotChromeTabNavigated`
- `SnapshotChromeTabNavigationFailed`

### Keep artifact files, remove marker semantics

- `abx_plugins/plugins/consolelog/console.jsonl`
- `abx_plugins/plugins/headers/headers.json`
- `abx_plugins/plugins/responses/index.jsonl`
- `abx_plugins/plugins/dns/dns.jsonl`

These remain useful outputs, but file existence must not mean “ready”.

### Remove cross-plugin log scraping

Remove uses of sibling `stdout.log` scraping such as `hasStaticFileOutput(...)`.

Replace with a real fact like:

- `SnapshotChromeStaticFileHandled`

and/or reduced state keys like:

- `STATICFILE_HANDLED`
- `STATICFILE_PATH`

## Shared Helper Simplification

### `abx_plugins/plugins/base/utils.js`
### `abx_plugins/plugins/base/utils.py`

Add one small generic helper on both sides:

- `emit_event_record("EventName", {...})`

That helper should support:

- normal payload fields
- optional reserved `env` patch

### `abx_plugins/plugins/chrome/chrome_utils.js`

Shrink or split helpers that currently encode file-based orchestration:

- `waitForChromeSessionState(...)`
- `connectToPage(...)`
- `waitForNavigationComplete(...)`

Helpers should remain useful for direct/manual invocation, but they should stop being the control plane.

### `staticfile`

`abx_plugins/plugins/staticfile/on_Snapshot__26_staticfile.daemon.bg.js` should stop polling `responses/index.jsonl`.

This is likely a deeper refactor than a simple resubscribe:

- today `staticfile` starts in the pre-navigation stage but later waits for `responses` output
- in the new model it should either be split into a pre-navigation detector and a later saver, or otherwise rewritten so it consumes a real later event cleanly

If `responses` needs to fan out to `staticfile`, use a real event such as:

- `SnapshotChromeMainResponseSaved`

## Tests To Rewrite

### Stop asserting numeric ordering as dependency semantics

- `abx_plugins/plugins/singlefile/tests/test_singlefile.py`
- `abx_plugins/plugins/claudechrome/tests/test_claudechrome.py`
- `abx_plugins/plugins/claudecodecleanup/tests/test_claudecodecleanup.py`

### Stop asserting readiness via file appearance

- `abx_plugins/plugins/redirects/tests/test_redirects.py`
- `abx_plugins/plugins/headers/tests/test_headers.py`
- `abx_plugins/plugins/consolelog/tests/test_consolelog.py`
- `abx_plugins/plugins/staticfile/tests/test_staticfile.py`
- `abx_plugins/plugins/chrome/tests/chrome_test_helpers.py`
- `abx_plugins/plugins/chrome/tests/test_chrome.py`

Prefer asserting:

- emitted events
- reduced `derived.env` state where useful
- resulting outputs
- final user-visible behavior

## Docs To Rewrite

- `abx_plugins/plugins/chrome/README.md`

Remove “authoritative marker file” language for:

- `cdp_url.txt`
- `target_id.txt`
- `navigation.json`

Document:

- event-driven control plane
- synthetic `After<Event>` settle barriers
- reduced context mirrored to `derived.env`
- files as outputs/debug aids only

## Migration Order

1. Update `abx-dl` for dynamic `Crawl*` / `Snapshot*` dispatch and synthetic `After<Event>` emission.
2. Add scoped context reduction and `derived.env` mirroring.
3. Pass full reduced context via env vars and current event payload via CLI args.
4. Add generic event emit helpers in JS and Python.
5. Expand fallback `ArchiveResult` synthesis so `on_AfterSnapshot__...` hooks behave like `on_Snapshot__...` hooks.
6. Delete `chrome_wait` barrier hooks.
7. Move Chrome launch/tab onto real event emission.
8. Define the bg pre-navigation settle semantics needed before moving `chrome_navigate` to `AfterSnapshotChromeTabReady`.
9. Move post-navigation extractors to `SnapshotChromeTabNavigated`.
10. Move clearly artifact-consuming late hooks to `AfterSnapshot`.
11. Rewrite or keep direct-source URL parsers based on their actual intended inputs.
12. Remove file markers and sibling log polling.
13. Rewrite tests and docs around event semantics and `derived.env`.

## Short Version

The final design is:

1. one hook syntax: `on_<Event>__...`
2. hooks emit real facts on stdout
3. hooks may include reserved `env` patches for durable aggregate state
4. `abx-dl` routes facts generically
5. `abx-dl` auto-emits `After<Event>` when an event subtree settles
6. `abx-dl` reduces prior scoped events into current key/value context
7. `abx-dl` mirrors crawl context to `CRAWL_DIR/derived.env` and snapshot overlays to `SNAP_DIR/derived.env` only
8. hooks receive reduced context as env vars and current event payload as CLI args
9. `SnapshotCleanup` waits for the full root `Snapshot` tree

That preserves the self-healing “read fresh current state” behavior from the old file-based system without keeping the fractured marker-file protocol.

## Repo Summary

### `abx-dl` changes

- make root hook-driving lifecycle events use stable string names like `CrawlSetup` and `Snapshot`
- dispatch crawl-tree and snapshot-tree hooks dynamically by event string
- route hook-emitted facts generically onto `abxbus`, including synthetic `After<Event>` barriers
- reduce prior scoped events into current crawl/snapshot context
- mirror crawl context to `CRAWL_DIR/derived.env`
- mirror per-snapshot overlays to `SNAP_DIR/derived.env` without writing snapshot state back to crawl scope
- launch hooks with full reduced context as env vars and current event payload as CLI args
- expand fallback `ArchiveResult` synthesis so `on_AfterSnapshot__...` hooks work the same as `on_Snapshot__...` hooks
- keep `SnapshotCleanup` waiting for the full root `Snapshot` tree

### `abx-plugins` changes

- add generic `emit_event_record(...)` helpers in JS and Python with reserved `env` patch support
- migrate Chrome launch/tab/navigate to emit real events plus durable env state
- move pre-navigation listeners onto `SnapshotChromeTabReady`
- move post-navigation extractors onto `SnapshotChromeTabNavigated`
- move clearly artifact-consuming late hooks onto `AfterSnapshot`
- review direct-source parsers before moving them late
- remove marker-file protocols like `prenav.json` and `.configured`
- remove sibling `stdout.log` scraping and replace it with real facts / reduced env state
- stop using file existence as readiness
- rewrite tests and docs around emitted events and `derived.env`

### `archivebox` changes

- consume `UrlDiscovered` instead of relying on hooks emitting child `Snapshot` records directly
- decide crawl policy for materializing new snapshots from `UrlDiscovered`
- ensure ArchiveBox’s parallel snapshot execution continues to treat crawl context as upstream-only and snapshot context as local overlay
- update any integration tests or docs that assume marker-file-driven Chrome coordination or old `Snapshot` discovery records

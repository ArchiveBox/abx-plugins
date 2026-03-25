# chrome

Launches or adopts a Chromium/CDP browser session and publishes a stable session contract for all browser-backed extractors.

The important rule is:

- browser readiness is defined by the published `chrome/` session markers
- browser liveness is verified through CDP
- `chrome.pid` is only ownership/cleanup metadata for local processes

This plugin now supports two execution modes:

- `CHROME_ISOLATION=crawl`
  - one browser session per crawl
  - each snapshot gets its own tab in that browser
- `CHROME_ISOLATION=snapshot`
  - one browser session per snapshot
  - the snapshot owns the browser directly

It also supports two session sources:

- launch a local Chromium process
- adopt an existing browser via `CHROME_CDP_URL`

## Why This Exists

Most browser extractors only need:

- a connectable CDP endpoint
- a concrete page target
- sometimes loaded extensions
- sometimes a configured downloads directory

They should not need to know:

- whether the browser is local or remote
- whether the browser was launched by the Chrome plugin or provided externally
- whether the browser is crawl-scoped or snapshot-scoped

The Chrome plugin publishes those details behind a shared `chrome/` session directory and `chrome_utils.js`.

## Config

Defined in [config.json](./config.json).

### Core session options

| Variable | Default | Meaning |
|---|---|---|
| `CHROME_ENABLED` | `true` | Enable browser-backed archiving. |
| `CHROME_BINARY` | `chromium` | Local Chromium binary to launch when not adopting an existing browser. |
| `CHROME_CDP_URL` | `""` | Adopt an already-running browser instead of launching a local one. Accepts WS or HTTP CDP endpoints. |
| `CHROME_IS_LOCAL` | `true` | Whether the owned browser process is local and should publish `chrome.pid`. If `CHROME_CDP_URL` is set, runtime behavior is external/non-local. |
| `CHROME_KEEPALIVE` | `false` | Whether the owning launch hook should exit immediately and leave the browser running, instead of staying alive and closing it during cleanup. |
| `CHROME_ISOLATION` | `crawl` | `crawl` for one browser per crawl, `snapshot` for one browser per snapshot. |

### Runtime browser options

| Variable | Default | Meaning |
|---|---|---|
| `CHROME_TIMEOUT` | `60` | General Chrome operation timeout in seconds. |
| `CHROME_PAGELOAD_TIMEOUT` | `60` | Navigation/page-load timeout in seconds. |
| `CHROME_WAIT_FOR` | `networkidle2` | Puppeteer navigation completion condition. |
| `CHROME_DELAY_AFTER_LOAD` | `0` | Extra delay after page load completes. |
| `CHROME_HEADLESS` | `true` | Run headless. |
| `CHROME_SANDBOX` | `true` | Enable Chromium sandbox. |
| `CHROME_RESOLUTION` | `1440,2000` | Viewport/window size. |
| `CHROME_USER_AGENT` | `""` | Optional browser user agent override. |
| `CHROME_CHECK_SSL_VALIDITY` | `true` | Whether to reject invalid TLS certs. |

### Profile / extension / download options

| Variable | Default | Meaning |
|---|---|---|
| `CHROME_USER_DATA_DIR` | `""` | User data dir for persistent local profile state. |
| `CHROME_EXTENSIONS_DIR` | persona-derived | Extension cache directory. |
| `CHROME_DOWNLOADS_DIR` | persona-derived | Download output directory configured via CDP after launch/adoption. |
| `CHROME_ARGS` | see config | Static Chromium flags. |
| `CHROME_ARGS_EXTRA` | `[]` | Final extra flags appended at launch. |

## Session Modes

### `CHROME_ISOLATION=crawl`

Ownership:

- [on_CrawlSetup__90_chrome_launch.daemon.bg.js](./on_CrawlSetup__90_chrome_launch.daemon.bg.js) owns the browser session
- [on_CrawlSetup__91_chrome_wait.js](./on_CrawlSetup__91_chrome_wait.js) verifies the crawl-scoped session is connectable
- [on_Snapshot__10_chrome_tab.daemon.bg.js](./on_Snapshot__10_chrome_tab.daemon.bg.js) creates one page/tab per snapshot

Contract:

- `CRAWL_DIR/chrome/` publishes the browser session
- `SNAP_DIR/chrome/` publishes the tab/session for one snapshot inside that browser

### `CHROME_ISOLATION=snapshot`

Ownership:

- [on_Snapshot__09_chrome_launch.daemon.bg.js](./on_Snapshot__09_chrome_launch.daemon.bg.js) owns the browser session for that snapshot
- [on_Snapshot__10_chrome_tab.daemon.bg.js](./on_Snapshot__10_chrome_tab.daemon.bg.js) adopts or verifies the already-published snapshot session

Contract:

- `SNAP_DIR/chrome/` is the only browser/session surface needed by downstream snapshot hooks
- there may be no crawl-scoped shared browser at all

## Local vs External Browsers

### Local launch

When `CHROME_CDP_URL` is unset, `ensureChromeSession(...)` launches local Chromium, waits until:

- the browser endpoint is connectable
- an `about:blank` page exists
- CDP can attach to that page and read its title

Only after that does it publish `cdp_url.txt`.

### External adoption

When `CHROME_CDP_URL` is set, `ensureChromeSession(...)` adopts that browser instead of launching one.

Important behavior:

- `CHROME_CDP_URL` may be a WS or HTTP CDP endpoint
- `chrome.pid` is not required for external sessions
- readiness and liveness are based on CDP, not PID
- repeated launch/adoption against the same `chrome/` dir and same live `CHROME_CDP_URL` reuses the session in place instead of closing it

## Keepalive and Cleanup

### `CHROME_KEEPALIVE=false`

The owning launch hook stays alive and closes the browser on `SIGTERM`:

- first tries `Browser.close()` over CDP
- if the process is local and still alive after timeout, escalates to `SIGTERM`/`SIGKILL`

Cleanup scope depends on isolation:

- `crawl` isolation: crawl launch hook owns browser shutdown
- `snapshot` isolation: snapshot launch hook owns browser shutdown

### `CHROME_KEEPALIVE=true`

The owning launch hook exits immediately and leaves the browser running.

This is useful when:

- an upstream provider manages lifecycle
- tests want to reattach repeatedly
- a caller wants to close the browser explicitly later

## Hook Order

### Crawl hooks

| Hook | Priority | Purpose |
|---|---:|---|
| `on_CrawlSetup__90_chrome_launch.daemon.bg.js` | 90 | Launch/adopt crawl-scoped browser when `CHROME_ISOLATION=crawl`. No-op when `snapshot`. |
| `on_CrawlSetup__91_chrome_wait.js` | 91 | Wait for a connectable crawl session when `crawl`. Reports `"snapshot isolation active"` when `snapshot`. |

Chromium and extension dependencies are resolved before crawl setup from
`config.json > required_binaries` during orchestrator preflight.

### Snapshot hooks

| Hook | Priority | Purpose |
|---|---:|---|
| `on_Snapshot__09_chrome_launch.daemon.bg.js` | 9 | Launch/adopt snapshot-scoped browser when `CHROME_ISOLATION=snapshot`. No-op readiness check when `crawl`. |
| `on_Snapshot__10_chrome_tab.daemon.bg.js` | 10 | Create or adopt the snapshot page target. |
| `on_Snapshot__11_chrome_wait.js` | 11 | Verify snapshot `cdp_url.txt` + `target_id.txt` point at a live target. |
| `on_Snapshot__30_chrome_navigate.js` | 30 | Navigate the snapshot page and publish navigation markers. |

## Directory Layout

### Crawl-scoped session

`CRAWL_DIR/chrome/`

Used when:

- `CHROME_ISOLATION=crawl`
- or crawl-scoped extension config hooks need a browser-wide session

### Snapshot-scoped session

`SNAP_DIR/chrome/`

Always the main contract for snapshot extractors.

Downstream snapshot hooks should consume:

- `SNAP_DIR/chrome/cdp_url.txt`
- `SNAP_DIR/chrome/target_id.txt`
- optional `SNAP_DIR/chrome/extensions.json`
- navigation markers written later by `chrome_navigate`

They should not reach back into `CRAWL_DIR/chrome/` directly unless they are intentionally crawl-scoped browser hooks.

## Artifact Contract

### Common session markers

| File | Meaning |
|---|---|
| `cdp_url.txt` | Authoritative browser readiness marker. |
| `chrome.pid` | Local-process ownership metadata. Optional for external sessions. |
| `extensions.json` | Loaded extension metadata. Optional if no extensions are present. |

### Snapshot-only markers

| File | Meaning |
|---|---|
| `target_id.txt` | Authoritative page-target marker for the snapshot. |
| `url.txt` | Requested URL used for snapshot reuse checks. |
| `navigation.json` | Structured navigation result, including errors. |

### Readiness rules

- `cdp_url.txt` means the browser is safe to connect to
- `target_id.txt` means a specific page target exists for this snapshot
- `navigation.json` means `chrome_navigate` completed, and success vs failure is encoded inside the JSON
- `chrome.pid` does not imply readiness

## `chrome_utils.js` Helpers

The shared helpers live in [chrome_utils.js](./chrome_utils.js).

### Main helpers

#### `ensureChromeSession(options)`

Launches or adopts a browser session and publishes the session markers.

Used by:

- crawl launch hook
- snapshot launch hook

Responsibilities:

- reuse healthy existing sessions when appropriate
- avoid destroying an explicit live `CHROME_CDP_URL` session when re-invoked against the same directory
- configure downloads over CDP
- import cookies if configured
- publish `extensions.json` before `cdp_url.txt`
- publish `chrome.pid` only for local sessions

#### `closeBrowserInChromeSession(options)`

Closes the owned browser session.

Behavior:

- send `Browser.close()` over CDP first
- for local processes, escalate to `SIGTERM`/`SIGKILL` if needed
- clean up stale session markers afterward

#### `waitForChromeSessionState(chromeSessionDir, options)`

Wait for a published session directory to be ready.

Returns:

- `null` on timeout
- otherwise a normalized state object:
  - `sessionDir`
  - `cdpUrl`
  - `targetId`
  - `pid`
  - `extensions`

Important behavior:

- does not require `chrome.pid` by default
- can require `target_id.txt` with `requireTargetId: true`
- can require parseable `extensions.json` with `requireExtensionsLoaded: true`
- is purely marker-based readiness, not browser-connection liveness

#### `connectToBrowserEndpoint(puppeteer, cdpUrl, connectOptions)`

Connects Puppeteer to a browser endpoint.

Handles both:

- `ws://.../devtools/browser/...` via `browserWSEndpoint`
- `http://host:port` via `browserURL`

Use this for browser-scoped hooks that need a browser but not a specific page target.

#### `connectToPage(options)`

High-level helper for snapshot page consumers.

Options include:

- `chromeSessionDir`
- `timeoutMs`
- `requireTargetId`
- `requireExtensionsLoaded`
- `waitForNavigationComplete`
- `pageLoadTimeoutMs`
- `postLoadDelayMs`
- `puppeteer`

Returns:

- the session state from `waitForChromeSessionState(...)`
- plus:
  - `browser`
  - `page`
  - `cdpSession`
  - `targetId`

Important behavior:

- fails fast if there is no Chrome session at all
- if `waitForNavigationComplete: true`, waits for successful `navigation.json` before attaching
- creates a page CDP session for the returned page
- sends initial `Target.setAutoAttach({ autoAttach: true, waitForDebuggerOnStart: false, flatten: true })`

This is the preferred helper for almost all snapshot extractors.

#### `waitForNavigationComplete(chromeSessionDir, timeoutMs, postLoadDelayMs)`

Waits for `navigation.json` from `chrome_navigate`, parses it, and throws if navigation failed.

This is marker-based, not a live CDP page-state poll.

#### `inspectChromeSessionArtifacts(chromeSessionDir, options)`

Low-level session inspection used mainly by core Chrome hooks for reuse/stale cleanup decisions.

Most downstream plugins should not need this directly.

#### `getBrowserCdpUrl(chromeSessionDir)`

Derives the SingleFile-style browser server URL from a published session directory.

#### `openTabInChromeSession(...)` / `closeTabInChromeSession(...)`

Core tab lifecycle helpers used by the Chrome hooks.

## Readiness Lifecycle

### Crawl isolation

1. Extension installer hooks populate the extension cache.
2. Crawl launch hook calls `ensureChromeSession(...)`.
3. Browser-wide setup completes.
4. `cdp_url.txt` is published in `CRAWL_DIR/chrome/`.
5. Crawl wait verifies a real CDP connection.
6. Snapshot tab hook creates a target and publishes `SNAP_DIR/chrome/target_id.txt`.
7. Snapshot wait verifies the target is live.
8. Navigate hook writes `navigation.json`.
9. Later extractors call `connectToPage(...)`.

### Snapshot isolation

1. Snapshot launch hook calls `ensureChromeSession(...)` in `SNAP_DIR/chrome/`.
2. `cdp_url.txt` is published in `SNAP_DIR/chrome/`.
3. Snapshot tab hook adopts or creates the snapshot page target.
4. Snapshot wait verifies the target is live.
5. Navigate hook writes the navigation markers.
6. Later extractors call `connectToPage(...)`.

## Practical Rules For Other Plugins

If you are writing a Chrome-dependent plugin:

- use `connectToPage(...)` for snapshot/page extractors
- use `connectToBrowserEndpoint(...)` only for crawl-scoped browser hooks that do not need a page target
- use `waitForChromeSessionState(..., { requireExtensionsLoaded: true })` only when you truly need extension metadata before page attachment
- treat `cdp_url.txt` as the browser readiness gate
- treat `target_id.txt` as the page readiness gate
- treat `navigation.json` as the post-navigation readiness gate

Anti-patterns:

- do not require local Chrome unless absolutely necessary
- do not use `chrome.pid` as a readiness check
- do not read raw session files directly when `connectToPage(...)` or `waitForChromeSessionState(...)` already provide the contract
- do not hardcode provider-specific behavior in downstream plugins
- do not configure downloads by mutating Chromium profile preferences; use CDP/browser setup

## Extensions and Downloads

Chrome itself does not know about specific extension plugins.

Extension flow:

- installer hooks populate extension cache metadata
- `ensureChromeSession(...)` loads or discovers those extensions into the active browser session
- `extensions.json` publishes the loaded metadata
- downstream extension-aware hooks consume the published `extensions` metadata

Download flow:

- browser/session setup configures downloads through CDP
- downstream hooks should consume the resulting files from the published downloads directory

## Current Model Summary

What downstream plugins can rely on:

- there is a published `chrome/` session directory
- `waitForChromeSessionState(...)` returns normalized session metadata
- `connectToPage(...)` gives them a live page plus connected page CDP session
- the browser may be local or external
- the browser may be crawl-scoped or snapshot-scoped

What they should not care about:

- how the browser was launched
- who owns process lifecycle
- whether `chrome.pid` exists

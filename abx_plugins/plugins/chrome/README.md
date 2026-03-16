# chrome

Launches and manages a shared Chromium session for an entire crawl, then gives each snapshot its own tab inside that browser.

This plugin is the readiness backbone for every browser-driven extractor. The important contract is that downstream hooks should treat `cdp_url.txt` as the "Chrome is safe to use now" marker, not merely "a process exists".

## Why This Exists

- ArchiveBox wants one browser per crawl, not one browser per snapshot.
- Snapshot hooks still need isolated tabs so they can navigate independently.
- Browser-backed hooks such as `singlefile`, `screenshot`, `pdf`, `title`, `seo`, and similar extractors need a stable CDP session and a concrete target/page to attach to.
- Extension-backed hooks also need extension metadata to be ready before they connect.

That leads to a two-level lifecycle:

1. Crawl-level browser readiness in `CRAWL_DIR/chrome/`
2. Snapshot-level tab readiness in `SNAP_DIR/chrome/`

## Install And Config Resolution

This README does not duplicate the `puppeteer` plugin docs, but the Chrome lifecycle depends on that install chain being understood:

1. `puppeteer` plugin:
   - `on_Crawl__60_puppeteer_install.py` emits the npm dependency for `puppeteer`
   - every Chrome JS hook uses `require('puppeteer')`, so this must already be installed
2. `chrome` plugin:
   - `on_Crawl__70_chrome_install.finite.bg.py` emits a `Binary` record for `chromium`/`chrome` via the `puppeteer` binprovider
   - `on_Crawl__90_chrome_launch.daemon.bg.js` assumes both the JS package and browser binary are already resolvable

Runtime/config resolution is:

- `NODE_MODULES_DIR` / `NODE_MODULE_DIR`
  - every JS entrypoint calls `ensureNodeModuleResolution(module)`
  - priority is explicit env var first, otherwise `LIB_DIR/npm/node_modules`
  - `ensureNodeModuleResolution(...)` also backfills `NODE_PATH` and amends `module.paths`
- `CHROME_BINARY`
  - a valid explicit `CHROME_BINARY` wins first
  - if unset or invalid, `findChromium()` falls back to:
    - hook-installed Chromium under `LIB_DIR`
    - Puppeteer cache locations
    - system Chromium locations
  - the fallback is intentionally Chromium-oriented, not "whatever branded Chrome app happens to exist"
- `CHROME_USER_DATA_DIR`
  - if set, launch passes it as `--user-data-dir`
  - if not set, higher-level config may derive it from the active persona
  - persistent cookies, extension state, and profile-scoped browser state belong here
- `CHROME_EXTENSIONS_DIR`
  - explicit override wins
  - otherwise defaults to `PERSONAS_DIR/ACTIVE_PERSONA/chrome_extensions`
- `CHROME_DOWNLOADS_DIR`
  - if set, download behavior is configured after launch over CDP
  - this is runtime browser setup, not pre-launch profile tampering
- `CHROME_ARGS` vs `CHROME_ARGS_EXTRA`
  - `CHROME_ARGS` is for static default flags
  - dynamic flags such as `--remote-debugging-port`, `--window-size`, `--user-data-dir`, `--headless=new`, and extension load args are appended at runtime
  - `CHROME_ARGS_EXTRA` is the last-mile override hook when you really need to append additional flags

## Hook Order

Prerequisite outside this plugin:

- `on_Crawl__60_puppeteer_install.py` from the `puppeteer` plugin should run before these JS hooks so `require('puppeteer')` resolves without relying on global npm state

### Crawl-level

| Hook | Priority | Type | Purpose |
|---|---:|---|---|
| `on_Crawl__70_chrome_install.finite.bg.py` | 70 | background finite | Emits the `chrome` binary dependency. |
| `on_Crawl__80_*_install*.js` / `on_Crawl__82_singlefile_install*.js` | 80-82 | finite bg | Install/cache Chrome extensions before launch. |
| `on_Crawl__90_chrome_launch.daemon.bg.js` | 90 | daemon bg | Launch the shared Chromium process and keep it alive for the crawl. |
| `on_Crawl__91_chrome_wait.js` | 91 | foreground | Block until the crawl-level Chrome session is actually connectable. |

### Snapshot-level

| Hook | Priority | Type | Purpose |
|---|---:|---|---|
| `on_Snapshot__10_chrome_tab.daemon.bg.js` | 10 | daemon bg | Create one tab for this snapshot inside the shared crawl browser. |
| `on_Snapshot__11_chrome_wait.js` | 11 | foreground | Block until the snapshot tab is connectable by target ID. |
| `on_Snapshot__30_chrome_navigate.js` | 30 | foreground | Navigate that tab to the snapshot URL and write post-navigation markers. |
| later hooks (`singlefile`, `screenshot`, `pdf`, `title`, etc.) | 50+ | mixed | Reuse the same snapshot tab via CDP. |

## Directory Layout

### Crawl scope

`CRAWL_DIR/chrome/`

This directory owns the shared browser process and the browser-wide readiness artifacts.

### Snapshot scope

`SNAP_DIR/chrome/`

This directory does **not** own a separate browser. It stores the per-snapshot tab state for one page inside the shared crawl browser.

The snapshot-level `cdp_url.txt` and `chrome.pid` are copies of the crawl session values. The snapshot-level `target_id.txt` is unique per snapshot.

## Readiness Lifecycle

### 1. Extension install hooks run before Chrome launch

Before Chromium launches, any crawl-level extension install hooks write extension cache metadata under the persona's `chrome_extensions/` directory.

That cache is what `on_Crawl__90_chrome_launch.daemon.bg.js` uses to decide which unpacked extensions to load at startup.

### 2. Crawl launch starts Chromium, but does not publish readiness immediately

`on_Crawl__90_chrome_launch.daemon.bg.js` does several things in order:

1. Acquires `.launch.lock` in `CRAWL_DIR/chrome/`
2. Reuses a healthy existing session if one already exists
3. Cleans stale session artifacts if they point at a dead browser
4. Writes `cmd.sh`
5. Spawns Chromium
6. Writes `chrome.pid` as soon as the browser process exists
7. Waits for the DevTools debug port to answer
8. Verifies the session is stable enough not to die during early startup
9. Discovers loaded extension targets
10. Configures browser-wide settings over CDP:
    - download directory
    - optional cookie import
11. Writes `extensions.json`
12. Writes `cdp_url.txt`
13. Writes `port.txt`

The crucial nuance is step 11 before step 12:

- `cdp_url.txt` is intentionally **not** published as soon as `/json/version` comes up
- it is only written after extension discovery and browser-scoped setup are complete
- downstream hooks use `cdp_url.txt` as the readiness gate
- the early stability check must stay *before* extension discovery and must *not* wait for `extensions.json`, because `extensions.json` is only written later by the crawl launch hook itself

If `cdp_url.txt` were written earlier, snapshot hooks could attach while extensions were still initializing, which is exactly the race this lifecycle is avoiding.

### 3. Crawl wait consumes only the crawl-level session markers

`on_Crawl__91_chrome_wait.js` waits on `CRAWL_DIR/chrome/` and requires:

- `cdp_url.txt`
- `chrome.pid`
- the PID to still be alive
- a real successful Puppeteer CDP connection

It does **not** create any new files. It only verifies that the published crawl session is truly usable.

### 4. Snapshot tab creation waits for crawl readiness, then publishes per-tab markers

`on_Snapshot__10_chrome_tab.daemon.bg.js` is the first snapshot hook that should touch Chrome.

It:

1. Acquires `.target.lock` in `SNAP_DIR/chrome/`
2. Waits for the crawl session using `waitForCrawlChromeSession(...)`
3. Reuses an existing live tab if the snapshot markers already point at a valid target for the same URL
4. Otherwise opens a new `about:blank` page target in the shared crawl browser
5. Writes snapshot-level markers:
   - `cdp_url.txt`
   - `chrome.pid`
   - `target_id.txt`
   - `url.txt`
6. Copies `extensions.json` from the crawl session if crawl launch created it
7. Emits success immediately
8. Stays alive until `SIGTERM` so it can close the tab cleanly

Important details:

- snapshot `cdp_url.txt` and `chrome.pid` are shared-session pointers, not new resources
- snapshot `target_id.txt` is the real readiness gate for "this snapshot has a tab"
- `extensions.json` at snapshot scope is a copy of crawl metadata, not a second discovery pass
- if crawl launch did not create `extensions.json`, snapshot tab creation does **not** synthesize one
- snapshot tab creation can succeed before any extension-specific consumer has read the copied metadata, so extension-backed hooks that need IDs should still wait for snapshot `extensions.json` (or explicitly consume crawl metadata)

### 5. Snapshot wait consumes tab-level readiness

`on_Snapshot__11_chrome_wait.js` waits on `SNAP_DIR/chrome/` and requires:

- `cdp_url.txt`
- `target_id.txt`
- a successful CDP connection to the browser
- the connected page's target ID to match `target_id.txt`

This is stronger than just waiting for files to exist. It verifies that the target is still alive and that the target ID on disk still matches the live page.

### 6. Navigation writes the post-load markers

`on_Snapshot__30_chrome_navigate.js` attaches to the snapshot tab using the snapshot-level `cdp_url.txt` + `target_id.txt` pair.

On success it writes:

- `navigation.json`
- `page_loaded.txt`
- `final_url.txt`

On failure it writes:

- `navigation.json` only, with an `error` field

Nuances:

- `navigation.json` is the structured navigation record
- `page_loaded.txt` is the legacy/backwards-compat marker that something finished loading
- `final_url.txt` is only written on successful navigation
- snapshot tab creation intentionally does **not** create any of these files

### 7. Later browser-backed hooks reuse the same snapshot tab

Hooks such as `singlefile`, `screenshot`, `pdf`, and `title` attach to the existing snapshot tab instead of launching their own Chrome process.

In practice they rely on:

- `SNAP_DIR/chrome/cdp_url.txt`
- `SNAP_DIR/chrome/target_id.txt`
- hook ordering after `chrome_wait` and usually after `chrome_navigate`

They do not need a second browser launch because `connectToPage(...)` resolves the live page from those markers.

## Artifact Contract

### Crawl-level artifacts in `CRAWL_DIR/chrome/`

| File | Writer | First valid when | Consumed by | Notes |
|---|---|---|---|---|
| `cmd.sh` | `on_Crawl__90_chrome_launch.daemon.bg.js` | immediately before spawn | humans/debugging | Re-run/debug command line, not a readiness marker. |
| `chrome.pid` | `on_Crawl__90_chrome_launch.daemon.bg.js` | immediately after Chromium spawn | crawl wait, snapshot tab, stale-session cleanup | Exists before full readiness. PID alone is not enough. |
| `extensions.json` | `on_Crawl__90_chrome_launch.daemon.bg.js` | after extension discovery and browser setup | snapshot tab, SingleFile helper, debug tooling | This must exist before `cdp_url.txt` is published when extensions are in play. |
| `cdp_url.txt` | `on_Crawl__90_chrome_launch.daemon.bg.js` | after Chrome is fully safe for downstream hooks | crawl wait, snapshot tab, any direct crawl-scoped tooling | This is the main crawl-level readiness gate. |
| `port.txt` | `on_Crawl__90_chrome_launch.daemon.bg.js` | immediately after `cdp_url.txt` | debug tooling/tests | Convenience derivative of `cdp_url.txt`. |

### Snapshot-level artifacts in `SNAP_DIR/chrome/`

| File | Writer | First valid when | Consumed by | Notes |
|---|---|---|---|---|
| `cdp_url.txt` | `on_Snapshot__10_chrome_tab.daemon.bg.js` | after the snapshot tab is created | snapshot wait, navigate, SingleFile helper, later browser hooks | Same browser endpoint as crawl-level `cdp_url.txt`. |
| `chrome.pid` | `on_Snapshot__10_chrome_tab.daemon.bg.js` | after the snapshot tab is created | cleanup/debugging | Same browser PID as the crawl-level session. |
| `target_id.txt` | `on_Snapshot__10_chrome_tab.daemon.bg.js` | after a unique tab target exists | snapshot wait, navigate, later browser hooks | The real per-snapshot readiness marker. |
| `url.txt` | `on_Snapshot__10_chrome_tab.daemon.bg.js` | after tab creation | reuse checks/debugging | Used to reject reusing a live tab for the wrong URL. |
| `extensions.json` | `on_Snapshot__10_chrome_tab.daemon.bg.js` | after crawl metadata copy succeeds | SingleFile helper and any extension-backed snapshot hooks | Optional copy of crawl metadata. |
| `navigation.json` | `on_Snapshot__30_chrome_navigate.js` | after navigation finishes or fails | debugging, post-load state inspection | Always the authoritative navigation record. |
| `page_loaded.txt` | `on_Snapshot__30_chrome_navigate.js` | only after successful navigation | legacy consumers/debugging | Not written on navigation failure. |
| `final_url.txt` | `on_Snapshot__30_chrome_navigate.js` | only after successful navigation | later hooks/debugging | Records redirects/canonical final URL. |

## SingleFile-Specific Nuance

`singlefile` consumes the Chrome lifecycle in two stages:

1. `on_Snapshot__50_singlefile.py`
   - delegates shared-session resolution to `chrome_utils.js`
   - for the CLI path, resolves the browser-scoped `--browser-server` URL from the published snapshot session markers
   - for the extension path, launches the helper only after the normal snapshot Chrome lifecycle has already published the tab markers

2. `singlefile_extension_save.js`
   - treats `../chrome/` as the snapshot Chrome session directory
   - uses `connectToPage(...)`, which requires the snapshot `target_id.txt`
   - waits for `../chrome/extensions.json`
   - resolves the loaded SingleFile extension ID from that metadata
   - connects to the already-open snapshot tab and triggers the extension there

So for the extension-backed path, SingleFile needs all of the following to be ready:

- crawl-level Chrome already launched and published
- snapshot tab already created
- snapshot `target_id.txt` present
- snapshot `extensions.json` copied from the crawl session

## Reuse and Cleanup Rules

### Crawl-level reuse

`on_Crawl__90_chrome_launch.daemon.bg.js` will reuse an existing crawl browser only if:

- the session artifacts exist
- `cdp_url.txt` is present and valid
- `chrome.pid` is alive
- the debug port is still reachable

Otherwise it deletes stale artifacts before launching a new browser.

### Snapshot-level reuse

`on_Snapshot__10_chrome_tab.daemon.bg.js` will reuse an existing snapshot tab only if:

- the snapshot markers point at a live target
- the target can be reattached over CDP
- `url.txt` matches the requested URL

If the target is dead or mismatched, it removes stale snapshot markers and opens a new tab.

### Shutdown

- the snapshot tab hook stays alive until `SIGTERM`, then closes its tab and removes `target_id.txt`
- stale snapshot replacement/discard paths also clear `url.txt`, `page_loaded.txt`, `final_url.txt`, and `navigation.json`
- the crawl launch hook stays alive until `SIGTERM`, then closes the browser
- `killChrome(...)` removes crawl `chrome.pid` and `port.txt`; broader stale-session cleanup may also remove `cdp_url.txt`, `extensions.json`, and other markers if they point at a dead session
- stale artifact cleanup only removes marker files; it should not destroy a healthy live session

## Runtime And Test Environments

### Runtime usage

- ArchiveBox/runtime should let the machine/binprovider layer install `puppeteer` and Chromium instead of shelling out to `npm install puppeteer` ad hoc
- JS hooks should rely on `ensureNodeModuleResolution(...)`, not shell-global npm installs or manually mutated `PATH`
- when you want a specific browser in CI or production, set `CHROME_BINARY` explicitly
- when you want persistent browser state, set `CHROME_USER_DATA_DIR` and `CHROME_DOWNLOADS_DIR` explicitly rather than writing profile `Preferences`

### Test usage

- `conftest.py` installs Puppeteer + Chromium once per session via the same hook chain used in production
- tests default to the hook-installed Puppeteer Chromium, but only via `os.environ.setdefault("CHROME_BINARY", chromium_binary)`
- an explicit runtime override such as `CHROME_BINARY=/usr/bin/chromium` remains authoritative
- `setup_test_env(tmpdir)` provisions isolated:
  - `SNAP_DIR`
  - `CRAWL_DIR`
  - `CHROME_EXTENSIONS_DIR`
  - `CHROME_DOWNLOADS_DIR`
  - `CHROME_USER_DATA_DIR`
  - `NODE_MODULES_DIR`
- test helpers should not assume fixture execution order for `NODE_MODULES_DIR` / `PATH`; each hook environment should be constructed explicitly with `get_test_env()` / `setup_test_env(...)`

## Practical Rules For Other Plugins

If you are writing another browser-backed plugin:

- wait for crawl readiness by consuming `CRAWL_DIR/chrome/cdp_url.txt`, not just `chrome.pid`
- for crawl-scoped extension configuration hooks like `twocaptcha` or `claudechrome`, use `waitForCrawlChromeSession(...)` and then `waitForExtensionsMetadata(...)`
- wait for snapshot readiness by consuming `SNAP_DIR/chrome/cdp_url.txt` + `target_id.txt`
- do not assume `navigation.json` / `page_loaded.txt` / `final_url.txt` exist until after `on_Snapshot__30_chrome_navigate.js`
- do not regenerate `extensions.json` yourself; treat the crawl launch hook as the source of truth
- do not launch a second browser for each snapshot unless you are deliberately opting out of the shared-session model

Anti-patterns to avoid:

- do not treat `chrome.pid` or `port.txt` as readiness gates
- do not publish or consume `cdp_url.txt` before crawl-level extension discovery and browser-scoped setup are complete
- do not mutate profile `Preferences` to configure downloads; use CDP download behavior instead
- do not assume unpacked extension IDs are stable or equal to manifest/web-store IDs; runtime IDs are resolved from the unpacked path and live targets
- do not make the Chrome plugin depend on specific child extension plugins; Chrome should only load whatever extension metadata is already cached in the persona extension directory
- do not hardcode host-specific Chrome app paths in tests when `CHROME_BINARY` is intended to be passed in by the runtime
- do not read raw `cdp_url.txt` / `extensions.json` yourself from crawl hooks when the shared helpers already provide the readiness contract

## Verified Local Flow

The current lifecycle was validated locally with a real ArchiveBox run against `https://example.com` using:

- `chrome`
- `singlefile`
- `screenshot`
- `pdf`
- `title`

In that run:

- crawl Chrome launched once
- the snapshot got a unique `target_id.txt`
- `navigation.json`, `page_loaded.txt`, and `final_url.txt` were written only after navigation
- `singlefile.html`, `screenshot.png`, `output.pdf`, and `title.txt` all completed successfully without Chrome crashing

# [ArchiveBox Plugin Marketplace](https://archivebox.github.io/abx-plugins/)

> [!TIP]
> **[➡️ View The Live Gallery 🌠](https://archivebox.github.io/abx-plugins/)**
> [![](https://github.com/user-attachments/assets/e1c70778-ba8b-4812-8b5a-4d8ebc461eed)](https://archivebox.github.io/abx-plugins/)

ArchiveBox-compatible plugin suite (hooks and config schemas).

This package contains only the plugins, to run them use [`abx-dl`](https://github.com/archiveBox/abx-dl) or [`archivebox`](https://github.com/archiveBox/ArchiveBox).

<img width="1000" height="1082" alt="Screenshot 2026-03-11 at 6 53 03 AM" src="https://github.com/user-attachments/assets/08c5f63b-05e2-4947-adca-f64e8c5ad8b3" />

## Usage

Tools like `abx-dl` and ArchiveBox can discover plugins from this package
without symlinks or environment-variable tricks.

## Plugin Contract

### Directory layout

Each plugin lives under `plugins/<name>/` and may include:

- `config.json` config schema
- `on_Crawl__...` per-crawl hook scripts (optional) - install dependencies / set up shared resources
- `on_Snapshot__...` per-snapshot hooks - for each URL: do xyz...

Hooks run with:

- **SNAP_DIR** = base snapshot directory (default: `.`)
- **CRAWL_DIR** = base crawl directory (default: `.`)
- **Snapshot hook output** = `SNAP_DIR/<plugin>/...`
- **Crawl hook output** = `CRAWL_DIR/<plugin>/...`
- **Other plugin outputs** can be read via `../<other-plugin>/...` from your own output dir

### Key environment variables

- `SNAP_DIR` - base snapshot directory (default: `.`)
- `CRAWL_DIR` - base crawl directory (default: `.`)
- `LIB_DIR` - binaries/tools root (default: `~/.config/abx/lib`)
- `PERSONAS_DIR` - persona profiles root (default: `~/.config/abx/personas`)
- `ACTIVE_PERSONA` - persona name (default: `Default`)

### Install hook contract (concise)

Lifecycle:

1. `on_Crawl__*install*` declares crawl dependencies.
2. `on_Binary__*install*` resolves/installs one binary with one provider.

`on_Crawl` output (dependency declaration):

```json
{"type":"Binary","name":"yt-dlp","binproviders":"pip,brew,apt,env","overrides":{"pip":{"install_args":["yt-dlp[default]"]}},"machine_id":"<optional>"}
```

`on_Binary` input/output:

- CLI input should accept `--binary-id`, `--machine-id`, `--name` (plus optional provider args).
- Output should emit installed facts like:

```json
{"type":"Binary","name":"yt-dlp","abspath":"/abs/path","version":"2025.01.01","sha256":"<optional>","binprovider":"pip","machine_id":"<recommended>","binary_id":"<recommended>"}
```

Optional machine patch record:

```json
{"type":"Machine","config":{"PATH":"...","NODE_MODULES_DIR":"...","CHROME_BINARY":"..."}}
```

Semantics:

- `stdout`: JSONL records only
- `stderr`: human logs/debug
- exit `0`: success or intentional skip
- exit non-zero: hard failure

State/OS:

- working dir: `CRAWL_DIR/<plugin>/`
- durable install root: `LIB_DIR` (e.g. npm prefix, pip venv, puppeteer cache)
- providers: `apt` (Debian/Ubuntu), `brew` (macOS/Linux), many hooks currently assume POSIX paths

### Snapshot hook contract (concise)

Lifecycle:

- runs once per snapshot, typically after crawl setup
- common Chrome flow: crawl browser/session -> `chrome_tab` -> `chrome_navigate` -> downstream extractors

State:

- output cwd is usually `SNAP_DIR/<plugin>/`
- hooks may read sibling outputs via `../<plugin>/...`

Output records:

- terminal record is usually:

```json
{"type":"ArchiveResult","status":"succeeded|noresults|skipped|failed","output_str":"path-or-message"}
```

- discovery hooks may also emit `Snapshot` and `Tag` records before `ArchiveResult`
- search indexing hooks are a known exception and may use exit code + stderr without `ArchiveResult`

Semantics:

- `stdout`: JSONL records
- `stderr`: diagnostics/logging
- exit `0`: succeeded, noresults, or skipped
- exit non-zero: failed

### Base plugin utilities

The `base/` plugin provides shared Python and JS helpers that all other plugins import:

**Python** (`base/utils.py`):
```python
from abx_plugins.plugins.base.utils import load_config, emit_archive_result, get_env
```

- `load_config()` — load plugin `config.json` via PydanticSettings with env var + alias resolution, merged with shared base/common runtime vars like `SNAP_DIR`, `CRAWL_DIR`, `LIB_DIR`, `PERSONAS_DIR`, `EXTRA_CONTEXT`, `TIMEOUT`, and `USER_AGENT`
- `emit_archive_result(status, output_str)` — print `{"type":"ArchiveResult",...}` JSONL to stdout
- `output_binary(name, abspath, version, ...)` — emit `Binary` JSONL record
- `output_machine_config(config_dict)` — emit `Machine` config patch
- `write_text_atomic(path, content)` — write file atomically (temp + rename)
- `find_html_source(snap_dir, ...)` — locate HTML from sibling plugins
- `has_staticfile_output(snap_dir, path)` — check if a sibling plugin produced a file
- `get_env(name, default)`, `get_env_bool`, `get_env_int`, `get_env_array` — typed env helpers
- `enforce_lib_permissions()` — lock down `LIB_DIR` so snapshot hooks can read/execute but not write

**JS** (`base/utils.js`):
```javascript
const { loadConfig, getEnv, getEnvBool, getEnvInt, getEnvArray, emitArchiveResult } = require('../base/utils.js');
```

- `loadConfig()` — load plugin `config.json` merged with shared base/common runtime vars using env var + alias + fallback resolution

**Test helpers** (`base/test_utils.py`):
```python
from base.test_utils import parse_jsonl_output, run_hook, get_hook_script
```

- `parse_jsonl_output(stdout)` — extract first matching JSONL record from hook stdout
- `run_hook(hook_script, url, snapshot_id)` — run a hook subprocess with standard args
- `get_hook_script(plugin_dir, pattern)` — find hook script by glob pattern

> **Note:** Use `sys.path.append()` (not `insert(0, ...)`) because the `ssl/` plugin directory would shadow Python's stdlib `ssl` module.

### Rules

- all plugins should:
  - *overwrite* existing files cleanly if re-run in the same dir, do not skip if files are already present (do not delete and then download, because if a process fails we want to leave previous output intact).
  - the exception to always overwriting files is: chrome.pid. target_id.txt, navigation.json, etc. chrome state which gets reused if it's not stale. we should detect if any of it is stale during chrome launch and tab creation, and clear all of it together if it is stale to prevent subtle drift errors / reuse of stale values.
  - status `succeeded` if they ran and produced output
  - status `noresults` if they ran successfully but produced no meaningful output (e.g. git on a non-github url, ytdlp on a site with no media, paperdl on a site with no pdfs, etc.)
  - status `skipped` if only if *config* caused them not to run (e.g. `YTDLP_ENABLED=False`)
  - status `failed` if any hard dependencies are missing/invalid (e.g. chrome) or if the process exited non-0 / raised an exception
  - return a short, meaningful `output_str` e.g. the page title, mimetype, return status code, or the relative path of the primary output file produced like `output.pdf` or `0 modals closed` or `The Page Title Verbatim` or `favicon.io` or `Not a git URL`
  - define execution order solely using lexicographic sort order of hook filenames
  - use bg hooks for either short-lived tasks that can run in parallel, or long-lived daemons that run for the whole duration of the snapshot and get killed for cleanup/final output at the end
  - bg hooks that depend on other bg hook outputs must implement their own waiters internally + check that inputs are truly ready and not just that the files are present, because they may be spawned in parallel/before the earlier one's outputs are actually ready and race. e.g. html/artifact generation should usually be fg so that later bg parsing hooks can safely depend on it being finished and not just part of the file being present
  - use rich_click for cli arg parsing with a uv file header when hooks are written in python. do not depend on archivebox or django, try to only depend on chrome or the output files of other plugins instead of importing code from them. the one exception is to always use chrome_utils.js as the interface for anything involving chrome.


### Event JSONL interface (bbus-style, no dependency)

Hooks emit JSONL events to stdout. They do **not** need to import `bbus`.
The event envelope matches the bbus style so higher layers can stream/replay.

Minimal envelope:

```json
{
  "event_id": "uuidv7",
  "event_type": "SnapshotCreated",
  "event_created_at": "2026-02-01T20:10:22Z",
  "event_parent_id": "uuidv7-or-null",
  "event_schema": "abx.events.v1",
  "event_path": "abx-plugins",
  "data": { "...": "event-specific fields" }
}
```

Conventions:

- Active verb names are **requests** (e.g. `BinaryInstall`, `ProcessLaunch`).
- Past tense names are **facts** (e.g. `BinaryInstalled`, `ProcessExited`).
- Plugins can emit additional fields inside `data` without coordination.

Common event types emitted by hooks:

- `ArchiveResultCreated` (status + output files)
- `Binary` records (dependency detection/install)
- `ProcessStarted` / `ProcessExited`

Higher-level tools (abx-dl / ArchiveBox) can:

- Parse these events from stdout
- Persist or project them (SQLite/JSONL/Django) without plugins knowing

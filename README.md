# [ArchiveBox Plugin Marketplace](https://archivebox.github.io/abx-plugins/)

> [!TIP]
> **[➡️ View The Live Gallery 🌠](https://archivebox.github.io/abx-plugins/)**
> [![](https://github.com/user-attachments/assets/e1c70778-ba8b-4812-8b5a-4d8ebc461eed)](https://archivebox.github.io/abx-plugins/)

ArchiveBox-compatible plugin suite (hooks and config schemas).

This package contains standalone plugin hook scripts and config schemas. A hook
can be run directly as a CLI; runners such as [`abx-dl`](https://github.com/archiveBox/abx-dl)
and [`archivebox`](https://github.com/archiveBox/ArchiveBox) add orchestration,
environment setup, and cache projection around the same scripts.

<img width="1000" height="1082" alt="Screenshot 2026-03-11 at 6 53 03 AM" src="https://github.com/user-attachments/assets/08c5f63b-05e2-4947-adca-f64e8c5ad8b3" />

## Usage

Tools like `abx-dl` and ArchiveBox can discover plugins from this package
without symlinks or environment-variable tricks.

## Plugin Contract

### Directory layout

Each plugin lives under `plugins/<name>/` and may include:

- `config.json` config schema
- `config.json > required_binaries` binary dependency declarations (optional)
- `on_CrawlSetup__...` crawl setup hook scripts (optional) - shared setup/process startup, emit no stdout JSONL records
- `on_Snapshot__...` per-snapshot hooks - emit `ArchiveResult` and may also emit `Snapshot` / `Tag`

Hooks run with:

- **SNAP_DIR** = base snapshot directory (default: `.`)
- **CRAWL_DIR** = base crawl directory (default: `.`)
- **Snapshot hook output** = `SNAP_DIR/<plugin>/...`
- **Crawl hook output** = `CRAWL_DIR/<plugin>/...`
- **Other plugin outputs** can be read via `../<other-plugin>/...` from your own output dir

### Key environment variables

- `SNAP_DIR` - base snapshot directory (default: `.`)
- `CRAWL_DIR` - base crawl directory (default: `.`)
- `ABXPKG_LIB_DIR` - binaries/tools root (default: `~/.config/abx/lib`)
- `PERSONAS_DIR` - persona profiles root (default: `~/.config/abx/personas`)
- `ACTIVE_PERSONA` - persona name (default: `Default`)

### Binary dependency contract (concise)

Lifecycle:

1. `config.json > required_binaries` declares plugin dependencies.
2. Hook config helpers hydrate `*_BINARY` values from env, known local paths, and abxpkg provider state so hooks can run as standalone CLIs.
3. Runners may perform an install preflight from the same declarations. `abx-dl` and ArchiveBox use abxpkg services/cache backends to prepare env/DB state, but hooks must not depend on those services being active.

`config.json` declaration:

```json
[
  {
    "name": "{YTDLP_BINARY}",
    "binproviders": "pip,brew,apt,env",
    "min_version": null,
    "overrides": {
      "pip": {
        "install_args": ["yt-dlp[default]"]
      }
    }
  }
]
```

Runners may project resolved binary metadata internally as `BinaryEvent` records shaped like:

```json
{"type":"Binary","name":"yt-dlp","abspath":"/abs/path","version":"2025.01.01","sha256":"<optional>","binprovider":"pip","machine_id":"<recommended>","binary_id":"<recommended>"}
```

Notes:

- Install resolution is optional runtime preflight work driven directly from `config.json > required_binaries`.
- Binary provider plugins are no longer part of this package; binary provider behavior lives in `abxpkg`.
- Standalone `abx-dl` stores derived binary cache entries in `derived.env`; ArchiveBox stores the equivalent cache in DB `machine_binary` rows. Plugins should stay unaware of both storage layers.

State/OS:

- working dir: `CRAWL_DIR/<plugin>/`
- durable install root: `ABXPKG_LIB_DIR` (e.g. npm prefix, pip venv, puppeteer cache)
- built-in providers include `apt` (Debian/Ubuntu), `brew` (macOS/Linux), and language/runtime-specific installers; many hooks currently assume POSIX paths

### Hook family contract

Lifecycle:

- optional binary preflight can run before crawl setup, but hook scripts also resolve declared binaries through shared config helpers when run directly
- `on_CrawlSetup__*` runs before snapshot extraction and emits no stdout JSONL records
- `on_Snapshot__*` runs once per snapshot and may emit `ArchiveResult`, `Snapshot`, and `Tag` records only

State:

- output cwd is usually `SNAP_DIR/<plugin>/`
- hooks may read sibling outputs via `../<plugin>/...`

Output records:

- `on_Snapshot__*` should finish with an `ArchiveResult` record:

```json
{"type":"ArchiveResult","status":"succeeded|noresults|skipped|failed","output_str":"path-or-message"}
```

- `Snapshot` and `Tag` records may appear before the final `ArchiveResult`

Semantics:

- `stdout`: JSONL records
- `stderr`: diagnostics/logging
- exit `0`: succeeded, noresults, or skipped
- exit non-zero: failed

Rules:

- `on_CrawlSetup__*` hooks should communicate only through side effects such as files, sockets, or long-lived processes, not stdout JSONL records
- `on_Snapshot__*` hooks should not emit `Machine`, `Process`, or `Binary` records

### Base plugin utilities

The `base/` plugin provides shared Python and JS helpers that all other plugins import:

**Python** (`base/utils.py`):
```python
from abx_plugins.plugins.base.utils import (
    load_config,
    emit_archive_result_record,
    emit_snapshot_record,
)
```

- `load_config()` — load plugin `config.json` via jambo with env var + alias + fallback resolution, merged with shared base/common runtime vars like `SNAP_DIR`, `CRAWL_DIR`, `ABXPKG_LIB_DIR`, `PERSONAS_DIR`, `EXTRA_CONTEXT`, `TIMEOUT`, and `USER_AGENT`
- `emit_archive_result_record(status, output_str)` — print `{"type":"ArchiveResult",...}` JSONL to stdout
- `emit_snapshot_record(record)` — emit `{"type":"Snapshot",...}` JSONL to stdout
- `write_text_atomic(path, content)` — write file atomically (temp + rename)
- `find_html_source(snap_dir, ...)` — locate HTML from sibling plugins
- `has_staticfile_output(snap_dir, path)` — check if a sibling plugin produced a file
- `enforce_lib_permissions()` — lock down `ABXPKG_LIB_DIR` so snapshot hooks can read/execute but not write

**JS** (`base/utils.js`):
```javascript
const { loadConfig, getEnv, getEnvBool, getEnvInt, getEnvArray, emitArchiveResultRecord, emitSnapshotRecord } = require('../base/utils.js');
```

- `loadConfig()` — load plugin `config.json` merged with shared base/common runtime vars using env var + alias + fallback resolution
- `emitArchiveResultRecord(status, outputStr)` — emit `ArchiveResult` JSONL to stdout
- `emitSnapshotRecord(record)` — emit `Snapshot` JSONL to stdout

**Test helpers** (`base/test_utils.py`):
```python
from abx_plugins.plugins.base.test_utils import (
    get_hook_script,
    parse_jsonl_output,
    run_hook,
)
```

- `parse_jsonl_output(stdout)` — extract first matching JSONL record from hook stdout
- `run_hook(hook_script, url, snapshot_id=None)` — run a hook subprocess with standard args, optionally relying on `EXTRA_CONTEXT` for snapshot metadata
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


### Hook JSONL interface

Hooks emit plain JSONL records to stdout. The current hook families and records are:

- `on_CrawlSetup__*` → no stdout JSONL records
- `on_Snapshot__*` → `ArchiveResult`, `Snapshot`, `Tag`

`abx-dl` and ArchiveBox map those records into their own internal event systems. Binary request events are produced from plugin config and handled by `abxpkg`, not by plugin hook scripts. Plugins do not need to know or emit any bus envelope format.

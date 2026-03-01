# abx-plugins

ArchiveBox-compatible plugin suite (hooks and config schemas).

This package contains only plugin assets and a tiny helper to locate them.
It does **not** depend on Django or ArchiveBox.

## Usage

```python
from abx_plugins import get_plugins_dir

plugins_dir = get_plugins_dir()
# scan plugins_dir for plugins/*/config.json and on_* hooks
```

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
{"type":"Binary","name":"yt-dlp","binproviders":"pip,brew,apt,env","overrides":{"pip":{"packages":["yt-dlp[default]"]}},"machine_id":"<optional>"}
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
{"type":"ArchiveResult","status":"succeeded|skipped|failed","output_str":"path-or-message"}
```

- discovery hooks may also emit `Snapshot` and `Tag` records before `ArchiveResult`
- search indexing hooks are a known exception and may use exit code + stderr without `ArchiveResult`

Semantics:

- `stdout`: JSONL records
- `stderr`: diagnostics/logging
- exit `0`: succeeded or skipped
- exit non-zero: failed
- current nuance: some skip/transient paths emit no JSONL and rely only on exit code

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

Legacy note:

Some hooks still emit a lightweight JSONL record with a top-level `type` field
(e.g., `{"type": "ArchiveResult", ...}`). Runtimes should accept those and
optionally translate them into the event envelope above.

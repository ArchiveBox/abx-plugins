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

- `config.json` (optional) - config schema
- `on_Crawl*install*` hooks (optional) - dependency/binary install records
- `on_*` hook scripts (required to do work)

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

Install hooks run in two phases:

1. `on_Crawl__*install*` declares dependencies for the crawl.
2. `on_Binary__*install*` resolves/installs one binary via a provider.

`on_Crawl` install hooks should emit `Binary` records like:

```json
{
  "type": "Binary",
  "name": "yt-dlp",
  "binproviders": "pip,brew,apt,env",
  "overrides": {"pip": {"packages": ["yt-dlp[default]"]}},
  "machine_id": "<optional>"
}
```

`on_Binary` install hooks should accept `--binary-id`, `--machine-id`, `--name` and emit installed facts like:

```json
{
  "type": "Binary",
  "name": "yt-dlp",
  "abspath": "/abs/path",
  "version": "2025.01.01",
  "sha256": "<optional>",
  "binprovider": "pip",
  "machine_id": "<recommended>",
  "binary_id": "<recommended>"
}
```

Hooks may also emit `Machine` patches (e.g. `PATH`, `NODE_MODULES_DIR`, `CHROME_BINARY`).

Install hook semantics:

- `stdout` = JSONL records only
- `stderr` = human logs/debug
- exit `0` = success or intentional skip
- non-zero = hard failure

Typical state dirs:

- `CRAWL_DIR/<plugin>/` for per-hook working state
- `LIB_DIR` for durable installs (`npm`, `pip/venv`, puppeteer cache)

OS notes:

- `apt`: Debian/Ubuntu Linux
- `brew`: macOS/Linux
- many hooks currently assume POSIX path semantics

### Snapshot hook contract (concise)

`on_Snapshot__*` hooks run per snapshot, usually after crawl-level setup.

For Chrome-dependent pipelines:

1. crawl hooks create browser/session
2. `chrome_tab` creates snapshot tab state
3. `chrome_navigate` loads page
4. downstream snapshot extractors consume session/output files

Snapshot hooks conventionally:

- use `SNAP_DIR/<plugin>/` as output cwd
- read sibling plugin outputs via `../<plugin>/...` when chaining

Most snapshot hooks emit terminal:

```json
{
  "type": "ArchiveResult",
  "status": "succeeded|skipped|failed",
  "output_str": "path-or-message"
}
```

Some snapshot hooks also emit:

- `Snapshot` and `Tag` records (URL discovery/fanout hooks)

Known exception:

- search indexing hooks may use exit code + stderr only, without `ArchiveResult`

Snapshot hook semantics:

- `stdout` = JSONL output records
- `stderr` = diagnostics/logging
- exit `0` = succeeded or skipped
- non-zero = failure

Current nuance in existing hooks:

- some skip paths emit `ArchiveResult(status='skipped')`
- some transient/disabled paths intentionally emit no JSONL and rely on exit code

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

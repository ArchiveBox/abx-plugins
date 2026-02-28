# abx-plugins

ArchiveBox-compatible plugin suite (hooks, config schemas, binaries manifests).

This package contains only plugin assets and a tiny helper to locate them.
It does **not** depend on Django or ArchiveBox.

## Usage

```python
from abx_plugins import get_plugins_dir

plugins_dir = get_plugins_dir()
# scan plugins_dir for plugins/*/config.json, binaries.jsonl, on_* hooks
```

Tools like `abx-dl` and ArchiveBox can discover plugins from this package
without symlinks or environment-variable tricks.

## Plugin Contract

### Directory layout

Each plugin lives under `plugins/<name>/` and may include:

- `config.json` (optional) - config schema
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

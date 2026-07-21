---
name: abx-plugins
description: Use this when working on ArchiveBox plugin hooks, config schemas, hook ordering, browser helpers, required binaries, and plugin tests.
---

# abx-plugins

## Purpose

`abx-plugins` contains standalone hook scripts and config schemas used by ArchiveBox and `abx-dl`.

## Shared Rules

- Keep this repo on branch `main`.
- Use `uv` and `uv run` for Python commands.
- Do not use system `python`, direct `.venv/bin/python`, or `pip` commands.
- Use real hook scripts, real installs, real browsers, real subprocesses, real files, and real URLs or `pytest-httpserver`.
- Do not mock, monkeypatch, fake, simulate, skip, xfail, or weaken tests.
- Verify JSONL records, exit codes, config hydration, output files, and filesystem side effects.
- Read `README.md` for the full plugin contract, hook lifecycle, config schema, and test surface.

## Development Setup

```bash
set -euo pipefail
uv sync --inexact
uv run --no-sync --no-sources python - <<'PY'
from pathlib import Path

import abx_plugins

package = Path(abx_plugins.__file__).resolve().parent
plugins = {path.name for path in (package / "plugins").iterdir() if path.is_dir()}
assert {"base", "chrome", "hashes", "title", "wget"} <= plugins
PY
```

## User-Facing Inspection

Inspect the installed plugin runtime through `abx-dl`:

```bash
set -euo pipefail
inspection="$(mktemp)"
trap 'rm -f -- "$inspection"' EXIT
uv run --no-sync --no-sources abx-dl version >"$inspection"
grep -q '^abx-dl v' "$inspection"
test "$(uv run --no-sync --no-sources abx-dl config --get TITLE_ENABLED)" = "TITLE_ENABLED=true"
uv run --no-sync --no-sources abx-dl plugins title >"$inspection"
grep -q 'title' "$inspection"
```

## Basic Usage

```bash
set -euo pipefail
test -d abx_plugins/plugins/title
uv run --no-sync --no-sources python -m json.tool abx_plugins/plugins/chrome/config.json >/dev/null
test -x abx_plugins/plugins/title/on_Snapshot__54_title.js
node -c abx_plugins/plugins/chrome/chrome_utils.js
```

## Verification

```bash
set -euo pipefail
uv run --no-sync --no-sources python - <<'PY'
from pathlib import Path

from abx_plugins.plugins.base.testing import get_hook_script, get_plugin_dir

plugin_dir = get_plugin_dir("abx_plugins/plugins/title/tests/test_title.py")
hook = get_hook_script(plugin_dir, "on_Snapshot__*_title.*")
assert plugin_dir.resolve() == Path("abx_plugins/plugins/title").resolve()
assert hook is not None and hook.is_file()
PY
```

Chrome-specific logic belongs in the Chrome plugin helpers. Plugins inherit config from `required_plugins`; do not duplicate config already provided by dependencies.

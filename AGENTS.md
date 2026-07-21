# abx-plugins Agent Guide

`abx-plugins` contains standalone plugin hook scripts and config schemas used by `abx-dl` and ArchiveBox. Keep this repo on `main`.

## Shared Standards

- Use `uv` and `uv run` for Python commands. Do not use system `python`, direct `.venv/bin/python`, or `pip` commands.
- Prefer existing repo patterns, helper APIs, fixtures, scripts, and command surfaces.
- Keep edits focused and minimal. Do not add wrappers, shims, aliases, or extra abstraction layers unless the current code path requires them.
- Do not weaken assertions, skip tests, xfail tests, or accept flaky behavior.
- No mocks, monkeypatches, fakes, simulated handlers, fake binaries, fake hooks, fake buses, or direct shortcuts around user-facing flows.
- Tests and verification should use real hook scripts, real CLI commands, real installs, real browsers, real subprocesses, real files, real URLs or `pytest-httpserver`, and existing fixtures.
- Assertions must verify real correctness: exit codes, JSONL records, output files, config hydration, filesystem contents, field values, and side effects.
- Start behavior fixes with a red failing test when a test is requested or practical.
- Trace root causes from observed behavior. Do not paper over failures with retries, wider timeouts, broad fallbacks, or looser assertions.
- Read `README.md` for the full plugin contract, hook lifecycle, config schema, and test surface.

## Development Setup

```bash
set -euo pipefail
uv sync --inexact
uv run python - <<'PY'
from pathlib import Path

import abx_plugins

repo = Path.cwd().resolve()
package = Path(abx_plugins.__file__).resolve().parent
plugins = {path.name for path in (package / "plugins").iterdir() if path.is_dir()}
assert package.is_relative_to(repo)
assert {"base", "chrome", "hashes", "title", "wget"} <= plugins
PY
```

## User-Facing Setup

Most users run these plugins through ArchiveBox or `abx-dl`:

```bash
set -euo pipefail
output_dir="$(mktemp -d)"
trap 'rm -rf -- "$output_dir"' EXIT
uv run --no-sync --no-sources abx-dl plugins title >"$output_dir/plugins.txt"
grep -q 'title' "$output_dir/plugins.txt"
uv run --no-sync --no-sources abx-dl install hashes
printf 'hello\n' >"$output_dir/output.html"
(cd "$output_dir" && uv run --project "$OLDPWD" --no-sync --no-sources \
  bash -c 'exec "$1" --url=https://example.com' bash \
  "$OLDPWD/abx_plugins/plugins/hashes/on_Snapshot__93_hashes.py" \
  >result.jsonl)
grep -q '"status": "succeeded"' "$output_dir/result.jsonl"
test -s "$output_dir/hashes/hashes.json"
```

## Basic Usage

Inspect plugin config and hooks:

```bash
set -euo pipefail
test -d abx_plugins/plugins/title
uv run python -m json.tool abx_plugins/plugins/chrome/config.json >/dev/null
test -x abx_plugins/plugins/title/on_Snapshot__54_title.js
```

Run targeted plugin tests:

```bash
set -euo pipefail
uv run python - <<'PY'
import tempfile
from pathlib import Path

from abx_plugins.plugins.base.utils import BASE_CONFIG_PATH, load_config

lib_dir = Path(tempfile.mkdtemp()) / "lib"
config = load_config(
    BASE_CONFIG_PATH,
    global_config={"ABXPKG_LIB_DIR": str(lib_dir)},
    hydrate_binaries=False,
)
assert config.ABXPKG_LIB_DIR == str(lib_dir)
assert Path("abx_plugins/plugins/title/on_Snapshot__54_title.js").is_file()
PY
```

Run JS syntax checks for edited hook scripts:

```bash
set -euo pipefail
node -c abx_plugins/plugins/chrome/chrome_utils.js
node -c abx_plugins/plugins/title/on_Snapshot__54_title.js
```

## Hook Rules

- `config.json` owns user-facing plugin config and `required_binaries`.
- Plugins inherit config from `required_plugins`; do not duplicate config already provided by dependencies.
- Hook ordering is lexicographic by hook filename.
- Foreground hooks block later foreground work.
- Background hooks must wait for the specific state they need.
- Snapshot hooks emit JSONL records on stdout and diagnostics on stderr.
- Chrome-specific logic belongs in the Chrome plugin helpers.

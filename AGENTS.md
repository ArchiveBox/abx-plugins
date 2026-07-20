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
uv sync --inexact
uv run pytest --collect-only -q
```

## User-Facing Setup

Most users run these plugins through ArchiveBox or `abx-dl`:

```bash
abx-dl plugins
abx-dl install chrome singlefile ublock
abx-dl dl --plugins=title,screenshot,pdf 'https://example.com'
```

## Basic Usage

Inspect plugin config and hooks:

```bash
ls abx_plugins/plugins
uv run python -m json.tool abx_plugins/plugins/chrome/config.json
find abx_plugins/plugins/title -maxdepth 1 -type f | sort
```

Run targeted plugin tests:

```bash
uv run pytest abx_plugins/plugins/title/tests -q
uv run pytest abx_plugins/plugins/chrome/tests -q
uv run pytest tests/test_runtime_path_isolation.py -q
```

Run JS syntax checks for edited hook scripts:

```bash
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

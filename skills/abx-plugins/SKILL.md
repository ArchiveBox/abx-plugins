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
uv sync --inexact
uv run pytest --collect-only -q
```

## User-Facing Setup

Most users run plugins through ArchiveBox or `abx-dl`:

```bash
output_dir="$(mktemp -d)"
trap 'rm -rf -- "$output_dir"' EXIT
abx-dl plugins
abx-dl install chrome singlefile ublock
abx-dl dl --dir "$output_dir" --plugins=title,screenshot,pdf 'https://example.com'
```

## Basic Usage

```bash
ls abx_plugins/plugins
uv run python -m json.tool abx_plugins/plugins/chrome/config.json
find abx_plugins/plugins/title -maxdepth 1 -type f | sort
node -c abx_plugins/plugins/chrome/chrome_utils.js
```

## Verification

```bash
uv run pytest abx_plugins/plugins/chrome/tests/test_chrome_test_helpers.py -q
uv run prek run --all-files
```

Chrome-specific logic belongs in the Chrome plugin helpers. Plugins inherit config from `required_plugins`; do not duplicate config already provided by dependencies.

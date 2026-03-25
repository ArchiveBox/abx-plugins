# claudecode

Base plugin that installs the [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`@anthropic-ai/claude-code`) and provides shared utilities for other Claude-powered plugins.

This plugin does not run any snapshot-level hooks itself. It provides:
- A crawl-level hook to install the `claude` binary via npm
- `claudecode_utils.py` — shared library used by `claudecodeextract` and `claudecodecleanup`

## Dependencies

| Dependency | Installed by | Notes |
|---|---|---|
| `claude` CLI | This plugin via npm (`@anthropic-ai/claude-code`) | Or provide your own binary via `CLAUDECODE_BINARY` |
| Node.js / npm | Host system | Required for npm-based installation |

## Configuration

All env vars below also serve as defaults for child plugins (`claudecodeextract`, `claudecodecleanup`).

| Variable | Type | Default | Description |
|---|---|---|---|
| `CLAUDECODE_ENABLED` | bool | `false` | Master switch. Must be `true` for the binary to be installed and for child plugins to work. |
| `ANTHROPIC_API_KEY` | string | *(required)* | Anthropic API key. Passed to every Claude Code invocation. |
| `CLAUDECODE_BINARY` | string | `claude` | Path to the Claude Code CLI binary. Set to a custom path to skip npm install. |
| `CLAUDECODE_MODEL` | string | `sonnet` | Default Claude model (`sonnet`, `opus`, `haiku`). Child plugins fall back to this. |
| `CLAUDECODE_TIMEOUT` | int | `120` | Default timeout in seconds. Child plugins fall back to this. |
| `CLAUDECODE_MAX_TURNS` | int | `10` | Default max agentic turns per invocation. Child plugins fall back to this. |

## Dependency Preflight

This plugin does not define runtime hooks of its own. The Claude CLI is resolved
by the orchestrator from `config.json > required_binaries` during `InstallEvent`
preflight before any crawl or snapshot work starts.

## Shared Utilities (`claudecode_utils.py`)

Imported by child plugins:

- `build_system_prompt(snap_dir, crawl_dir, extra_context)` — Builds a system prompt describing the ArchiveBox directory layout and current snapshot metadata.
- `run_claude_code(prompt, work_dir, ...)` — Spawns the Claude Code CLI as a subprocess with the given prompt, model, timeout, allowed tools, and env filtering.
- `load_config()` — Loads typed Claude/child-plugin config with aliases and fallbacks already resolved.
- `emit_archive_result(status, output_str)` — Prints a JSON `ArchiveResult` record to stdout.

## Usage

```bash
# Enable Claude Code integration
export CLAUDECODE_ENABLED=true
export ANTHROPIC_API_KEY=sk-ant-...

# Optionally use a specific model
export CLAUDECODE_MODEL=opus

# Optionally point to a pre-installed binary
export CLAUDECODE_BINARY=/usr/local/bin/claude
```

Enabling this plugin alone only installs the binary. To do useful work, enable one or both child plugins:
- [`claudecodeextract`](../claudecodeextract/) — AI-powered content extraction
- [`claudecodecleanup`](../claudecodecleanup/) — AI-powered deduplication and cleanup

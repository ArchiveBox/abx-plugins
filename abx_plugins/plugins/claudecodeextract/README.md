# claudecodeextract

AI-powered content extraction plugin. Runs a user-configurable prompt against the snapshot directory using [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code), allowing Claude to read existing extractor outputs and generate new derived content.

**Default behavior:** Reads all available extractor outputs (readability, singlefile, dom, etc.) and produces a clean Markdown representation of the page in `content.md`.

## Dependencies

| Dependency | Provided by | Notes |
|---|---|---|
| `claude` CLI | [`claudecode`](../claudecode/) plugin | Must have `CLAUDECODE_ENABLED=true` |
| `ANTHROPIC_API_KEY` | Environment | Required |

## Configuration

Each variable falls back to the corresponding `CLAUDECODE_*` default if unset.

| Variable | Type | Default | Fallback | Description |
|---|---|---|---|---|
| `CLAUDECODEEXTRACT_ENABLED` | bool | `false` | — | Enable AI extraction. |
| `CLAUDECODEEXTRACT_PROMPT` | string | *(see below)* | — | The prompt sent to Claude. Customize to extract different content. |
| `CLAUDECODEEXTRACT_TIMEOUT` | int | `120` | `CLAUDECODE_TIMEOUT` | Timeout in seconds. |
| `CLAUDECODEEXTRACT_MODEL` | string | `sonnet` | `CLAUDECODE_MODEL` | Claude model to use. |
| `CLAUDECODEEXTRACT_MAX_TURNS` | int | `10` | `CLAUDECODE_MAX_TURNS` | Max agentic turns. |

**Default prompt:**
> Read all the previously extracted outputs in this snapshot directory (readability/, mercury/, defuddle/, htmltotext/, dom/, singlefile/, etc.). Using the best available source, generate a clean, well-formatted Markdown representation of the page content. Save the output as content.md in your output directory.

## Hooks

| Hook | Event | Priority | Description |
|---|---|---|---|
| `on_Snapshot__58_claudecodeextract.py` | `Snapshot` | 58 | Runs after most extractors (singlefile, readability, etc.) so their outputs are available as input. |

## Permissions / Scope

- **Read:** Any file within the snapshot directory (`SNAP_DIR`)
- **Write:** Only to its own output directory (`SNAP_DIR/claudecodeextract/`)
- The agent cannot access files outside the snapshot directory

## Output

Files are written to `SNAP_DIR/claudecodeextract/`:

| File | Description |
|---|---|
| `content.md` | Default output — Markdown version of the page (customizable via prompt) |
| `response.txt` | Raw text response from Claude |
| `session.json` | Full conversation log (JSON) |

## Usage

```bash
# Enable the extraction plugin
export CLAUDECODE_ENABLED=true
export CLAUDECODEEXTRACT_ENABLED=true
export ANTHROPIC_API_KEY=sk-ant-...
```

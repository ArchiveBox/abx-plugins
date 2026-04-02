# claudecodecleanup

AI-powered deduplication and cleanup plugin. Runs near the end of the snapshot pipeline to analyze all extractor outputs, identify duplicates and redundant files, and keep only the best version of each.

**Default behavior:** Finds groups of similar outputs (e.g. multiple HTML extractions), compares quality, deletes the inferior versions, and writes a detailed report explaining what was removed and why.

## Dependencies

| Dependency | Provided by | Notes |
|---|---|---|
| `claude` CLI | [`claudecode`](../claudecode/) plugin | Must have `CLAUDECODE_ENABLED=true` |
| `ANTHROPIC_API_KEY` | Environment | Required |

## Configuration

Each variable falls back to the corresponding `CLAUDECODE_*` default if unset.

| Variable | Type | Default | Fallback | Description |
|---|---|---|---|---|
| `CLAUDECODECLEANUP_ENABLED` | bool | `false` | — | Enable AI cleanup. |
| `CLAUDECODECLEANUP_PROMPT` | string | *(see below)* | — | The prompt defining cleanup behavior. |
| `CLAUDECODECLEANUP_TIMEOUT` | int | `180` | `CLAUDECODE_TIMEOUT` | Timeout in seconds. |
| `CLAUDECODECLEANUP_MODEL` | string | `claude-sonnet-4-6` | `CLAUDECODE_MODEL` | Claude model to use. |
| `CLAUDECODECLEANUP_MAX_TURNS` | int | `50` | `CLAUDECODE_MAX_TURNS` | Max agentic turns (higher default than extract due to more complex task). |

**Default prompt:**
> Analyze all the extractor output directories in this snapshot. Look for duplicate or redundant outputs across plugins (e.g. multiple HTML extractions, multiple text extractions, multiple URL extraction outputs, etc.). For each group of similar outputs, inspect the content and determine which version is the best quality. Delete the inferior/redundant versions, keeping only the best one. Also remove any unnecessary temporary files, empty directories, or incomplete outputs. Write a summary of what you cleaned up to cleanup_report.txt in your output directory.

## Hooks

| Hook | Event | Priority | Description |
|---|---|---|---|
| `on_Snapshot__92_claudecodecleanup.py` | `Snapshot` | 92 | Runs near the end of the pipeline, after all extractors but before hashes (priority 93). |

## Permissions / Scope

- **Full access** (read, write, rename, move, delete) within the snapshot directory (`SNAP_DIR`)
- The agent **cannot** access files outside the snapshot directory
- Protected items: `hashes/` directory and `.json` metadata files are never deleted

## Output

Files are written to `SNAP_DIR/claudecodecleanup/`:

| File | Description |
|---|---|
| `cleanup_report.txt` | **Required** — detailed report of what was cleaned up and why (always created, even if nothing was removed) |
| `response.txt` | Raw text response from Claude |
| `session.json` | Full conversation log (JSON) |

## Usage

```bash
# Enable the cleanup plugin
export CLAUDECODE_ENABLED=true
export CLAUDECODECLEANUP_ENABLED=true
export ANTHROPIC_API_KEY=sk-ant-...

# Use a more capable model for complex cleanup decisions
export CLAUDECODECLEANUP_MODEL=claude-opus-4-6

# Custom prompt example: aggressive cleanup
export CLAUDECODECLEANUP_PROMPT="Delete all extractor outputs except singlefile/ and readability/. Remove any files larger than 10MB. Write a report of what was removed."
```

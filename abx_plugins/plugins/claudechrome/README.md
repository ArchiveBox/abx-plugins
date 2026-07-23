# claudechrome

Browser automation plugin that uses Claude's computer-use capability to interact with pages during archiving. Takes screenshots of the current page, sends them to Claude with a user-configurable prompt, and executes the actions Claude requests (click, type, scroll, etc.) via CDP.

This replicates what the [Claude for Chrome](https://chromewebstore.google.com/detail/claude/fcoeoabgfenejglbffodgkkbkcdhcgfn) extension does internally, but works reliably in headless/automated mode without requiring OAuth login.

Optionally, the plugin can also install the official Claude for Chrome extension from the Chrome Web Store for manual use in non-headless sessions.

## How It Works

The snapshot hook runs an agentic loop:
1. Take screenshot of the current page via CDP
2. Send screenshot + prompt to Claude via the Anthropic Messages API (with `computer_20250124` tool)
3. Execute any actions Claude returns (click, type, scroll, key press, etc.)
4. Take new screenshot, send back as tool result
5. Repeat until Claude responds with text-only (task complete) or max iterations reached

## Dependencies

| Dependency | Provided by | Notes |
|---|---|---|
| Chrome/Chromium | [`chrome`](../chrome/) plugin | Required (declared in `required_plugins`) |
| `puppeteer-core` | [`chrome`](../chrome/) plugin | Used for CDP interaction |
| `curl` | System | Used for Anthropic API calls (reliable proxy support) |
| `ANTHROPIC_API_KEY` | Environment | Required for API authentication |

## Configuration

| Variable | Type | Default | Description |
|---|---|---|---|
| `CLAUDECHROME_ENABLED` | bool | `false` | Enable Claude for Chrome. |
| `CLAUDECHROME_PROMPT` | string | *(see below)* | The prompt telling Claude what to do on each page. |
| `CLAUDECHROME_TIMEOUT` | int | `120` | Timeout in seconds per page. |
| `CLAUDECHROME_MODEL` | string | `claude-sonnet-4-6` | Claude model to use. Model IDs: `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`, `claude-opus-4-6`. |
| `CLAUDECHROME_MAX_ACTIONS` | int | `15` | Maximum agentic loop iterations per page. |
| `ANTHROPIC_API_KEY` | string | *(required)* | Anthropic API key. |

**Default prompt:**
> Look at the current page. If there are any "expand", "show more", "load more", or similar buttons/links, click them all to reveal hidden content. Report what you did.

## Hooks

| Hook | Event | Priority | Type | Description |
|---|---|---|---|---|
| `on_CrawlSetup__96_claudechrome_config.js` | `Crawl` | 96 | Foreground | (Optional) Injects `ANTHROPIC_API_KEY` into extension storage. |
| `on_Snapshot__47_claudechrome.js` | `Snapshot` | 47 | Foreground | Runs Claude computer-use on the page via CDP screenshots + Anthropic API. |

The optional Claude for Chrome extension asset is resolved during orchestrator
preflight from `config.json > required_binaries`; it is not installed by a
plugin hook family.

### Hook Execution Order (Snapshot)

```
...
45  infiniscroll        Expand infinite scroll / lazy loading
47  claudechrome        <-- Claude interacts with the page
50  singlefile          Archive the (now-modified) page HTML
51  screenshot          Take screenshot of the (now-modified) page
...
```

This ordering means Claude's page modifications (expanded sections, downloaded files, filled forms) are captured by all subsequent extractors.

## Permissions / Scope

- **Read:** Takes screenshots of the current browser tab via CDP
- **Write:** Final artifacts are stored in its own output directory (`SNAP_DIR/claudechrome/`); Chrome may temporarily write downloads under `chrome_downloads/` before they are moved
- **Browser interaction:** Can click, type, scroll, and navigate within the current tab
- Any files downloaded by Chrome during the interaction are moved from `chrome_downloads/` to the output directory

## Output

Files are written to `SNAP_DIR/claudechrome/`:

| File | Description |
|---|---|
| `conversation.json` | Structured log: prompt, responses, actions, timestamps, model used |
| `conversation.txt` | Human-readable conversation transcript |
| `screenshot_initial.png` | Page state before Claude interacted with it |
| `screenshot_001.png`, ... | Screenshots after each action |
| `screenshot_final.png` | Final page state after all actions |
| `*.pdf`, `*.html`, etc. | Any files Claude downloaded from the page (moved from `chrome_downloads/`) |

## Usage

```bash
set -euo pipefail
repo_root="$PWD"
snap_dir="$(mktemp -d)"
trap 'rm -rf -- "$snap_dir"' EXIT

(
  cd "$snap_dir"
  CLAUDECHROME_ENABLED=false SNAP_DIR="$snap_dir" \
    "$repo_root/abx_plugins/plugins/claudechrome/on_Snapshot__47_claudechrome.js" \
    --url=https://example.com
) >"$snap_dir/result.jsonl"

grep -q '"status":"skipped"' "$snap_dir/result.jsonl"
```

For a live run, set `CLAUDECHROME_ENABLED=true`, provide
`ANTHROPIC_API_KEY`, and invoke the same hook inside the Chrome plugin's active
snapshot session. `CLAUDECHROME_PROMPT`, `CLAUDECHROME_MODEL`,
`CLAUDECHROME_MAX_ACTIONS`, and `CLAUDECHROME_TIMEOUT` customize the interaction.

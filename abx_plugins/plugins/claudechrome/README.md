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
| `CLAUDECHROME_MODEL` | string | `sonnet` | Claude model to use. Short names: `sonnet`, `haiku`, `opus`. |
| `CLAUDECHROME_MAX_ACTIONS` | int | `15` | Maximum agentic loop iterations per page. |
| `ANTHROPIC_API_KEY` | string | *(required)* | Anthropic API key. |

**Default prompt:**
> Look at the current page. If there are any "expand", "show more", "load more", or similar buttons/links, click them all to reveal hidden content. Report what you did.

## Hooks

| Hook | Event | Priority | Type | Description |
|---|---|---|---|---|
| `on_Crawl__84_claudechrome_install.bg.js` | `Crawl` | 84 | Background | (Optional) Downloads Claude for Chrome extension from CWS for manual use. |
| `on_Crawl__96_claudechrome_config.js` | `Crawl` | 96 | Foreground | (Optional) Injects `ANTHROPIC_API_KEY` into extension storage. |
| `on_Snapshot__47_claudechrome.js` | `Snapshot` | 47 | Foreground | Runs Claude computer-use on the page via CDP screenshots + Anthropic API. |

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
# Enable Claude for Chrome
export CLAUDECHROME_ENABLED=true
export ANTHROPIC_API_KEY=sk-ant-...

# Default: click all expand/show-more buttons
archivebox add "https://example.com"

# Custom: download all linked PDFs
export CLAUDECHROME_PROMPT="Find all links to PDF files on this page and click each one to download it."

# Custom: fill in a search form
export CLAUDECHROME_PROMPT="Find the search input field, type 'archivebox', and press Enter."

# Custom: click through a cookie consent dialog
export CLAUDECHROME_PROMPT="If there is a cookie consent banner, click 'Accept All' or 'OK' to dismiss it."

# Use a faster model
export CLAUDECHROME_MODEL=haiku

# Allow more complex interactions
export CLAUDECHROME_MAX_ACTIONS=30
export CLAUDECHROME_TIMEOUT=300
```

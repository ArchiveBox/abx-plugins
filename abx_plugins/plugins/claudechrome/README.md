# claudechrome

Browser automation plugin that installs and drives the official [Claude for Chrome](https://chromewebstore.google.com/detail/claude/fcoeoabgfenejglbffodgkkbkcdhcgfn) extension to interact with pages during archiving.

Claude for Chrome is an AI agent that can click buttons, fill forms, navigate pages, and download files directly in the browser. This plugin runs a user-configurable prompt against each page before extractors run, allowing you to prepare the page content (expand collapsed sections, click through paywalls, download linked files, etc.).

## Dependencies

| Dependency | Provided by | Notes |
|---|---|---|
| Chrome/Chromium | [`chrome`](../chrome/) plugin | Required (declared in `required_plugins`) |
| Claude for Chrome extension | This plugin (auto-installed from CWS) | Extension ID: `fcoeoabgfenejglbffodgkkbkcdhcgfn` |
| `puppeteer-core` | [`chrome`](../chrome/) plugin | Used for CDP interaction |
| `ANTHROPIC_API_KEY` | Environment | Required for API authentication |

## Configuration

| Variable | Type | Default | Description |
|---|---|---|---|
| `CLAUDECHROME_ENABLED` | bool | `false` | Enable Claude for Chrome. |
| `CLAUDECHROME_PROMPT` | string | *(see below)* | The prompt telling Claude what to do on each page. |
| `CLAUDECHROME_TIMEOUT` | int | `120` | Timeout in seconds per page. |
| `CLAUDECHROME_MODEL` | string | `sonnet` | Claude model to use. Availability depends on your Anthropic plan. |
| `ANTHROPIC_API_KEY` | string | *(required)* | Anthropic API key. |

**Default prompt:**
> Look at the current page. If there are any "expand", "show more", "load more", or similar buttons/links, click them all to reveal hidden content. Report what you did.

## Hooks

| Hook | Event | Priority | Type | Description |
|---|---|---|---|---|
| `on_Crawl__84_claudechrome_install.bg.js` | `Crawl` | 84 | Background | Downloads and unpacks the Claude for Chrome extension CRX from Chrome Web Store. |
| `on_Crawl__96_claudechrome_config.js` | `Crawl` | 96 | Foreground | Injects `ANTHROPIC_API_KEY` into extension storage after Chrome launches (priority 90). |
| `on_Snapshot__47_claudechrome.js` | `Snapshot` | 47 | Foreground | Runs the prompt on the current page via the extension's side panel. |

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

- The extension runs within the Chrome session and can interact with any page Chrome has loaded
- Downloads triggered by Claude are saved to `chrome_downloads/` then moved to the output directory
- The extension cannot access files outside the browser context

## Output

Files are written to `SNAP_DIR/claudechrome/`:

| File | Description |
|---|---|
| `conversation.json` | Structured log: prompt, response, timestamps, success/error status |
| `conversation.txt` | Human-readable conversation transcript |
| `*.pdf`, `*.html`, etc. | Any files Claude downloaded from the page (moved from `chrome_downloads/`) |

## Authentication

Claude for Chrome normally authenticates via OAuth (claude.com login). This plugin attempts to inject the `ANTHROPIC_API_KEY` directly into the extension's storage, but this may not work with all extension versions. If API key injection fails:

1. The plugin logs a warning but does not fail
2. You may need to manually log in to claude.com in the Chrome session before archiving
3. The config hook writes a marker file to avoid repeated injection attempts

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

# Longer timeout for complex interactions
export CLAUDECHROME_TIMEOUT=300
```

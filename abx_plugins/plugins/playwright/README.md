# Playwright

Plugin identifier: `playwright`.

`on_Snapshot__31_playwright.js` runs in the foreground after Chrome navigation and before scrolling or capture. It exits with status `skipped` (code 10) when no script or prompt is configured, before loading a browser driver. Dependencies are declared in `config.json` and installed by the runner/abxpkg.

Set `PLAYWRIGHT_SCRIPT` through plugin/persona config or an environment variable. It accepts an async JavaScript function body receiving `browser`, `page`, `config` and `outputDir`. The hook attaches to the exact existing snapshot tab over CDP, verifies main-world JavaScript, runs the script, and disconnects without closing the capture tab or browser. Scripts must leave them open. `PLAYWRIGHT_TIMEOUT` bounds the task (default 120 seconds).

Example:

```sh
export PLAYWRIGHT_SCRIPT="await page.click('#prepare');"
abx-dl --plugins=playwright,title 'https://example.com'
```

Successful tasks write `playwright/result.log` with the tab ID, final title and URL. Tests execute real hooks and browser clients without ArchiveBox:

```sh
uv run pytest abx_plugins/plugins/playwright/tests -q
```

Saved files appear under **Embedded media** on the snapshot detail page. Hooks write an `index.html` listing only when their output directory contains captured files; status logs alone produce no card. JavaScript scripts receive `outputDir`; Python script relative paths use the plugin output directory.

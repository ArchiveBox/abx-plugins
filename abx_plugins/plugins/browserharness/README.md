# browser-harness

Plugin identifier: `browserharness`.

`on_Snapshot__31_browserharness.py` runs in the foreground after Chrome navigation and before scrolling or capture. It exits with status `skipped` (code 10) when no script or prompt is configured, before loading a browser driver. Dependencies are declared in `config.json` and installed by the runner/abxpkg.

Set `BROWSERHARNESS_SCRIPT` through plugin/persona config or an environment variable. It accepts Python executed by browser-harness with its native helpers (`js`, `switch_tab`, etc.). The hook attaches to the exact existing snapshot tab over CDP, verifies main-world JavaScript, runs the script, and disconnects without closing the capture tab or browser. Scripts must leave them open. `BROWSERHARNESS_TIMEOUT` bounds the task (default 240 seconds).

Example:

```sh
export BROWSERHARNESS_SCRIPT="js(\"document.querySelector('#prepare').click()\")"
abx-dl --plugins=browserharness,title 'https://example.com'
```

Set `BROWSERHARNESS_PROMPT` and `BROWSERHARNESS_MODEL` (provider/model, for example `openai/gpt-6.1-sol`) for AI page preparation. A configured script takes precedence. The task receives the current title, URL, bounded DOM summary and your instructions. Provider credentials come from the provider environment variables. OpenCode supplies the agent loop, invokes the connected browser-harness CLI, and exposes a native `done` tool to end the task. The pinned browser-harness has no native secret-substitution API, so this plugin does not invent one.

The plugin also declares `agent_tools` and `agent_skills` for OpenCode browser agents. These declarations preserve web UI access without making OpenCode depend on this plugin. Importers explicitly depend on both browser clients; the existing replay and tab lifecycle remain unchanged.

Successful tasks write `browserharness/result.log` with the tab ID, final title and URL. Tests execute real hooks and browser clients without ArchiveBox:

```sh
uv run pytest abx_plugins/plugins/browserharness/tests -q
```

The normal CI test `test_ten_challenges` uses `OPENAI_API_KEY`, model `openai/gpt-6.1-sol`, and a 240-second hook timeout. It solves ten tasks on the public browser challenge page, calls `done`, then verifies a score of 10 or 11 in the DOM plugin’s saved HTML. It takes no screenshots.

Saved files appear under **Embedded media** on the snapshot detail page. Hooks write an `index.html` listing only when their output directory contains captured files; status logs alone produce no card. JavaScript scripts receive `outputDir`; Python script relative paths use the plugin output directory.

## Domain-specific preparation

The settings editor can generate domain guards in the existing Python script
field. Use the attached page's hostname to match exact domains and subdomains:

```python
import sys

host = js("location.hostname")
if host != "example.com" and not host.endswith(".example.com"):
    print("Domain not selected: " + host)
    sys.exit(10)
# Prepare the matching page here.
```

Exit code 10 from the configured script emits the existing `skipped`
ArchiveResult after the normal temporary-daemon cleanup. Script stdout becomes
the reason, or `Script skipped` when empty. Other nonzero exits remain failures;
normal scripts keep their existing behavior. Config keys and string values are
unchanged.

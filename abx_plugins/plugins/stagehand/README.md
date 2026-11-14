# Stagehand

Plugin identifier: `stagehand`.

`on_Snapshot__31_stagehand.js` runs in the foreground after Chrome navigation and before scrolling or capture. It exits with status `skipped` (code 10) when no script or prompt is configured, before loading a browser driver. Dependencies are declared in `config.json` and installed by the runner/abxpkg.

Set `STAGEHAND_SCRIPT` through plugin/persona config or an environment variable. It accepts an async JavaScript function body receiving `browser`, `page`, `config`, `client` and `outputDir`. The hook attaches to the exact existing snapshot tab over CDP, verifies main-world JavaScript, runs the script, and disconnects without closing the capture tab or browser. Scripts must leave them open. `STAGEHAND_TIMEOUT` bounds the task (default 240 seconds).

Example:

```sh
export STAGEHAND_SCRIPT="await page.evaluate({expression: \"document.querySelector('#prepare').click()\"});"
abx-dl --plugins=stagehand,title 'https://example.com'
```

Set `STAGEHAND_PROMPT` and `STAGEHAND_MODEL` (provider/model, for example `openai/gpt-6.1-sol`) for AI page preparation. A configured script takes precedence. The task receives the current title, URL, bounded DOM summary and your instructions. Provider credentials come from the provider environment variables. OpenCode runs the task loop and calls the native Stagehand client on the same tab; a native `done` tool ends the task. The pinned Stagehand release offers action planning rather than a complete task loop. `STAGEHAND_VARIABLES` accepts the native JSON mapping of variable names to `{type, value, description}` objects; reference them as `%name%` in the prompt. The agent can call `act(instruction)` for native Stagehand substitution; values are passed directly to Stagehand, not interpolated into the task prompt. `STAGEHAND_LLM_MODEL`, `STAGEHAND_API_KEY`, and `STAGEHAND_BASE_URL` configure that native LLM connection when needed.

The plugin also declares `agent_tools` and `agent_skills` for OpenCode browser agents. These declarations preserve web UI access without making OpenCode depend on this plugin. Importers explicitly depend on both browser clients; the existing replay and tab lifecycle remain unchanged.

Successful tasks write `stagehand/result.log` with the tab ID, final title and URL. Tests execute real hooks and browser clients without ArchiveBox:

```sh
uv run pytest abx_plugins/plugins/stagehand/tests -q
```

The normal CI test `test_ten_challenges` uses `OPENAI_API_KEY`, model `openai/gpt-6.1-sol`, and a 240-second hook timeout. It solves ten tasks on the public browser challenge page, calls `done`, then verifies a score of 10 or 11 in the DOM plugin’s saved HTML. It takes no screenshots.

Saved files appear under **Embedded media** on the snapshot detail page. Hooks write an `index.html` listing only when their output directory contains captured files; status logs alone produce no card. JavaScript scripts receive `outputDir`; Python script relative paths use the plugin output directory.

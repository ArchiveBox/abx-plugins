---
name: archivebox-browser
description: Use Browser Harness or Stagehand v4 to edit a persistent persona or inspect the exact browser used by a capture.
---

Run from the ArchiveBox collection directory. For logins/settings that future
crawls should inherit, work on the **base persona**, opened with
`archivebox persona open NAME`. It publishes its live CDP session; the handoff
command below attaches without restarting, navigating, or importing old state.
`--snapshot UUID` / `--crawl UUID` attach to a capture's isolated fork instead;
changes there do not automatically update the base persona.

`{python} -m abx_plugins.plugins.opencode.archivebox.browser --list` lists active
browsers, crawl/persona identities, tabs, and CDP URLs. Use `--current` to attach
to the exact browser/tab selected in `/admin/agent/`, or `--browser ID --target ID`
for an explicit selection. Always select `ARCHIVEBOX_BROWSER_TARGET_ID` in the
client when set. Never guess which concurrent crawl the user means.

Pause affected crawls before editing the base persona:
`printf '%s\n' '{"id":"CRAWL_UUID"}' | archivebox crawl update --status=paused`.
Wait for their running hooks and crawl browser to stop. Make and verify the changes, save portable
state as below, then resume with the same command using `--status=queued`.
Pause intentionally cleans up the crawl browser. Resume restarts it, rereads the
crawl's current config, and forks the updated base persona; pending URLs stay in
the existing crawl queue. Browser IDs/CDP URLs change, so rediscover after resume.
Never delete runtime profiles or
cookie/storage files to force a refresh.

The plugin provides the clients and connection environment. Write your own
scripts for repeated tasks under `opencode/scripts/`; use the clients' native
APIs. This prefix resolves and verifies the explicitly selected live browser:

```sh
{python} -m abx_plugins.plugins.opencode.archivebox.browser --persona NAME -- browser-harness <<'PY'
print(list_tabs())
# For --snapshot, select os.environ['ARCHIVEBOX_BROWSER_TARGET_ID'] with switch_tab().
PY
```

Repeat the prefix for each invocation, including `browser-harness skill` for its
API. It sets BU_CDP_WS, BU_CDP_URL, and a browser-specific BU_NAME. Reuse existing
tabs/contexts. Do not use local browser discovery or cloud launch commands.

For Stagehand v4, write an `.mts` script and run with the same prefix followed by
`-- tsx /absolute/path/task.mts`. The installed version is `shalpha` rc.56:

```ts
const { StagehandClient } = await import(process.env.ARCHIVEBOX_STAGEHAND_MODULE!);
const client = new StagehandClient({
  cdp_url: process.env.ARCHIVEBOX_BROWSER_CDP_URL!, keep_alive: true,
});
await client.connect();
try {
  const pages = await client.browser.pages();
  console.log(pages.map(page => ({ targetId: page.targetId, url: page.url })));
  // Select the intended page and run your task here.
} finally { await client.close(); } // disconnect; preserve the shared browser
```

After editing a **base persona**, save its live session cookies and open-tab
storage for the next fork. Run your Python script through the same handoff
prefix; use ArchiveBox's existing export function:

```python
import os
from archivebox.config.django import setup_django
setup_django(check_db=True)
from archivebox.personas.models import Persona
from archivebox.personas.importers import export_browser_state
persona = Persona.find_named(os.environ['ARCHIVEBOX_PERSONA_NAME'])
assert os.environ['ARCHIVEBOX_BROWSER_PROFILE_SCOPE'] == 'persona'
ok, _, error = export_browser_state(
    cdp_url=os.environ['ARCHIVEBOX_BROWSER_CDP_URL'],
    cookies_output_file=persona.path / 'cookies.txt',
    auth_output_file=persona.path / 'auth.json',
)
assert ok, error
```

Leave the relevant tabs open through export so their sessionStorage is included.
Keep existing cookie expiry/session flags; never add TTLs, clear storage, or
close the persona browser as routine cleanup. CDP URLs and auth files grant
access: keep them local. If the session has closed, report it; don't substitute
another browser. Stagehand requires extension injection support on the CDP host.

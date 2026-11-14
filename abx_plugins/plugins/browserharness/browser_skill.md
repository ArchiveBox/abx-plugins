```sh
{browser_command} --persona NAME -- browser-harness <<'PY'
print(list_tabs())
import os
if os.environ.get('ARCHIVEBOX_BROWSER_TARGET_ID'):
    switch_tab(os.environ['ARCHIVEBOX_BROWSER_TARGET_ID'])
print(page_info())
PY
```

Repeat the prefix for each invocation, including `browser-harness skill` for its
API. It sets BU_CDP_WS, BU_CDP_URL, and a browser-specific BU_NAME. Reuse existing
tabs/contexts. Do not use local browser discovery or cloud launch commands.
For a new browsing task with no suitable live browser, open the intended persona
using the background command above, then attach to that persona. Do not navigate
a capture's tab away from its capture URL; create a task tab in the chosen persona
when needed. If an explicitly requested capture has closed, report that fact.

Use `--list` to discover its exact tab before attaching. Do not repeatedly launch
it or replace an unrelated browser. Foreground `persona open` (without
`--background`) stays running until Chrome closes and is for interactive terminals
or an existing process supervisor, not an agent's bounded shell tool.

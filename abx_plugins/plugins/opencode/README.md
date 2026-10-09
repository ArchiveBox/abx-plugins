# OpenCode

This optional plugin owns the OpenCode process, dependency resolution, isolated
state, session setup, HTTP/SSE/WebSocket forwarding, and agent UI templates. Importing the
plugin package does not import or start its runtime. The runtime's HTTP clients
are declared in the `opencode` package extra.

`image.py` owns image-specific package cleanup and installed-binary checks.
`archivebox/screenshots.py` owns gallery setup and the agent screenshot entry.
ArchiveBox imports these entry points rather than duplicating plugin paths,
package names, configuration, or validation logic.

The plugin owns an optional `archivebox/` integration module, imported by the
host for its routes and WebSocket handler. It supplies the lazy Django adapter: authentication, collection and
route context, template rendering, and conversion to Django responses. The
runtime imports neither ArchiveBox nor Django. Only the optional integration
module imports host APIs; its test fixtures reuse the host conftest helpers. WebSockets use the same superuser
session boundary and require a same-origin handshake before connecting upstream.
A failed import, startup, request,
or template returns an AI-only unavailable response; failed streams report an
error event and close. Ordinary pages do not import the runtime.

The embedded app uses its native router base for the mount path. Browser origin,
pathname, and history retain their normal semantics; only the web entrypoint's
default API server and asset URLs receive the prefix. SDK requests and protocol
discovery preserve that server's mount path when resolving endpoints. Session navigation and
reloads are exercised with the real UI, and terminal transport with a real shell.
Both `/event` and `/global/event` stream without buffering. Provider OAuth
callbacks remain open while OpenCode waits for authorization or cancellation;
the ordinary API read timeout must not abort a pending human login.

For ChatGPT on a remote server or in Docker, choose **ChatGPT Pro/Plus (headless)**
and complete the device-code flow. The embedded picker makes a best-effort tweak
to put this choice first, preserving every method and its original OAuth ID.
Unrecognized future UI builds are left unchanged. OpenCode's **browser** method
requires its `localhost:1455` callback listener to be reachable from the user's
browser; that loopback callback is not the ArchiveBox HTTP proxy. Anthropic uses
an API key.

The wrapper preserves existing browser-side servers and projects. Unavailable
or full browser storage cannot suppress the access warning or block dismissal.
The authenticated Agent wrapper renders before a cold OpenCode start. Its iframe
request starts the server, reuses or creates the collection's default session,
and redirects to that session; the welcome panel is available while it loads.

OpenCode works directly in the collection directory without initializing Git.
Git discovery is disabled for its process, including ancestor and nested repos.
Checkpointing is forced off even when existing user configuration enables it.
The FFF indexer and filesystem watcher are disabled; a final `*` rule in the
working directory's `.ignore` prevents the fallback ripgrep indexer from walking
any descendants, including snapshots and OpenCode's own state. This also excludes
the collection from ordinary ripgrep searches; existing ignore rules are retained.
Targeted file reads and ArchiveBox CLI/database/API access remain available.
State and credentials stay under `DATA_DIR/opencode`; user configuration files
are preserved. These controls prevent automatic scans, not explicit shell commands.

Runtime tests live in `tests/test_runtime.py`. The host's authentication,
HTTP/streaming, browser, and incomplete-install cases live in `archivebox/`
alongside its conftest fixtures. ArchiveBox imports these cases into its existing
CI test files, where Django and the real host are available. Standalone plugin
CI runs the runtime suite without requiring ArchiveBox or Django.
`tests/test_collection_isolation.py` runs the real pinned OpenCode server against
ancestor/nested Git repositories and verifies indexing exclusions and file reads.

ArchiveBox's Dockerfile installs this plugin's dependencies in its app layers;
the abx-dl downloader image does not include OpenCode.
The agent remains disabled by default; installing its executable does not enable
the route or start the service.

## Capture tasks

The optional ArchiveBox adapter accepts a separate JSON submission at
`POST /admin/agent/tasks/` (or `/api/v1/agent/tasks/` on the API origin):
`{"snapshot_id": "<UUID>", "task": "save this entire site"}`.
It requires an active superuser's API key in `Authorization: Bearer ...` or
`X-ArchiveBox-API-Key`; cookies and query-string keys do not authorize this
CSRF-exempt endpoint. The ordinary agent UI/proxy still requires its admin session.

The adapter loads authoritative snapshot metadata, starts the existing collection
runtime, creates a new session in its normal OpenCode database, and submits the
context and user task through `prompt_async`. It returns HTTP 201 with `session_id`,
`snapshot_id`, and an admin `session_url`. `/admin/agent/?session=ses_...` selects
that session in the embedded UI. Acceptance means submitted, not task completion.
Disabled agents return 409; invalid/missing snapshots or tasks return 400/404;
service failures return 503. The extension preserves its form on failure.

The API alias avoids redirecting a credentialed POST between API and admin hosts.
No host dependency changes are needed; the normal plugin publication cascade
carries these routes into ArchiveBox. Real host integration coverage lives in
`archivebox/agent_3_cases.py`, imported by ArchiveBox's existing agent suite.

## Live browsers

The right side of `/admin/agent/` lists Chrome processes and their real tabs.
Selecting a tab activates that server tab and shows its native screencast. The
managed `archivebox-browser` skill documents `--list`, `--current`, explicit
browser/target selection, Browser Harness, and Stagehand v4. Clients attach to
the same CDP session; they never launch a second automation browser.

For persistent login or profile changes, pause the affected crawl, wait for its
browser cleanup, edit the base persona with `archivebox persona open NAME`, save
its portable state, and resume the existing crawl. Resume rereads crawl config
and forks the updated persona. Pending URLs remain in that queue. Direct edits
to a running capture affect its fork, so export wanted changes to the base
persona before cleanup if they should survive a restart.

Inventory requests read small files published by the existing Chrome owner and
active Process rows; they never probe CDP, scan the snapshot queue, or spawn a
subprocess. One native screencast per viewed tab writes a latest-frame mailbox;
HTTP backpressure discards intermediate frames. Hidden/disconnected viewers
release demand, crashes invalidate only that target, and stale browser selection
never silently attaches to a different process. Expiring abandoned viewer demand
does not expire cookies, session storage, or browser profiles. These controls
bound preview work, but browser encoding and streaming still consume resources.

`archivebox/browser_panel_cases.py` measures real HTTP latency, frame age, CPU and
RSS with previews closed, inventory open, streaming, and disconnected. It also
exercises real UI selection, navigation, and a renderer crash against two live
Chrome instances. Run it through the host's `test_opencode_browser_panel.py`.

## Server context

OpenCode automatically loads the collection's editable `opencode/SKILL.md` and
plugin-managed `opencode/server_context.md`. The latter points to one read-only
command, `python -m abx_plugins.plugins.opencode.archivebox.context`, for current
crawl/queue summaries, running processes, exact data/log/config paths, personas,
and browser/tab connections. `--crawl UUID` scopes the view; default category
limits keep large queues out of the prompt. Changes and execution use existing
ArchiveBox commands and model APIs, described in the instructions.

Performance data is the progress monitor's own timestamped system sample,
published once per background refresh to local temporary storage. Missing/stale
samples remain visibly unknown; the agent starts no additional OS sampler.
Context is queried on demand, never on the preview polling path. `ONLY_NEW=True`
is the documented default unless the administrator asks to recapture URLs.


## Web browsing and visual checks

The managed browser skill is loaded as a system instruction for every session,
including existing collections. It directs the agent to Browser Harness or
Stagehand after a Webfetch failure, or immediately for JavaScript, authenticated
pages, interactions, or visual checks. Webfetch does not inherit browser cookies;
ArchiveBox CLI/shell handles authenticated server operations.

Both clients can save a screenshot of the explicitly selected live tab. The skill
includes native screenshot examples and instructs image-capable models to open
the PNG with OpenCode's `read` tool before claiming visual verification. Evidence
lives under `opencode/screenshots/`, outside captured snapshot payloads.

Dependency resolution preserves the host's Python environment. Browser clients
use their own installed interpreters without projecting unrelated downloader
packages into ArchiveBox CLI subprocesses.

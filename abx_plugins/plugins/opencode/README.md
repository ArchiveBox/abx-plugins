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

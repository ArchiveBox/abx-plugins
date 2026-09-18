# OpenCode

This optional plugin owns the OpenCode process, dependency resolution, isolated
state, session setup, HTTP/SSE/WebSocket forwarding, and agent UI templates. Importing the
plugin package does not import or start its runtime. The runtime's HTTP clients
are declared in the `opencode` package extra.

ArchiveBox supplies a thin, lazy Django adapter: authentication, collection and
route context, template rendering, and conversion to Django responses. The
runtime imports neither ArchiveBox nor Django. WebSockets use the same superuser
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
and complete the device-code flow. OpenCode's **browser** method requires its
`localhost:1455` callback listener to be reachable from the user's browser; that
loopback callback is not the ArchiveBox HTTP proxy. Anthropic uses an API key.

The wrapper preserves existing browser-side servers and projects. Unavailable
or full browser storage cannot suppress the access warning or block dismissal.

OpenCode works directly in the collection directory without initializing Git.
Checkpointing defaults to disabled. State and credentials stay under
`DATA_DIR/opencode`; existing user configuration is preserved.

Runtime tests live in `tests/test_runtime.py`. The host's authentication,
HTTP/streaming, and incomplete-install integration tests live in ArchiveBox.

ArchiveBox's Dockerfile installs this plugin's dependencies in its app layers;
the abx-dl downloader image does not include OpenCode.
The agent remains disabled by default; installing its executable does not enable
the route or start the service.

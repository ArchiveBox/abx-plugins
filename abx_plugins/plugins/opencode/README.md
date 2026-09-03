# OpenCode

This optional plugin owns the OpenCode process, dependency resolution, isolated
state, session setup, HTTP/SSE forwarding, and agent UI templates. Importing the
plugin package does not import or start its runtime. The runtime's HTTP clients
are declared in the `opencode` package extra.

ArchiveBox supplies a thin, lazy Django adapter: authentication, collection and
route context, template rendering, and conversion to Django responses. The
runtime imports neither ArchiveBox nor Django. A failed import, startup, request,
or template returns an AI-only unavailable response; failed streams report an
error event and close. Ordinary pages do not import the runtime.

The wrapper preserves existing browser-side servers and projects. Unavailable
or full browser storage cannot suppress the access warning or block dismissal.

OpenCode works directly in the collection directory without initializing Git.
Checkpointing defaults to disabled. State and credentials stay under
`DATA_DIR/opencode`; existing user configuration is preserved.

Runtime tests live in `tests/test_runtime.py`. The host's authentication,
HTTP/streaming, and incomplete-install integration tests live in ArchiveBox.

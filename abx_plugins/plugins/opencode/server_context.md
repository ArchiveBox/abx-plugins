You are operating a live ArchiveBox server. Start server work by reading its
current context from the collection directory:

```sh
{python} -m abx_plugins.plugins.opencode.archivebox.context
```

This read-only command returns active/queued/paused crawls, queue counts for those
crawls, running processes, personas, data/log/config paths, live browser CDP URLs
and tabs, the user's selected tab, and the progress UI's latest performance sample
(host load, available RAM, disk free, IOPS, bandwidth and I/O latency). Check sample
timestamps: missing or stale metrics are unknown, not zero. Load/core count is
not CPU utilization. The command does not launch browsers or sample the OS.
Use `--crawl UUID` for a specific crawl or `--limit N` for a larger bounded view.
Refresh before acting; browser IDs and process IDs can change. Never recursively
scan the collection, dump every queued URL, or poll this context in a tight loop.

Use existing ArchiveBox interfaces and their `--help`, rather than inventing a
second job runner. `archivebox add URL` queues work for the server;
`archivebox run --help` describes explicitly running queued work. Keep
`ONLY_NEW=True` unless the user asks to recapture existing URLs. Use
`archivebox crawl`, `archivebox snapshot`, `archivebox persona`, and the REST
OpenAPI schema to discover supported filters and mutations.

For targeted inspection, use `archivebox shell` with the Django models and the
IDs returned above. `get_config(crawl=crawl, persona=persona)` from
`archivebox.config.common` resolves effective config; `redact_sensitive_config`
can make it safe to display. Collection config is managed with
`archivebox config --get KEY` / `--set KEY=VALUE`; crawl and persona overrides
live in their model's `config` JSON, editable in admin or through the shell.
Merge requested keys instead of replacing unrelated settings. Prefer the
returned model paths over guessing the archive layout. Inspect specific hook
outputs/logs and ArchiveResult records to diagnose work.

For live browser work, load the `archivebox-browser` skill. It describes the
connection prefix, Browser Harness and Stagehand v4. `--current` attaches to the
browser/tab selected in the Agent panel. Those are already hydrated browsers;
do not launch substitutes. A live crawl uses a persona fork; durable changes
belong in the base persona and its exported state. When changes need a fresh
browser/config, pause affected crawls, wait for their browser cleanup, then
resume those same queues. Do not restart unrelated crawls or clear session state.

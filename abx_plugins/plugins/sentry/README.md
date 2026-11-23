# Sentry

Optional monitoring for ArchiveBox, **disabled by default** with an **empty
DSN**. A DSN alone never enables reporting. Disabled processes do not import the
Sentry SDK, OpenTelemetry, or the plugin runtime, and emit no browser SDK markup.

## Setup

From the collection directory:

```bash
SENTRY_ENABLED=false archivebox install sentry
archivebox config --set SENTRY_DSN='YOUR_PROJECT_DSN' SENTRY_ENABLED=True
```

Restart servers and workers after configuration changes.
The Machine configuration UI uses the same settings. Installation is explicit;
startup never downloads packages. No ArchiveBox reporting destination is built in.
All Sentry settings are machine/collection-owned (`x-scope: server`); they are
absent from add/persona settings and cannot be overridden or frozen on a crawl.

## Reporting

- Django exceptions and Python ERROR/CRITICAL records create issues. Breadcrumbs
  use `SENTRY_LOG_LEVEL` (INFO by default). Failed, non-cancelled hook processes
  also create issues grouped by plugin, hook and exit code, with a bounded stderr
  tail. Skipped hooks and ordinary stderr progress do not create issues.
- `SENTRY_ENABLE_LOGS=true` enables structured Python logs and browser warning/error
  console logs. Host Python logger thresholds still apply. The existing process
  bus also forwards hook stdout/stderr and completion records, with plugin, hook,
  stream, event and output-directory attributes. Ordinary stderr is INFO;
  unsuccessful, non-cancelled completions are ERROR. Python records and hook output
  are sent once, at their source.
- `SENTRY_TRACES_SAMPLE_RATE=1` records all traces; fractions sample fewer traces
  and the default zero disables tracing. Django requests, SQLite queries,
  requests/urllib3 calls, browser navigation/fetch and native EventBus event/handler
  trees are instrumented. Sentry headers propagate browser-to-server traces only
  to the same origin. Monitoring export requests are excluded from instrumentation.
  Spans use Sentry's native OTLP ingestion with SQL statements and database
  attributes intact. Request and event context supplies query parents; queries
  outside those operations remain visible so background work can be diagnosed.
- `SENTRY_ENABLE_METRICS=true` sends span counts and duration distributions, grouped
  by instrumentation scope/status, plus browser page-load counts and DOM-ready times.
- `SENTRY_PROFILE_SESSION_SAMPLE_RATE=1` enables native Python CPU profiling in
  every server/worker process. Fractions sample fewer processes; the default zero
  disables profiling. Restart after changes. It does not profile browser JS.
  Profiles cover the enabled process lifetime, including work between requests
  and bus events. Spans carry the profiler and thread IDs that link them to those
  samples. Profiling is independent of the trace sample rate.
- `SENTRY_BROWSER_ENABLED=true` allows the browser integration **only when the
  machine's SENTRY_ENABLED is also true**. Its pinned, integrity-checked CDN bundle
  includes errors, tracing, logs and metrics. Session replay is never loaded or enabled.
  Page-load spans end at the browser load event; background progress polls remain
  traced without extending the page-load measurement to the SDK's 30-second limit.
- `SENTRY_ENVIRONMENT` defaults to production. `SENTRY_RELEASE` defaults to SDK
  detection; set it to the deployed version/commit for reliable release grouping.
- PII collection is off by default. Request bodies and frame locals are excluded.
  `SENTRY_SEND_DEFAULT_PII=true` enables Django's authenticated user ID, username
  and email, and the same identity in the browser. Anonymous pages clear identity.
  Messages, SQL statements and URLs may still contain application data; configure
  project scrubbing as appropriate.

## Host hooks

`config.json` declares `app_module`, settings and optional
pinned dependencies. ArchiveBox registers the native Django `AppConfig`; `ready()`
reads `settings.CONFIG.model_dump(mode="json")` and initializes the SDK only after
explicit opt-in. Its `render_page_head(request, config)` hook renders browser setup
using the current machine configuration and Django's safe JSON serialization.
The plugin imports neither ArchiveBox nor abx-dl.

ArchiveBox calls `app.register_eventbus(bus, config)` before dispatch on its crawl,
binary-resolution and install buses. The plugin
checks its own enable flag before importing its runtime and attaching the native
`OtelTracingMiddleware`, backed by a private tracer provider and the standard
batched OTLP exporter. Completed spans are sent independently, without waiting
for an entire crawl or buffering a transaction's complete span tree. Sentry's
OTLP integration links errors, logs and metrics to the current OTel context.
Standalone abx-dl never calls this hook or initializes Sentry.
Child extractors do not initialize their own SDK; their process and handler spans
are recorded by ArchiveBox's bus.

See the upstream [OpenTelemetry Django instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/django/django.html),
[Sentry metrics](https://docs.sentry.io/platforms/python/metrics/) and
[browser bundles](https://docs.sentry.io/platforms/javascript/install/loader/#cdn).

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

## Reporting

- Django exceptions and Python ERROR/CRITICAL records create issues. Breadcrumbs
  use `SENTRY_LOG_LEVEL` (INFO by default).
- `SENTRY_ENABLE_LOGS=true` enables structured Python logs and browser warning/error
  console logs. Host Python logger thresholds still apply.
- `SENTRY_TRACES_SAMPLE_RATE=1` records all traces; fractions sample fewer traces
  and the default zero disables tracing. Django requests, SQLite queries,
  requests/urllib3 calls, browser navigation/fetch and native EventBus event/handler
  trees are instrumented. Sentry headers propagate browser-to-server traces only
  to the same origin. Monitoring export requests are excluded from instrumentation.
- `SENTRY_ENABLE_METRICS=true` sends span counts and duration distributions, grouped
  by instrumentation scope/status, plus browser page-load counts and DOM-ready times.
- `SENTRY_BROWSER_ENABLED=true` allows the browser integration **only when the
  machine's SENTRY_ENABLED is also true**. Its pinned, integrity-checked CDN bundle
  includes errors, tracing, logs and metrics. Session replay is never loaded or enabled.
- `SENTRY_ENVIRONMENT` defaults to production. `SENTRY_RELEASE` defaults to SDK
  detection; set it to the deployed version/commit for reliable release grouping.
- PII collection is off by default. Request bodies and frame locals are excluded.
  Messages, SQL statements and URLs may still contain application data; configure
  project scrubbing as appropriate. CPU profiling is not enabled by this plugin.

## Host hooks

`config.json` declares `app_module`, settings and optional
pinned dependencies. ArchiveBox registers the native Django `AppConfig`; `ready()`
reads `settings.CONFIG.model_dump(mode="json")` and initializes the SDK only after
explicit opt-in. Its `render_page_head(request, config)` hook renders browser setup
using the current machine configuration and Django's safe JSON serialization.
The plugin imports neither ArchiveBox nor abx-dl.

ArchiveBox calls `app.register_eventbus(bus, config)` for each new bus. The plugin
checks its own enable flag before importing its runtime and attaching the native
`OtelTracingMiddleware`, backed by a private tracer provider and Sentry's native
span processor. Standalone abx-dl never calls this hook or initializes Sentry.
Child extractors do not initialize their own SDK; their process and handler spans
are recorded by ArchiveBox's bus.

See the upstream [OpenTelemetry Django instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/django/django.html),
[Sentry metrics](https://docs.sentry.io/platforms/python/metrics/) and
[browser bundles](https://docs.sentry.io/platforms/javascript/install/loader/#cdn).

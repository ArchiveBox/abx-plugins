"""Sentry SDK and native EventBus tracing; imported only after explicit opt-in."""

import logging
import os
import re
import sys
from importlib import import_module
from pathlib import Path
from urllib.parse import urlsplit

from abxpkg import BinaryLoadError

from abx_plugins.plugins.base.utils import (
    get_hydrated_required_binaries,
    load_required_binary,
)


CONFIG_PATH = Path(__file__).with_name("config.json")
_provider = None
_sentry = None


def initialize(config, *, django=False):
    global _provider, _sentry
    if not config.SENTRY_ENABLED:
        return None
    if not config.SENTRY_DSN.strip():
        raise ValueError("SENTRY_ENABLED requires your own SENTRY_DSN")
    if _sentry is None:
        # Installation is explicit, never a side effect of runtime startup.
        record = get_hydrated_required_binaries(CONFIG_PATH)[0]
        try:
            sdk = load_required_binary(record, config=config.model_dump())
        except BinaryLoadError as error:
            raise RuntimeError(
                "Sentry SDK is not installed. Run `SENTRY_ENABLED=false "
                "archivebox install sentry` (or `abx-dl install sentry`), "
                "then enable Sentry and restart.",
            ) from error
        # Provider-owned paths avoid coupling to the managed venv layout.
        for path in sdk.loaded_binprovider.ENV.get("PYTHONPATH", "").split(os.pathsep):
            if path and path not in sys.path:
                sys.path.append(path)

        sentry = import_module("sentry_sdk")
        logs = import_module("sentry_sdk.integrations.logging")
        tracing = import_module("opentelemetry.sdk.trace")
        sentry_otel = import_module("sentry_sdk.integrations.opentelemetry")
        level = getattr(logging, config.SENTRY_LOG_LEVEL)
        integrations = [
            logs.LoggingIntegration(
                level=level,
                event_level=logging.ERROR,
                sentry_logs_level=level,
            ),
        ]
        if django:
            integration = import_module("sentry_sdk.integrations.django")
            integrations.append(integration.DjangoIntegration())
        sentry.init(
            dsn=config.SENTRY_DSN.strip(),
            environment=config.SENTRY_ENVIRONMENT,
            release=config.SENTRY_RELEASE or None,
            integrations=integrations,
            auto_enabling_integrations=False,
            send_default_pii=config.SENTRY_SEND_DEFAULT_PII,
            include_local_variables=False,
            max_request_body_size="never",
            traces_sample_rate=config.SENTRY_TRACES_SAMPLE_RATE,
            enable_logs=config.SENTRY_ENABLE_LOGS,
            enable_metrics=config.SENTRY_ENABLE_METRICS,
            instrumenter="otel",
        )
        # A private provider leaves the host's global OpenTelemetry provider alone.
        _provider = tracing.TracerProvider()
        _provider.add_span_processor(sentry_otel.SentrySpanProcessor())
        if config.SENTRY_ENABLE_METRICS:

            class MetricsProcessor(tracing.SpanProcessor):
                def on_end(self, span):
                    attributes = {
                        "scope": span.instrumentation_scope.name,
                        "status": span.status.status_code.name,
                    }
                    sentry.metrics.count("archivebox.spans", 1, attributes=attributes)
                    sentry.metrics.distribution(
                        "archivebox.span.duration",
                        (span.end_time - span.start_time) / 1_000_000,
                        unit="millisecond",
                        attributes=attributes,
                    )

            _provider.add_span_processor(MetricsProcessor())
        propagation = import_module("opentelemetry.propagate")
        propagation.set_global_textmap(sentry_otel.SentryPropagator())
        for module, name in (
            ("sqlite3", "SQLite3Instrumentor"),
            ("requests", "RequestsInstrumentor"),
            ("urllib3", "URLLib3Instrumentor"),
        ):
            instrumentation = import_module(f"opentelemetry.instrumentation.{module}")
            options = {"tracer_provider": _provider}
            if module != "sqlite3":
                # Monitoring exports must never recursively trace themselves.
                endpoint = urlsplit(config.SENTRY_DSN)
                options["excluded_urls"] = re.escape(
                    f"{endpoint.scheme}://{endpoint.hostname}",
                )
            getattr(instrumentation, name)().instrument(**options)
        _sentry = sentry

    if django:
        instrumentation = import_module("opentelemetry.instrumentation.django")
        instrumentation.DjangoInstrumentor().instrument(tracer_provider=_provider)
    return _provider


def get_eventbus_middlewares(config):
    provider = initialize(config)
    if provider is None:
        return []
    from abxbus.middlewares import OtelTracingMiddleware

    return [OtelTracingMiddleware(tracer=provider.get_tracer("abxbus"))]

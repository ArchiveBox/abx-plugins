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
    load_required_binary_from_config,
)


CONFIG_PATH = Path(__file__).with_name("config.json")
_provider = None
_sentry = None


def shutdown():
    if _sentry is not None:
        if _sentry.get_client().options["profile_lifecycle"] == "manual":
            _sentry.profiler.stop_profiler()
        _sentry.flush()


def register_hook_logs(bus, config):
    """Consume the host bus's existing process streams, without rereading log files."""
    assert _sentry is not None
    sentry = _sentry
    level = getattr(logging, config.SENTRY_LOG_LEVEL)

    def attributes(event):
        return {
            "sentry.origin": "auto.log.archivebox.hook",
            "plugin.name": event.plugin_name,
            "hook.name": event.hook_name,
            "abxbus.event_id": event.event_id,
            "abxbus.event_parent_id": event.event_parent_id or "",
            "process.output_dir": event.output_dir,
        }

    async def stdout(event):
        if level <= logging.INFO:
            sentry.logger.info(
                event.line,
                attributes={**attributes(event), "process.stream": "stdout"},
            )

    async def stderr(event):
        # stderr is also used for ordinary progress; it is not proof of an error.
        if level <= logging.INFO:
            sentry.logger.info(
                event.line,
                attributes={**attributes(event), "process.stream": "stderr"},
            )

    async def completed(event):
        failed = event.status == "failed" and not event.cancelled
        severity = logging.ERROR if failed else logging.INFO
        if level <= severity:
            emit = sentry.logger.error if failed else sentry.logger.info
            emit(
                "Hook {hook} finished: {status} (exit {exit_code})",
                hook=event.hook_name,
                status=event.status,
                exit_code=event.exit_code,
                attributes={
                    **attributes(event),
                    "process.exit_code": event.exit_code,
                    "process.cancelled": event.cancelled,
                },
            )

    bus.on("ProcessStdoutEvent", stdout)
    bus.on("ProcessStderrEvent", stderr)
    bus.on("ProcessCompletedEvent", completed)


def initialize(config):
    global _provider, _sentry
    if not config.SENTRY_ENABLED:
        return None
    if not config.SENTRY_DSN.strip():
        raise ValueError("SENTRY_ENABLED requires your own SENTRY_DSN")
    if _sentry is None:
        # Installation is explicit, never a side effect of runtime startup.
        try:
            sdk = load_required_binary_from_config(
                "sentry_sdk",
                CONFIG_PATH,
                global_config=config.model_dump(),
                environ={},
            )
        except BinaryLoadError as error:
            raise RuntimeError(
                "Sentry SDK is not installed. Run `SENTRY_ENABLED=false "
                "archivebox install sentry`, "
                "then enable Sentry and restart.",
            ) from error
        # Provider-owned paths avoid coupling to the managed venv layout.
        for path in sdk.loaded_binprovider.ENV.get("PYTHONPATH", "").split(os.pathsep):
            if path and path not in sys.path:
                sys.path.append(path)

        sentry = import_module("sentry_sdk")
        logs = import_module("sentry_sdk.integrations.logging")
        tracing = import_module("opentelemetry.sdk.trace")
        trace_api = import_module("opentelemetry.trace")
        sentry_otel = import_module("sentry_sdk.integrations.opentelemetry")

        def link_telemetry(item, hint):
            context = trace_api.get_current_span().get_span_context()
            if context.is_valid:
                item["trace_id"] = trace_api.format_trace_id(context.trace_id)
                item["span_id"] = trace_api.format_span_id(context.span_id)
            return item

        level = getattr(logging, config.SENTRY_LOG_LEVEL)
        integrations = [
            logs.LoggingIntegration(
                level=level,
                event_level=logging.ERROR,
                sentry_logs_level=level,
            ),
        ]
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
            profile_session_sample_rate=config.SENTRY_PROFILE_SESSION_SAMPLE_RATE,
            profile_lifecycle=config.SENTRY_PROFILE_LIFECYCLE,
            enable_logs=config.SENTRY_ENABLE_LOGS,
            enable_metrics=config.SENTRY_ENABLE_METRICS,
            before_send_log=link_telemetry,
            before_send_metric=link_telemetry,
            instrumenter="otel",
        )
        if (
            config.SENTRY_PROFILE_LIFECYCLE == "manual"
            and config.SENTRY_PROFILE_SESSION_SAMPLE_RATE
        ):
            sentry.profiler.start_profiler()
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

        instrumentation = import_module("opentelemetry.instrumentation.django")
        instrumentation.DjangoInstrumentor().instrument(tracer_provider=_provider)
    return _provider

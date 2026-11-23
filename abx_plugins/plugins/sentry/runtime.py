"""Sentry SDK and native EventBus tracing; imported only after explicit opt-in."""

import logging
import os
import re
import sys
import threading
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
    if _provider is not None:
        _provider.shutdown()
    if _sentry is not None:
        _sentry.profiler.stop_profiler()
        _sentry.flush()


def register_hook_reporting(bus, config):
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
        if config.SENTRY_ENABLE_LOGS and level <= logging.INFO:
            sentry.logger.info(
                event.line,
                attributes={**attributes(event), "process.stream": "stdout"},
            )

    async def stderr(event):
        # stderr is also used for ordinary progress; it is not proof of an error.
        if config.SENTRY_ENABLE_LOGS and level <= logging.INFO:
            sentry.logger.info(
                event.line,
                attributes={**attributes(event), "process.stream": "stderr"},
            )

    async def completed(event):
        failed = event.status == "failed" and not event.cancelled
        severity = logging.ERROR if failed else logging.INFO
        if failed:
            with sentry.isolation_scope() as scope:
                scope.fingerprint = [
                    "archivebox.hook",
                    event.plugin_name,
                    event.hook_name,
                    str(event.exit_code),
                ]
                scope.set_tag("plugin.name", event.plugin_name)
                scope.set_tag("hook.name", event.hook_name)
                scope.set_context(
                    "hook",
                    {
                        **attributes(event),
                        "exit_code": event.exit_code,
                        "stderr": event.stderr[-8192:],
                    },
                )
                sentry.capture_message(
                    f"Hook {event.hook_name} failed (exit {event.exit_code})",
                    level="error",
                )
        if config.SENTRY_ENABLE_LOGS and level <= severity:
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
        sampling = import_module("opentelemetry.sdk.trace.sampling")
        trace_api = import_module("opentelemetry.trace")
        otlp = import_module("sentry_sdk.integrations.otlp")
        exporter = import_module(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        )
        export = import_module("opentelemetry.sdk.trace.export")
        resources = import_module("opentelemetry.sdk.resources")
        profiler = import_module("sentry_sdk.profiler.continuous_profiler")

        level = getattr(logging, config.SENTRY_LOG_LEVEL)
        integrations = [
            otlp.OTLPIntegration(setup_otlp_traces_exporter=False),
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
            profile_session_sample_rate=config.SENTRY_PROFILE_SESSION_SAMPLE_RATE,
            profile_lifecycle="manual",
            enable_logs=config.SENTRY_ENABLE_LOGS,
            enable_metrics=config.SENTRY_ENABLE_METRICS,
            # Native Django/SQL spans would otherwise shadow the OTel span ID
            # on logs and metrics, even without a Sentry trace sampling rate.
            instrumenter="otel",
        )
        if config.SENTRY_PROFILE_SESSION_SAMPLE_RATE:
            sentry.profiler.start_profiler()
        # A private provider leaves the host's global OpenTelemetry provider alone.
        _provider = tracing.TracerProvider(
            sampler=sampling.ParentBased(
                sampling.TraceIdRatioBased(config.SENTRY_TRACES_SAMPLE_RATE),
            )
            if config.SENTRY_TRACES_SAMPLE_RATE
            else sampling.ALWAYS_OFF,
            resource=resources.Resource.create(
                {
                    "service.name": "archivebox",
                    "service.version": sentry.get_client().options["release"] or "",
                    "deployment.environment.name": config.SENTRY_ENVIRONMENT,
                    "host.name": sentry.get_client().options["server_name"] or "",
                    "process.pid": os.getpid(),
                },
            ),
        )
        auth = sentry.utils.Dsn(config.SENTRY_DSN.strip()).to_auth(
            f"sentry.python/{sentry.VERSION}",
        )
        _provider.add_span_processor(
            export.BatchSpanProcessor(
                exporter.OTLPSpanExporter(
                    endpoint=auth.get_api_url(sentry.consts.EndpointType.OTLP_TRACES),
                    headers={"X-Sentry-Auth": auth.to_header()},
                ),
            ),
        )

        class ProfileContextProcessor(tracing.SpanProcessor):
            def on_start(self, span, parent_context=None):
                span.set_attribute("thread.id", str(threading.get_ident()))
                span.set_attribute("thread.name", threading.current_thread().name)
                profiler_id = profiler.get_profiler_id()
                if profiler_id is not None:
                    span.set_attribute("sentry.profiler_id", profiler_id)

        _provider.add_span_processor(ProfileContextProcessor())
        if config.SENTRY_ENABLE_METRICS:

            class MetricsProcessor(tracing.SpanProcessor):
                def on_end(self, span):
                    attributes = {
                        "scope": span.instrumentation_scope.name,
                        "status": span.status.status_code.name,
                    }
                    # on_end can run after the caller has detached this span.
                    # Keep its native trace context on metrics about that span.
                    with trace_api.use_span(trace_api.NonRecordingSpan(span.context)):
                        sentry.metrics.count(
                            "archivebox.spans",
                            1,
                            attributes=attributes,
                        )
                        sentry.metrics.distribution(
                            "archivebox.span.duration",
                            (span.end_time - span.start_time) / 1_000_000,
                            unit="millisecond",
                            attributes=attributes,
                        )

            _provider.add_span_processor(MetricsProcessor())
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

        def response_hook(span, request, response):
            if config.SENTRY_SEND_DEFAULT_PII:
                user = getattr(request, "user", None)
                if user is not None and user.is_authenticated:
                    span.set_attribute("user.id", str(user.pk))
                    span.set_attribute("user.name", user.get_username())
                    if getattr(user, "email", None):
                        span.set_attribute("user.email", user.email)

        instrumentation = import_module("opentelemetry.instrumentation.django")
        instrumentation.DjangoInstrumentor().instrument(
            tracer_provider=_provider,
            response_hook=response_hook,
        )
    return _provider

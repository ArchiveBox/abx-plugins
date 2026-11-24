"""Sentry SDK and native EventBus tracing; imported only after explicit opt-in."""

import logging
import os
import re
import sys
import threading
import weakref
from contextlib import contextmanager
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

from abxpkg import BinaryLoadError
from abxbus.middlewares import OtelTracingMiddleware as NativeOtelTracingMiddleware

from abx_plugins.plugins.base.utils import (
    load_required_binary_from_config,
)


CONFIG_PATH = Path(__file__).with_name("config.json")
_provider = None
_sentry = None
_bus_middlewares = weakref.WeakSet()

PROCESS_ATTRIBUTES = (
    "process.id",
    "process.parent_id",
    "process.type",
    "worker.type",
    "process.pid",
    "process.parent_pid",
    "process.command",
    "supervisor.process.name",
    "crawl.id",
    "snapshot.id",
    "plugin.name",
    "hook.name",
    "hook.process.id",
    "hook.process.parent_id",
    "hook.process.type",
    "hook.process.pid",
    "hook.worker.type",
    "user.id",
    "user.name",
    "user.email",
    "snapshot.url",
    "snapshot.title",
)


def event_attributes(bus, event):
    """Read existing event ancestry and persisted Process results without DB reads."""
    ancestry = []
    seen = set()
    current = event
    while current is not None and current.event_id not in seen:
        seen.add(current.event_id)
        ancestry.append(current)
        current = bus.event_history.get(current.event_parent_id)
    attributes = {}
    for ancestor in reversed(ancestry):
        if ancestor.event_type.startswith("Snapshot") and getattr(
            ancestor,
            "snapshot_id",
            "",
        ):
            attributes["snapshot.id"] = ancestor.snapshot_id
            if getattr(ancestor, "url", ""):
                attributes["snapshot.url"] = ancestor.url
        env = getattr(ancestor, "env", {})
        if env.get("CRAWL_ID"):
            attributes["crawl.id"] = env["CRAWL_ID"]
        for field, key in (
            ("plugin_name", "plugin.name"),
            ("hook_name", "hook.name"),
            ("output_dir", "process.output_dir"),
        ):
            value = getattr(ancestor, field, "")
            if value:
                attributes[key] = value
        if ancestor.event_type in {"ProcessStartedEvent", "ProcessCompletedEvent"}:
            for result in ancestor.event_results.values():
                record = result.result
                if not isinstance(record, dict) or record.get("type") != "Process":
                    continue
                if record.get("parent_id"):
                    attributes["process.id"] = record["parent_id"]
                # Process.to_json is the host's existing return record. Select
                # only identity/timing fields; commands and environment may hold
                # credentials and must never become telemetry attributes.
                for field, key in (
                    ("id", "hook.process.id"),
                    ("parent_id", "hook.process.parent_id"),
                    ("process_type", "hook.process.type"),
                    ("worker_type", "hook.worker.type"),
                    ("pid", "hook.process.pid"),
                    ("started_at", "process.started_at"),
                    ("ended_at", "process.ended_at"),
                    ("exit_code", "process.exit_code"),
                ):
                    if record.get(field) is not None:
                        attributes[key] = record[field]
        if ancestor.event_type == "ProcessCompletedEvent":
            attributes.update(
                {
                    "process.exit_code": ancestor.exit_code,
                    "process.cancelled": ancestor.cancelled,
                },
            )
            if ancestor.start_ts and ancestor.end_ts:
                from datetime import datetime

                attributes.update(
                    {
                        "process.started_at": ancestor.start_ts,
                        "process.ended_at": ancestor.end_ts,
                        "process.duration": (
                            datetime.fromisoformat(ancestor.end_ts)
                            - datetime.fromisoformat(ancestor.start_ts)
                        ).total_seconds()
                        * 1000,
                    },
                )
    return attributes


class OtelTracingMiddleware(NativeOtelTracingMiddleware):
    """Enrich the native bus tree with the host's existing Process lifecycle."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._process_completions = {}
        _bus_middlewares.add(self)
        # A destroyed bus can disappear before application shutdown. Keep only
        # the span dictionaries in the finalizer, never the bus/events/self;
        # otherwise the cleanup callback would itself retain the whole crawl.
        self._finalizer = weakref.finalize(
            self,
            self._close_spans,
            self._event_spans,
            self._handler_spans,
        )

    @staticmethod
    def _close_spans(events, handlers):
        for spans in handlers.values():
            for span in spans.values():
                span.end()
        for span in events.values():
            span.end()
        handlers.clear()
        events.clear()

    def close(self):
        self._finalizer()
        self._process_completions.clear()
        self._completed_events.clear()
        self._handler_contexts.clear()

    def _event_span_attributes(self, eventbus, event):
        return {
            **super()._event_span_attributes(eventbus, event),
            **event_attributes(eventbus, event),
        }

    def _event_span(self, eventbus, event):
        # The bus consumes events in a context-empty queue task. Its saved
        # emit-time context is also the parent of a top-level event's span;
        # otherwise a crawl starts a disconnected trace before handlers restore
        # that context. Copy it so concurrent callbacks never enter one Context.
        if not event.event_parent_id:
            context = event._get_dispatch_context()
            if context is not None:
                return context.copy().run(super()._event_span, eventbus, event)
        return super()._event_span(eventbus, event)

    def _handler_span_attributes(self, eventbus, event, event_result):
        return {
            **super()._handler_span_attributes(eventbus, event, event_result),
            **event_attributes(eventbus, event),
        }

    def _finish_completed_event(self, eventbus, event):
        if event.event_type == "ProcessEvent":
            completion = self._process_completions.get(event.event_id)
            # Background dispatch completes as soon as the child starts. Keep
            # only its telemetry span open until the existing completion fact;
            # this does not change bus scheduling or subprocess ownership.
            started = any(
                child.event_type == "ProcessStartedEvent"
                for child in event.event_children
            )
            if (
                getattr(event, "is_background", False)
                and started
                and completion is None
            ):
                return
            if completion is not None:
                attributes = event_attributes(eventbus, completion)
                for span in [
                    self._event_spans[event.event_id],
                    *self._handler_spans.get(event.event_id, {}).values(),
                ]:
                    self._set_span_attributes(span, attributes)
        super()._finish_completed_event(eventbus, event)
        if event.event_id not in self._event_spans:
            self._process_completions.pop(event.event_id, None)

    def _end_span(self, span, start_time=None, end_time=None):
        attributes = getattr(span, "attributes", {}) or {}
        if attributes.get("abxbus.event_type") == "ProcessEvent":
            completion = self._process_completions.get(
                attributes.get("abxbus.event_id"),
            )
            if completion is not None and completion.event_completed_at:
                # The span includes completion projection/logging; the actual
                # child runtime remains in process.started_at/ended_at/duration.
                # Foreground handlers may finish later still, so never shorten.
                end_time = max(
                    filter(None, (end_time, completion.event_completed_at)),
                    key=lambda value: self._otel_timestamp(value) or 0,
                )
        super()._end_span(span, start_time=start_time, end_time=end_time)

    def _set_span_ok(self, span):
        attributes = getattr(span, "attributes", {}) or {}
        if (
            attributes.get("process.exit_code") not in (None, 0)
            and not attributes.get("process.cancelled")
            and self._status_cls
            and self._status_code
        ):
            span.set_status(
                self._status_cls(self._status_code.ERROR, "Hook process failed"),
            )
        else:
            super()._set_span_ok(span)

    async def on_event_result_change(self, eventbus, event, event_result, status):
        # Publishing the row result identifies the already-running parent span
        # before stdout consumers execute, including background hook readers.
        if status == "completed" and event.event_type == "ProcessStartedEvent":
            parent = (
                eventbus.event_history.get(event.event_parent_id)
                if event.event_parent_id
                else None
            )
            if parent is not None and parent.event_id in self._event_spans:
                attributes = event_attributes(eventbus, event)
                self._set_span_attributes(
                    self._event_spans[parent.event_id],
                    attributes,
                )
                for span in self._handler_spans.get(parent.event_id, {}).values():
                    self._set_span_attributes(span, attributes)
        await super().on_event_result_change(eventbus, event, event_result, status)

    async def on_event_change(self, eventbus, event, status):
        process_event = None
        if event.event_type == "ProcessCompletedEvent":
            current = (
                eventbus.event_history.get(event.event_parent_id)
                if event.event_parent_id
                else None
            )
            while current is not None:
                if current.event_type == "ProcessEvent":
                    process_event = current
                    self._process_completions[current.event_id] = event
                    break
                current = (
                    eventbus.event_history.get(current.event_parent_id)
                    if current.event_parent_id
                    else None
                )
        await super().on_event_change(eventbus, event, status)
        if (
            status == "completed"
            and process_event is not None
            and process_event.event_id in self._completed_events
        ):
            self._finish_completed_event(eventbus, process_event)


def get_release():
    return _sentry.get_client().options["release"] if _sentry is not None else None


def shutdown():
    for middleware in tuple(_bus_middlewares):
        middleware.close()
    if _provider is not None:
        _provider.shutdown()
    if _sentry is not None:
        _sentry.profiler.stop_profiler()
        _sentry.flush()


@contextmanager
def enter_operation(name, *, attributes):
    """Keep worker SQL, logs and errors under the same completed operation."""
    assert _provider is not None and _sentry is not None
    with _sentry.isolation_scope() as scope:
        scope.set_attributes(attributes)
        for key, value in attributes.items():
            scope.set_tag(key, value)
        # The operation ends before the scheduler sleeps. Its children can
        # therefore be navigated immediately instead of waiting for daemon exit.
        with _provider.get_tracer("archivebox").start_as_current_span(
            name,
            attributes=attributes,
        ):
            try:
                yield
            except Exception as error:
                _sentry.capture_exception(error)
                raise


def register_hook_reporting(bus, config):
    """Consume the host bus's existing process streams, without rereading log files."""
    assert _sentry is not None
    sentry = _sentry
    level = getattr(logging, config.SENTRY_LOG_LEVEL)

    def attributes(event):
        return {
            **event_attributes(bus, event),
            "sentry.origin": "auto.log.archivebox.hook",
            "plugin.name": event.plugin_name,
            "hook.name": event.hook_name,
            "abxbus.event_id": event.event_id,
            "abxbus.event_parent_id": event.event_parent_id or "",
            "process.output_dir": event.output_dir,
            **({"hook.process.pid": event.pid} if getattr(event, "pid", 0) else {}),
            **(
                {"hook.process.type": event.process_type}
                if getattr(event, "process_type", "")
                else {}
            ),
            **(
                {"hook.worker.type": event.worker_type}
                if getattr(event, "worker_type", "")
                else {}
            ),
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
                scope.set_attributes(attributes(event))
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
        release = config.SENTRY_RELEASE
        if not release:
            try:
                release = f"archivebox@{version('archivebox')}"
            except PackageNotFoundError:
                release = None

        def current_attributes():
            attributes = getattr(trace_api.get_current_span(), "attributes", {}) or {}
            return {
                key: attributes[key]
                for key in PROCESS_ATTRIBUTES
                if key in attributes
                and (not key.startswith("user.") or config.SENTRY_SEND_DEFAULT_PII)
            }

        def before_send_log(log, hint):
            # Uvicorn logs response delivery after Django has left its isolation
            # scope. The outer ASGI span still owns the request and its identity.
            attributes = log.setdefault("attributes", {})
            for key, value in current_attributes().items():
                attributes.setdefault(key, value)
            return log

        def before_send(event, hint):
            attributes = current_attributes()
            tags = event.setdefault("tags", {})
            for key, value in attributes.items():
                tags.setdefault(key, str(value))
            if config.SENTRY_SEND_DEFAULT_PII:
                for key, field in (
                    ("user.id", "id"),
                    ("user.name", "username"),
                    ("user.email", "email"),
                ):
                    if key in attributes:
                        event.setdefault("user", {}).setdefault(field, attributes[key])
            return event

        sentry.init(
            dsn=config.SENTRY_DSN.strip(),
            environment=config.SENTRY_ENVIRONMENT,
            release=release,
            integrations=integrations,
            auto_enabling_integrations=False,
            send_default_pii=config.SENTRY_SEND_DEFAULT_PII,
            include_local_variables=False,
            max_request_body_size="never",
            profile_session_sample_rate=config.SENTRY_PROFILE_SESSION_SAMPLE_RATE,
            profile_lifecycle="manual",
            enable_logs=config.SENTRY_ENABLE_LOGS,
            enable_metrics=config.SENTRY_ENABLE_METRICS,
            before_send_log=before_send_log,
            before_send=before_send,
            # Native Django/SQL spans would otherwise shadow the OTel span ID
            # on logs and metrics, even without a Sentry trace sampling rate.
            instrumenter="otel",
        )
        process_attributes = {
            "process.pid": os.getpid(),
            "process.parent_pid": os.getppid(),
            "process.command": Path(sys.argv[0]).name,
            **(
                {"supervisor.process.name": os.environ["SUPERVISOR_PROCESS_NAME"]}
                if os.environ.get("SUPERVISOR_PROCESS_NAME")
                else {}
            ),
        }
        sentry.get_global_scope().set_attributes(process_attributes)
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
                    **process_attributes,
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
                # Resource fields are not indexed as span attributes in Sentry.
                # Copy identity onto each span and retain operation ancestry on
                # SQL/HTTP children without consulting the database again.
                parent = trace_api.get_current_span(parent_context)
                parent_attributes = getattr(parent, "attributes", {}) or {}
                inherited = {
                    **process_attributes,
                    **{
                        key: parent_attributes[key]
                        for key in PROCESS_ATTRIBUTES
                        if key in parent_attributes
                    },
                }
                # start_span already applied the child's explicit attributes.
                # A nested crawl/snapshot may intentionally change ownership;
                # inheritance fills missing values rather than replacing them.
                span.set_attributes(
                    {
                        key: value
                        for key, value in inherited.items()
                        if key not in (span.attributes or {})
                    },
                )
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
                        **{
                            key: span.attributes[key]
                            for key in PROCESS_ATTRIBUTES
                            if key in span.attributes
                        },
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
    return _provider

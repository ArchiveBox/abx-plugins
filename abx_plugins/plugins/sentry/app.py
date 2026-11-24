"""Opt-in Sentry initialization through the host's native Django app registry."""

from pathlib import Path

from django.apps import AppConfig as DjangoAppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.deprecation import MiddlewareMixin
from abx_plugins.plugins.base.utils import load_config


CONFIG_PATH = Path(__file__).with_name("config.json")


class AppConfig(DjangoAppConfig):
    name = "abx_plugins.plugins.sentry"
    label = "abx_sentry"
    default_auto_field = "django.db.models.BigAutoField"

    def render_page_head(self, request, config):
        """Render only for the host's currently enabled machine configuration."""
        resolved = load_config(
            CONFIG_PATH,
            global_config=config,
            environ={},
            hydrate_binaries=False,
            hydrate_user_agent=False,
        )
        if not resolved.SENTRY_ENABLED or not resolved.SENTRY_BROWSER_ENABLED:
            return ""
        from django.template.loader import render_to_string
        from .runtime import get_release

        user = getattr(request, "user", None)
        user_info = None
        if (
            resolved.SENTRY_SEND_DEFAULT_PII
            and user is not None
            and user.is_authenticated
        ):
            user_info = {"id": str(user.pk), "username": user.get_username()}
            if getattr(user, "email", None):
                user_info["email"] = user.email
        return render_to_string(
            "sentry/head.html",
            {
                "sentry_config": {
                    "dsn": resolved.SENTRY_DSN,
                    "environment": resolved.SENTRY_ENVIRONMENT,
                    "release": get_release(),
                    "sendDefaultPii": resolved.SENTRY_SEND_DEFAULT_PII,
                    "tracesSampleRate": resolved.SENTRY_TRACES_SAMPLE_RATE,
                    "enableLogs": resolved.SENTRY_ENABLE_LOGS,
                    "enableMetrics": resolved.SENTRY_ENABLE_METRICS,
                    "user": user_info,
                },
            },
            request=request,
        )

    def ready(self):
        host_config = getattr(settings, "CONFIG", None)
        config = load_config(
            CONFIG_PATH,
            global_config=host_config.model_dump(mode="json")
            if host_config is not None
            else {},
            environ={},
            hydrate_binaries=False,
            hydrate_user_agent=False,
        )
        if not config.SENTRY_ENABLED:
            return
        from .runtime import initialize, shutdown

        try:
            initialize(config)
            settings.MIDDLEWARE = [
                *settings.MIDDLEWARE,
                "abx_plugins.plugins.sentry.app.RequestAttributesMiddleware",
            ]
            self._shutdown = shutdown
        except (ValueError, RuntimeError) as error:
            raise ImproperlyConfigured(str(error)) from error

    def shutdown(self):
        close = getattr(self, "_shutdown", None)
        if close is not None:
            close()


def enter_operation(name, config, *, attributes):
    """Initialize optional telemetry only for an enabled host operation."""
    from contextlib import nullcontext

    resolved = load_config(
        CONFIG_PATH,
        global_config=config,
        environ={},
        hydrate_binaries=False,
        hydrate_user_agent=False,
    )
    if not resolved.SENTRY_ENABLED:
        return nullcontext()
    from .runtime import initialize, enter_operation as operation

    initialize(resolved)
    return operation(name, attributes=attributes)


def register_eventbus(bus, config):
    """ArchiveBox offers the bus; this plugin decides whether to attach tracing."""
    resolved = load_config(
        CONFIG_PATH,
        global_config=config,
        environ={},
        hydrate_binaries=False,
        hydrate_user_agent=False,
    )
    if not resolved.SENTRY_ENABLED:
        return
    from .runtime import initialize, register_hook_reporting, OtelTracingMiddleware

    provider = initialize(resolved)
    assert provider is not None
    bus.middlewares.append(OtelTracingMiddleware(tracer=provider.get_tracer("abxbus")))
    register_hook_reporting(bus, resolved)


def wrap_asgi(application, config):
    """Own request tracing through response delivery, including cancellation."""
    resolved = load_config(
        CONFIG_PATH,
        global_config=config,
        environ={},
        hydrate_binaries=False,
        hydrate_user_agent=False,
    )
    if not resolved.SENTRY_ENABLED:
        return application
    from .runtime import initialize
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    def server_request_hook(span, scope):
        from sentry_sdk import get_global_scope

        # The host supplies its already registered Process identity. It is
        # constant for this worker; never look up database rows in the exporter.
        attributes = scope.get("archivebox.process", {})
        span.set_attributes(attributes)
        get_global_scope().set_attributes(attributes)

    return OpenTelemetryMiddleware(
        application,
        tracer_provider=initialize(resolved),
        server_request_hook=server_request_hook,
        # Resolved Django routes replace this low-cardinality initial name.
        default_span_details=lambda scope: (scope.get("method", scope["type"]), {}),
        # The server span covers transport waits without a child per body chunk.
        exclude_spans=["receive", "send"],
    )


class RequestAttributesMiddleware(MiddlewareMixin):
    @staticmethod
    def _set_attributes(request):
        from opentelemetry.trace import get_current_span
        from sentry_sdk import get_client, get_isolation_scope

        attributes = {}
        # Replay views already resolved this object. Inspect loaded fields only:
        # monitoring must not fetch deferred columns or a related Crawl.
        snapshot = request.__dict__.get("archivebox_snapshot")
        if snapshot is not None:
            fields = vars(snapshot)
            for field, key in (
                ("id", "snapshot.id"),
                ("crawl_id", "crawl.id"),
                ("url", "snapshot.url"),
                ("title", "snapshot.title"),
            ):
                if fields.get(field) is not None:
                    attributes[key] = str(fields[field])
        if get_client().options["send_default_pii"]:
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated:
                attributes["user.id"] = str(user.pk)
                attributes["user.name"] = user.get_username()
                if getattr(user, "email", None):
                    attributes["user.email"] = user.email
        get_current_span().set_attributes(attributes)
        get_isolation_scope().set_attributes(attributes)

    def process_view(self, request, view_func, view_args, view_kwargs):
        from opentelemetry.trace import get_current_span

        span = get_current_span()
        if span.is_recording() and request.resolver_match is not None:
            route = request.resolver_match.route
            span.set_attribute("http.route", route)
            span.update_name(f"{request.method} {route}")
        # Child SQL and deferred threads inherit identity before the view runs.
        self._set_attributes(request)

    def process_exception(self, request, exception):
        self._set_attributes(request)

    def process_response(self, request, response):
        # API authentication and replay resolution may happen inside the view.
        self._set_attributes(request)
        return response

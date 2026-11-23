"""Opt-in Sentry initialization through the host's native Django app registry."""

from pathlib import Path

from django.apps import AppConfig as DjangoAppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
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
            hydrate_binaries=False,
            hydrate_user_agent=False,
        )
        if not resolved.SENTRY_ENABLED or not resolved.SENTRY_BROWSER_ENABLED:
            return ""
        from django.template.loader import render_to_string

        return render_to_string(
            "sentry/head.html",
            {
                "sentry_config": {
                    "dsn": resolved.SENTRY_DSN,
                    "environment": resolved.SENTRY_ENVIRONMENT,
                    "release": resolved.SENTRY_RELEASE or None,
                    "sendDefaultPii": resolved.SENTRY_SEND_DEFAULT_PII,
                    "tracesSampleRate": resolved.SENTRY_TRACES_SAMPLE_RATE,
                    "enableLogs": resolved.SENTRY_ENABLE_LOGS,
                    "enableMetrics": resolved.SENTRY_ENABLE_METRICS,
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
            hydrate_binaries=False,
            hydrate_user_agent=False,
        )
        if not config.SENTRY_ENABLED:
            return
        from .runtime import initialize

        try:
            initialize(config)
        except (ValueError, RuntimeError) as error:
            raise ImproperlyConfigured(str(error)) from error


def register_eventbus(bus, config):
    """ArchiveBox offers the bus; this plugin decides whether to attach tracing."""
    resolved = load_config(
        CONFIG_PATH,
        global_config=config,
        hydrate_binaries=False,
        hydrate_user_agent=False,
    )
    if not resolved.SENTRY_ENABLED:
        return
    from .runtime import initialize
    from abxbus.middlewares import OtelTracingMiddleware

    provider = initialize(resolved)
    assert provider is not None
    bus.middlewares.append(OtelTracingMiddleware(tracer=provider.get_tracer("abxbus")))

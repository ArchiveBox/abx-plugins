"""Optional Django app, routes, WebSockets, and CLI commands."""

from django.apps import AppConfig as DjangoAppConfig


class AppConfig(DjangoAppConfig):
    name = "abx_plugins.plugins.opencode"
    label = "abx_opencode"
    default_auto_field = "django.db.models.BigAutoField"


def get_cli_commands(host):
    from .archivebox.browser import get_cli_command as browser_command
    from .archivebox.context import get_cli_command as context_command

    return [browser_command(host), context_command(host)]


def get_urlpatterns(host):
    from .archivebox.urls import get_urlpatterns

    return get_urlpatterns(host)


def get_websocket_views(host):
    from functools import partial
    from .archivebox.views import websocket_view

    return {"opencode-proxy": partial(websocket_view, host=host)}

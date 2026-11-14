from functools import partial, update_wrapper

from django.urls import include, path, re_path

from .views import (
    agent_view,
    agent_browser_view,
    opencode_proxy_view,
    capture_task_view,
    create_custom,
)


def get_urlpatterns(host):
    def bind(view):
        return update_wrapper(partial(view, host=host), view)

    agent_patterns = [
        path("tasks/", bind(capture_task_view), name="opencode-capture-task"),
        path("", bind(agent_view), name="opencode-agent"),
        path("browser/<str:action>", bind(agent_browser_view), name="opencode-browser"),
        re_path(
            r"^opencode(?:/(?P<path>.*))?$",
            bind(opencode_proxy_view),
            name="opencode-proxy",
        ),
    ]

    return [
        path(
            "admin/importers/custom/",
            bind(create_custom),
            name="opencode-create-importer",
        ),
        path(
            "api/v1/agent/tasks/",
            bind(capture_task_view),
            name="opencode-capture-task-api",
        ),
        path("admin/agent/", include(agent_patterns)),
        path("admin/agent", bind(agent_view)),
        re_path(
            r"^(?P<path>assets/.*)$",
            bind(opencode_proxy_view),
            name="opencode-assets",
        ),
    ]

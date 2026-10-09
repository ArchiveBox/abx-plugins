from django.urls import include, path, re_path

from .views import agent_view, opencode_proxy_view, capture_task_view


agent_patterns = [
    path("tasks/", capture_task_view, name="opencode-capture-task"),
    path("", agent_view, name="opencode-agent"),
    re_path(
        r"^opencode(?:/(?P<path>.*))?$",
        opencode_proxy_view,
        name="opencode-proxy",
    ),
]

urlpatterns = [
    path("api/v1/agent/tasks/", capture_task_view, name="opencode-capture-task-api"),
    re_path(r"^admin/agent/?(?=$|opencode|tasks/)", include(agent_patterns)),
    re_path(r"^(?P<path>assets/.*)$", opencode_proxy_view, name="opencode-assets"),
]

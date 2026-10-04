from django.urls import include, path, re_path

from .views import agent_view, opencode_proxy_view


agent_patterns = [
    path("", agent_view, name="opencode-agent"),
    re_path(
        r"^opencode(?:/(?P<path>.*))?$",
        opencode_proxy_view,
        name="opencode-proxy",
    ),
]

urlpatterns = [
    re_path(r"^admin/agent/?(?=$|opencode)", include(agent_patterns)),
    re_path(r"^(?P<path>assets/.*)$", opencode_proxy_view, name="opencode-assets"),
]

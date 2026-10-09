from django.urls import include, path, re_path

from .views import agent_view, agent_browser_view, opencode_proxy_view


agent_patterns = [
    path("", agent_view, name="opencode-agent"),
    path("browser/<str:action>", agent_browser_view, name="opencode-browser"),
    re_path(
        r"^opencode(?:/(?P<path>.*))?$",
        opencode_proxy_view,
        name="opencode-proxy",
    ),
]

urlpatterns = [
    path("admin/agent/", include(agent_patterns)),
    path("admin/agent", agent_view),
    re_path(r"^(?P<path>assets/.*)$", opencode_proxy_view, name="opencode-assets"),
]

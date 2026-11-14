"""Optional ArchiveBox routes, WebSockets, and gallery setup."""

import os
from pathlib import Path

import requests


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


def configure_gallery(model, config, data_dir):
    """Use the collection's real OpenCode server and its protected credential store."""
    from abx_plugins.plugins.opencode import runtime

    collection = Path(data_dir).resolve()
    if not config.get("OPENCODE_ENABLED"):
        return False
    settings = runtime._settings(config, collection)
    if settings["host"] not in {"127.0.0.1", "localhost", "::1"} or any(
        not settings[key].resolve().is_relative_to(collection)
        for key in ("opencode_dir", "workdir")
    ):
        raise ValueError(
            "AI gallery authentication requires a local OpenCode server scoped to its screenshot collection.",
        )
    ok, error = runtime._ensure_opencode(settings)
    if not ok:
        raise RuntimeError(error)
    auth = runtime._server_auth(settings)
    if api_key := os.environ.get("OPENAI_API_KEY"):
        response = requests.put(
            f"{settings['origin']}/auth/openai",
            json={"type": "api", "key": api_key},
            auth=auth,
            timeout=30,
        )
        response.raise_for_status()
        credential_store = settings["data_home"] / "opencode" / "auth.json"
        if credential_store.exists():
            credential_store.chmod(0o600)
    response = requests.get(f"{settings['origin']}/provider", auth=auth, timeout=30)
    response.raise_for_status()
    providers = response.json()
    provider = next(
        (
            provider
            for provider in providers.get("all", [])
            if provider.get("id") == "openai"
        ),
        {},
    )
    if "openai" not in providers.get("connected", []) or model.removeprefix(
        "openai/",
    ) not in provider.get("models", {}):
        raise RuntimeError(
            "The requested OpenAI model is unavailable. Configure OpenAI in this collection or supply OPENAI_API_KEY.",
        )

    return True

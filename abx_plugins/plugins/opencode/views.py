from __future__ import annotations

import base64
import importlib
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin

import httpx
import requests
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    StreamingHttpResponse,
)
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt


_PROCESS: subprocess.Popen | None = None
_PROCESS_LOCK = threading.Lock()
_PROXY_PREFIX = "/admin/agent/opencode"
_PROXY_PREFIX_REGEX = _PROXY_PREFIX.replace("/", r"\/")
_PROXY_PREFIX_NO_SLASH_REGEX = _PROXY_PREFIX.lstrip("/").replace("/", r"\/")

_TEXT_CONTENT_TYPES = (
    "text/",
    "application/javascript",
    "application/json",
    "application/x-javascript",
)
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_ARCHIVEBOX_SKILL = """---
name: archivebox
description: Use ArchiveBox's CLI and local REST API from an ArchiveBox collection.
---

You are running inside an ArchiveBox collection directory.

- ArchiveBox collection directory: {archivebox_data_dir}
- ArchiveBox BASE_URL: {archivebox_base_url}
- ArchiveBox Admin URL: {archivebox_admin_url}
- ArchiveBox REST API URL: {archivebox_api_url}
- Prefer the `archivebox` CLI for authenticated changes, e.g. `archivebox add`, `archivebox schedule`, `archivebox update`, and `archivebox shell`.
- Run ArchiveBox CLI commands from the ArchiveBox collection directory above.
- Get command help with `archivebox list --help`, `archivebox add --help`, `archivebox schedule --help`, etc. Do not use `archivebox help <command>`.
- Use `archivebox shell -c '...'` or `archivebox shell <<'PY' ... PY` for Django ORM work. Shell Plus prints an import banner first; keep stderr visible while debugging.
- Use full ArchiveBox module paths in shell code: `from archivebox.crawls.models import Crawl, CrawlSchedule` and `from archivebox.core.models import Snapshot, ArchiveResult`.
- If a model/field/relation is unclear, inspect `_meta.fields` before guessing, e.g. `archivebox shell -c "from archivebox.crawls.models import Crawl; print([f.name for f in Crawl._meta.fields])"`.
- Use `archivebox config --get BASE_URL` only to verify the configured base URL; prefer the seeded URLs above for API/admin requests.
- Use `$ARCHIVEBOX_API_URL` for REST API inspection when helpful. Do not assume admin session cookies authenticate API subdomain requests; prefer CLI/shell for authenticated mutations unless the admin provides or asks you to create an API token.
- Discover REST endpoints from `${{ARCHIVEBOX_API_URL}}v1/openapi.json`; crawl endpoints live under `/api/v1/crawls/`, snapshots under `/api/v1/core/`.
- Do not bypass ArchiveBox auth, expose API keys, or modify config unless the admin explicitly asks.
- After creating crawls or snapshots, report the crawl/snapshot IDs and the exact command or API request used.
"""


def _machine_config() -> dict[str, Any]:
    machine_models = importlib.import_module("archivebox.machine.models")
    Machine = machine_models.Machine
    config = Machine.current().config
    if not isinstance(config, Mapping):
        return {}
    config_map = cast(Mapping[str, Any], config)
    return {key: value for key, value in config_map.items()}


def _archivebox_data_dir_default() -> Path:
    try:
        archivebox_config = importlib.import_module("archivebox.config")
        return Path(getattr(archivebox_config.CONSTANTS, "DATA_DIR", Path.cwd()))
    except Exception:
        return Path.cwd()


def _archivebox_route_urls(request: HttpRequest, route_config) -> tuple[str, str, str]:
    routes_util = importlib.import_module("archivebox.core.routes_util")
    base_url = routes_util.get_base_url(
        request=request,
        config=route_config,
    ).rstrip("/")
    admin_url = routes_util.build_admin_url(
        "/admin/",
        request=request,
        config=route_config,
    ).rstrip("/")
    api_url = f"{routes_util.get_api_base_url(request=request, config=route_config).rstrip('/')}/api/"
    return base_url, admin_url, api_url


def _config_value(config: dict, key: str, default):
    value = config.get(key, default)
    if value in (None, ""):
        return default
    return value


def _opencode_enabled(config: dict) -> bool:
    value = config.get("OPENCODE_ENABLED", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _require_enabled(config: dict) -> None:
    if not _opencode_enabled(config):
        raise Http404


def _require_superuser(request: HttpRequest):
    user = getattr(request, "user", None)
    if user is None:
        return redirect(f"/admin/login/?next={request.get_full_path()}")
    if (
        bool(getattr(user, "is_authenticated", False))
        and bool(getattr(user, "is_active", False))
        and bool(getattr(user, "is_superuser", False))
    ):
        return None
    if bool(getattr(user, "is_authenticated", False)):
        return HttpResponseForbidden(
            b"ArchiveBox agent access requires a superuser account.",
        )
    return redirect(f"/admin/login/?next={request.get_full_path()}")


def _origin_allowed(request: HttpRequest, path: str | None = None) -> bool:
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return True

    expected = f"{request.scheme}://{request.get_host()}"
    pty_connect = bool(
        path and path.startswith("pty/") and path.endswith("/connect-token"),
    )
    if pty_connect:
        return True

    origin = _request_header(request, "Origin")
    if origin:
        return origin == expected

    referer = _request_header(request, "Referer")
    if referer:
        return referer.startswith(f"{expected}/")

    fetch_site = _request_header(request, "Sec-Fetch-Site")
    if fetch_site:
        return fetch_site in {"same-origin", "same-site", "none"}

    return False


def _settings(config: dict) -> dict:
    host = str(_config_value(config, "OPENCODE_HOST", "127.0.0.1"))
    port = int(_config_value(config, "OPENCODE_PORT", 4096))
    default_data_dir = _archivebox_data_dir_default()
    workdir = Path(
        str(_config_value(config, "OPENCODE_WORKDIR", default_data_dir)),
    ).expanduser()
    opencode_dir = Path(
        str(_config_value(config, "OPENCODE_STATE_DIR", workdir / "opencode")),
    ).expanduser()
    binary = str(_config_value(config, "OPENCODE_BINARY", "opencode"))
    timeout = int(_config_value(config, "OPENCODE_TIMEOUT", 30))
    return {
        "host": host,
        "port": port,
        "origin": f"http://{host}:{port}",
        "workdir": workdir,
        "opencode_dir": opencode_dir,
        "config_home": opencode_dir / "config",
        "data_home": opencode_dir / "data",
        "state_home": opencode_dir / "state",
        "cache_home": opencode_dir / "cache",
        "home": opencode_dir / "home",
        "binary": binary,
        "timeout": timeout,
    }


def _project_route(workdir: Path) -> str:
    encoded = base64.b64encode(str(workdir.resolve()).encode()).decode()
    encoded = encoded.replace("+", "-").replace("/", "_").rstrip("=")
    return f"{_PROXY_PREFIX}/{encoded}/session"


def _ensure_project_files(settings: dict) -> None:
    workdir = settings["workdir"].resolve()
    git_dir = workdir / ".git"
    if not git_dir.exists():
        git_dir.mkdir()
    if git_dir.is_dir():
        git_marker = git_dir / "not-a-git"
        if not git_marker.exists():
            git_marker.write_text(
                "ArchiveBox marker so OpenCode treats DATA_DIR as the project root.\n",
            )

    skill_path = (
        settings["config_home"] / "opencode" / "skills" / "archivebox" / "SKILL.md"
    )
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill = _ARCHIVEBOX_SKILL.format(
        archivebox_data_dir=workdir,
        archivebox_base_url=settings.get("archivebox_base_url", ""),
        archivebox_admin_url=settings.get("archivebox_admin_url", ""),
        archivebox_api_url=settings.get("archivebox_api_url", ""),
    )
    if not skill_path.exists() or skill_path.read_text() != skill:
        skill_path.write_text(skill)


def _ensure_default_session(settings: dict) -> None:
    workdir = str(settings["workdir"].resolve())
    params = {"directory": workdir}
    timeout = settings["timeout"]
    try:
        requests.get(
            f"{settings['origin']}/project/current",
            params=params,
            timeout=timeout,
        ).raise_for_status()
        sessions = requests.get(
            f"{settings['origin']}/session",
            params={**params, "roots": "true", "limit": 55},
            timeout=timeout,
        )
        sessions.raise_for_status()
        if not sessions.json():
            requests.post(
                f"{settings['origin']}/session",
                params=params,
                json={},
                timeout=timeout,
            ).raise_for_status()
    except requests.RequestException:
        pass


def _recent_session_id(settings: dict) -> str:
    workdir = str(settings["workdir"].resolve())
    try:
        response = requests.get(
            f"{settings['origin']}/session",
            params={"directory": workdir, "roots": "true", "limit": 1},
            timeout=settings["timeout"],
        )
        response.raise_for_status()
        sessions = response.json()
        if sessions:
            return str(sessions[0].get("id") or "")
    except requests.RequestException:
        pass
    return ""


def _health(settings: dict) -> bool:
    try:
        response = requests.get(
            f"{settings['origin']}/global/health",
            timeout=2,
        )
        return response.status_code == 200
    except requests.RequestException:
        return False


def _ensure_opencode(settings: dict) -> tuple[bool, str]:
    global _PROCESS
    settings["workdir"].mkdir(parents=True, exist_ok=True)
    settings["config_home"].mkdir(parents=True, exist_ok=True)
    settings["data_home"].mkdir(parents=True, exist_ok=True)
    settings["state_home"].mkdir(parents=True, exist_ok=True)
    settings["cache_home"].mkdir(parents=True, exist_ok=True)
    settings["home"].mkdir(parents=True, exist_ok=True)
    _ensure_project_files(settings)
    if _health(settings):
        _ensure_default_session(settings)
        return True, ""

    with _PROCESS_LOCK:
        if _health(settings):
            return True, ""

        workdir = settings["workdir"].resolve()
        binary = shutil.which(settings["binary"]) or settings["binary"]
        if not shutil.which(binary) and not Path(binary).exists():
            return False, f"OpenCode binary not found: {settings['binary']}"

        env = {
            **os.environ,
            "ARCHIVEBOX_BASE_URL": str(settings.get("archivebox_base_url", "")),
            "ARCHIVEBOX_ADMIN_URL": str(settings.get("archivebox_admin_url", "")),
            "ARCHIVEBOX_API_URL": str(settings.get("archivebox_api_url", "")),
            "BROWSER": "false",
            "GIT_CEILING_DIRECTORIES": str(workdir),
            "HOME": str(settings["home"]),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
            "XDG_CONFIG_HOME": str(settings["config_home"]),
            "XDG_DATA_HOME": str(settings["data_home"]),
            "XDG_STATE_HOME": str(settings["state_home"]),
            "XDG_CACHE_HOME": str(settings["cache_home"]),
        }
        cmd = [
            binary,
            "serve",
            "--hostname",
            settings["host"],
            "--port",
            str(settings["port"]),
        ]
        _PROCESS = subprocess.Popen(
            cmd,
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    deadline = time.monotonic() + settings["timeout"]
    while time.monotonic() < deadline:
        if _health(settings):
            _ensure_default_session(settings)
            return True, ""
        if _PROCESS and _PROCESS.poll() is not None:
            return False, "OpenCode exited before the web server became ready."
        time.sleep(0.25)

    return False, "Timed out waiting for OpenCode to start."


def agent_view(request: HttpRequest):
    config = _machine_config()
    _require_enabled(config)
    auth_response = _require_superuser(request)
    if auth_response:
        return auth_response

    settings = _settings(config)
    route_config = request.__dict__.get("archivebox_config")
    base_url, admin_url, api_url = _archivebox_route_urls(request, route_config)
    settings["archivebox_base_url"] = base_url
    settings["archivebox_admin_url"] = admin_url
    settings["archivebox_api_url"] = api_url
    ok, error = _ensure_opencode(settings)
    archivebox_admin = importlib.import_module(
        "archivebox.core.admin_site",
    ).archivebox_admin

    context = {
        **archivebox_admin.each_context(request),
        "title": "Agent",
        "error": "" if ok else error,
        "command": f"{settings['binary']} serve --hostname {settings['host']} --port {settings['port']}"
        if error
        else "",
        "proxy_url": _project_route(settings["workdir"]),
        "workdir": str(settings["workdir"].resolve()),
        "recent_session_id": _recent_session_id(settings) if ok else "",
    }
    return render(
        request,
        "opencode/agent.html",
        context,
        status=200 if ok else 502,
    )


def _proxy_url(settings: dict, path: str | None) -> str:
    rel = "/" if not path else f"/{path}"
    return urljoin(settings["origin"], rel)


def _request_header(request: HttpRequest, name: str) -> str | None:
    if name == "Content-Type":
        value = request.META.get("CONTENT_TYPE")
    elif name == "Content-Length":
        value = request.META.get("CONTENT_LENGTH")
    else:
        value = request.META.get(f"HTTP_{name.upper().replace('-', '_')}")
    return str(value) if value else None


def _request_headers(request: HttpRequest, settings: dict) -> dict[str, str]:
    forwarded = {}
    for key in ("Accept", "Accept-Language", "Content-Type", "Range", "User-Agent"):
        value = _request_header(request, key)
        if value:
            forwarded[key] = value
    return forwarded


def _request_params(request: HttpRequest) -> tuple[tuple[str, str], ...]:
    return tuple(
        (key, str(value)) for key, values in request.GET.lists() for value in values
    )


async def _event_chunks(request: HttpRequest, settings: dict, path: str | None):
    timeout = httpx.Timeout(settings["timeout"], read=None)
    url = _proxy_url(settings, path)
    method = request.method or "GET"
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        async with client.stream(
            method,
            url,
            params=_request_params(request),
            headers=_request_headers(request, settings),
        ) as upstream:
            async for chunk in upstream.aiter_raw(chunk_size=512):
                yield chunk


def _rewrite_text(body: bytes, settings: dict) -> bytes:
    text = body.decode("utf-8", errors="replace")
    text = text.replace(settings["origin"], _PROXY_PREFIX)
    text = text.replace("location.origin", f'location.origin+"{_PROXY_PREFIX}"')
    text = text.replace(
        "k(k5,{get component(){return t.router??Az},",
        f'k(k5,{{base:"{_PROXY_PREFIX}",get component(){{return t.router??Az}},',
    )
    text = text.replace('"/assets/', f'"{_PROXY_PREFIX}/assets/')
    text = text.replace("'/assets/", f"'{_PROXY_PREFIX}/assets/")
    proxy_path = rf'(\1.replace(/^{_PROXY_PREFIX_REGEX}(?=\/|$)/,"")||"/")'
    text = re.sub(r"\b(window\.location\.pathname)\b", proxy_path, text)
    text = re.sub(r"(?<![.\w])(location\.pathname)\b", proxy_path, text)
    text = text.replace(
        'window.history.replaceState(nz(o),"",r):window.history.pushState(o,"",r)',
        (
            f'window.history.replaceState(nz(o),"",r.startsWith("{_PROXY_PREFIX}")?r:r.startsWith("/")?"{_PROXY_PREFIX}"+r:r):'
            f'window.history.pushState(o,"",r.startsWith("{_PROXY_PREFIX}")?r:r.startsWith("/")?"{_PROXY_PREFIX}"+r:r)'
        ),
    )
    text = text.replace(
        'const BL="modulepreload",UL=function(t){return"/"+t}',
        f'const BL="modulepreload",UL=function(t){{return"{_PROXY_PREFIX}/"+t}}',
    )
    text = re.sub(
        rf"""(?P<prefix>\b(?:href|src|action)=["'])/(?!{_PROXY_PREFIX_NO_SLASH_REGEX}(?:/|$))""",
        rf"\g<prefix>{_PROXY_PREFIX}/",
        text,
    )
    text = re.sub(
        rf"""(?P<prefix>\b(?:fetch|EventSource)\(["'])/(?!{_PROXY_PREFIX_NO_SLASH_REGEX}(?:/|$))""",
        rf"\g<prefix>{_PROXY_PREFIX}/",
        text,
    )
    text = re.sub(
        rf"""(?P<prefix>\burl\(["']?)/(?!{_PROXY_PREFIX_NO_SLASH_REGEX}(?:/|$))""",
        rf"\g<prefix>{_PROXY_PREFIX}/",
        text,
    )
    return text.encode("utf-8")


def _response_headers(upstream: requests.Response, settings: dict) -> dict[str, str]:
    headers = {}
    for key, value in upstream.headers.items():
        lower = key.lower()
        if lower in _HOP_BY_HOP_HEADERS or lower in {
            "content-length",
            "content-encoding",
            "x-frame-options",
        }:
            continue
        if lower == "location":
            if value.startswith(settings["origin"]):
                value = value.replace(settings["origin"], _PROXY_PREFIX, 1)
            elif value.startswith("/"):
                value = f"{_PROXY_PREFIX}{value}"
        headers[key] = value
    return headers


@csrf_exempt
def opencode_proxy_view(request: HttpRequest, path: str | None = None):
    config = _machine_config()
    _require_enabled(config)
    auth_response = _require_superuser(request)
    if auth_response:
        return auth_response
    if not _origin_allowed(request, path):
        return HttpResponseForbidden(
            b"Cross-origin OpenCode agent requests are blocked.",
        )

    settings = _settings(config)
    route_config = request.__dict__.get("archivebox_config")
    base_url, admin_url, api_url = _archivebox_route_urls(request, route_config)
    settings["archivebox_base_url"] = base_url
    settings["archivebox_admin_url"] = admin_url
    settings["archivebox_api_url"] = api_url
    ok, error = _ensure_opencode(settings)
    if not ok:
        return HttpResponse(
            error.encode(),
            status=502,
            content_type="text/plain; charset=utf-8",
        )

    if request.method == "GET" and (path or "").endswith("/event"):
        response = StreamingHttpResponse(
            _event_chunks(request, settings, path),
            content_type="text/event-stream",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    try:
        method = request.method or "GET"
        upstream = requests.request(
            method,
            _proxy_url(settings, path),
            params=_request_params(request),
            data=request.body if method not in {"GET", "HEAD"} else None,
            headers=_request_headers(request, settings),
            stream=True,
            timeout=(settings["timeout"], None),
            allow_redirects=False,
        )
    except requests.RequestException as err:
        return HttpResponse(
            str(err).encode(),
            status=502,
            content_type="text/plain; charset=utf-8",
        )

    content_type = upstream.headers.get("Content-Type", "")
    is_event_stream = content_type.startswith("text/event-stream")
    is_text = not is_event_stream and any(
        content_type.startswith(prefix) for prefix in _TEXT_CONTENT_TYPES
    )
    headers = _response_headers(upstream, settings)
    if is_text:
        body = _rewrite_text(upstream.content, settings)
        response = HttpResponse(
            body,
            status=upstream.status_code,
            content_type=content_type or "text/plain; charset=utf-8",
        )
    elif is_event_stream:
        response = StreamingHttpResponse(
            upstream.iter_lines(chunk_size=1),
            status=upstream.status_code,
            content_type=content_type or "text/event-stream",
        )
    else:
        response = StreamingHttpResponse(
            upstream.iter_content(chunk_size=64 * 1024),
            status=upstream.status_code,
            content_type=content_type or "application/octet-stream",
        )
    for key, value in headers.items():
        response.headers[key] = value
    response.headers["Cache-Control"] = "no-store"
    return response

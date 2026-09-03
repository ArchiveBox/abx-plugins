from __future__ import annotations

import asyncio
import atexit
import base64
import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import requests

_PROCESS: subprocess.Popen | None = None
_PROCESS_READY: subprocess.Popen | None = None
_PROCESS_LOCK = threading.Lock()
_SESSION_LOCK = threading.Lock()
_LOGGER = logging.getLogger(__name__)
_PROXY_PREFIX = "/admin/agent/opencode"
_PROXY_PREFIX_NO_SLASH_REGEX = _PROXY_PREFIX.lstrip("/").replace("/", r"\/")
_CONFIG_PATH = Path(__file__).with_name("config.json")
_DEFAULT_MODEL = "opencode/big-pickle"
_DEFAULT_CONFIG = f'''{{
  "$schema": "https://opencode.ai/config.json",
  "model": "{_DEFAULT_MODEL}",
  "snapshot": false
}}
'''

_TEXT_CONTENT_TYPES = (
    "text/",
    "application/javascript",
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
- Use `--depth=0` by default. Only use recursive crawling when the user explicitly asks for it; use `--depth=1` when you need pages one hop out.
- Before any recursive crawl, constrain scope with ArchiveBox config such as `CRAWL_MAX_URLS`, `CRAWL_MAX_SIZE`, `SNAPSHOT_MAX_*`, `URL_ALLOWLIST`, `URL_DENYLIST`, and related limits.
- Respect the configured `archivebox config --get ONLY_NEW` behavior unless the user explicitly says otherwise. Remind users that expected crawl URLs can be skipped when the collection already contains snapshots with the same URL.
- Always audit newly discovered crawl URLs before letting a crawl run broadly. Treat junk URLs such as privacy policies, legal pages, tag archives, sitemap files, feeds, login/logout URLs, and other low-value boilerplate as unwanted unless the user explicitly asked to archive them.
- Always watch crawl output and logs as the crawl progresses, and correct errors early instead of waiting until the crawl finishes.
- If a crawl contains bad URLs, pause it, edit the crawl's `urls` field to remove them, delete any unneeded snapshots already created under that crawl, then resume the crawl.
- Use `archivebox shell -c '...'` or `archivebox shell <<'PY' ... PY` for Django ORM work. Shell Plus prints an import banner first; keep stderr visible while debugging.
- Use full ArchiveBox module paths in shell code: `from archivebox.crawls.models import Crawl, CrawlSchedule` and `from archivebox.core.models import Snapshot, ArchiveResult`.
- If a model/field/relation is unclear, inspect `_meta.fields` before guessing, e.g. `archivebox shell -c "from archivebox.crawls.models import Crawl; print([f.name for f in Crawl._meta.fields])"`.
- Use `archivebox config --get BASE_URL` only to verify the configured base URL; prefer the seeded URLs above for API/admin requests.
- Use `$ARCHIVEBOX_API_URL` for REST API inspection when helpful. Do not assume admin session cookies authenticate API subdomain requests; prefer CLI/shell for authenticated mutations unless the admin provides or asks you to create an API token.
- Discover REST endpoints from `${{ARCHIVEBOX_API_URL}}v1/openapi.json`; crawl endpoints live under `/api/v1/crawls/`, snapshots under `/api/v1/core/`.
- Do not bypass ArchiveBox auth, expose API keys, or modify config unless the admin explicitly asks.
- After creating crawls or snapshots, report the crawl/snapshot IDs and the exact command or API request used.
"""


def _signal_owned_process(process: subprocess.Popen, sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except OSError:
        try:
            process.send_signal(sig)
        except ProcessLookupError:
            pass


def _stop_owned_process(process: subprocess.Popen | None = None) -> None:
    global _PROCESS, _PROCESS_READY
    owned_process = process or _PROCESS
    if owned_process is None:
        return
    if owned_process.poll() is None:
        _signal_owned_process(owned_process, signal.SIGCONT)
        _signal_owned_process(owned_process, signal.SIGTERM)
        try:
            owned_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _signal_owned_process(owned_process, signal.SIGKILL)
            owned_process.wait()
    if _PROCESS is owned_process:
        _PROCESS = None
    if _PROCESS_READY is owned_process:
        _PROCESS_READY = None


def _config_value(config: dict, key: str, default):
    value = config.get(key, default)
    if value in (None, ""):
        return default
    return value


def _origin_allowed(method: str, expected_host: str, headers) -> bool:
    """Called by the host's authenticated adapter before invoking proxy()."""
    if method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return True

    origin = headers.get("Origin")
    if origin:
        return _same_host(origin, expected_host)

    referer = headers.get("Referer")
    if referer:
        return _same_host(referer, expected_host)

    fetch_site = headers.get("Sec-Fetch-Site")
    if fetch_site:
        return fetch_site in {"same-origin", "same-site", "none"}

    return False


def _same_host(value: str, expected_host: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and parsed.netloc == expected_host


def _settings(config: dict, data_dir: Path | None = None) -> dict:
    host = str(_config_value(config, "OPENCODE_HOST", "127.0.0.1"))
    port = int(_config_value(config, "OPENCODE_PORT", 4096))
    default_data_dir = Path(
        data_dir or config.get("DATA_DIR") or os.environ.get("DATA_DIR") or ".",
    ).resolve()
    opencode_dir = Path(
        str(_config_value(config, "OPENCODE_STATE_DIR", default_data_dir / "opencode")),
    ).expanduser()
    workdir = Path(
        str(_config_value(config, "OPENCODE_WORKDIR", default_data_dir)),
    ).expanduser()
    binary = str(_config_value(config, "OPENCODE_BINARY", "opencode"))
    timeout = int(_config_value(config, "OPENCODE_TIMEOUT", 120))
    return {
        "host": host,
        "port": port,
        "origin": f"http://{host}:{port}",
        "archivebox_data_dir": default_data_dir,
        "workdir": workdir,
        "opencode_dir": opencode_dir,
        "config_home": opencode_dir / "config",
        "data_home": opencode_dir / "data",
        "state_home": opencode_dir / "state",
        "cache_home": opencode_dir / "cache",
        "home": opencode_dir / "home",
        "binary": binary,
        "config": config,
        "timeout": timeout,
    }


def _resolve_binary(binary: str, config: dict) -> tuple[Any, dict[str, str]]:
    try:
        from abxpkg import BinProvider
        from abx_plugins.plugins.base.utils import load_required_binary_from_config

        binary_environ = os.environ.copy()
        lib_dir = config.get("ABXPKG_LIB_DIR")
        if lib_dir:
            binary_environ["ABXPKG_LIB_DIR"] = str(lib_dir)
        loaded_dependencies = [
            load_required_binary_from_config(
                required_binary,
                _CONFIG_PATH,
                global_config=config,
                environ=binary_environ,
                install=False,
            )
            for required_binary in (
                str(config.get("NODE_BINARY") or "node"),
                str(config.get("GIT_BINARY") or "git"),
                binary,
            )
        ]
    except Exception as err:
        raise RuntimeError(
            f"OpenCode dependency is not installed from required_binaries: {err}",
        ) from err

    if any(not loaded.loaded_abspath for loaded in loaded_dependencies):
        raise RuntimeError(
            "OpenCode dependency is not installed from required_binaries.",
        )

    providers = [
        loaded.loaded_binprovider
        for loaded in loaded_dependencies
        if loaded.loaded_binprovider is not None
    ]
    binary_env = BinProvider.build_exec_env(
        providers=providers,
        base_env=binary_environ,
    )
    return loaded_dependencies[-1], binary_env


def _project_route(workdir: Path, session_id: str = "") -> str:
    encoded = base64.b64encode(str(workdir.resolve()).encode()).decode()
    encoded = encoded.replace("+", "-").replace("/", "_").rstrip("=")
    route = f"{_PROXY_PREFIX}/{encoded}/session"
    return f"{route}/{session_id}" if session_id else route


def _ensure_project_files(settings: dict) -> None:
    workdir = settings["workdir"].resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    editable_skill_path = settings["opencode_dir"] / "SKILL.md"
    editable_skill_path.parent.mkdir(parents=True, exist_ok=True)
    if not editable_skill_path.exists():
        editable_skill_path.write_text(
            _ARCHIVEBOX_SKILL.format(
                archivebox_data_dir=settings["archivebox_data_dir"],
                archivebox_base_url=settings.get("archivebox_base_url", ""),
                archivebox_admin_url=settings.get("archivebox_admin_url", ""),
                archivebox_api_url=settings.get("archivebox_api_url", ""),
            ),
        )

    opencode_skill_path = (
        settings["config_home"] / "opencode" / "skills" / "archivebox" / "SKILL.md"
    )
    opencode_skill_path.parent.mkdir(parents=True, exist_ok=True)
    if opencode_skill_path.resolve() != editable_skill_path.resolve():
        if opencode_skill_path.exists() or opencode_skill_path.is_symlink():
            opencode_skill_path.unlink()
        opencode_skill_path.symlink_to(editable_skill_path)

    opencode_config_path = settings["config_home"] / "opencode" / "opencode.jsonc"
    if not opencode_config_path.exists():
        opencode_config_path.write_text(_DEFAULT_CONFIG)


def _ensure_default_session(settings: dict) -> str:
    workdir = settings["workdir"].resolve()
    params = {"directory": str(workdir)}
    timeout = settings["timeout"]
    sessions = requests.get(
        f"{settings['origin']}/session",
        params={**params, "roots": "true", "limit": 55},
        timeout=timeout,
    )
    sessions.raise_for_status()
    session_data = sessions.json()
    if not isinstance(session_data, list):
        raise RuntimeError("OpenCode returned an invalid project session list.")
    for session_data_item in session_data:
        if not isinstance(session_data_item, dict):
            continue
        session_id = str(session_data_item.get("id") or "")
        session_directory = session_data_item.get("directory")
        if (
            session_id
            and session_directory
            and Path(str(session_directory)).resolve() == workdir
        ):
            return session_id

    session = requests.post(
        f"{settings['origin']}/session",
        params=params,
        json={},
        timeout=timeout,
    )
    session.raise_for_status()
    session_data = session.json()
    session_id = str(session_data.get("id") or "")
    session_directory = session_data.get("directory")
    if (
        not session_id
        or not session_directory
        or Path(str(session_directory)).resolve() != workdir
    ):
        raise RuntimeError(
            "OpenCode did not create a session for the requested worktree.",
        )
    return session_id


def _owned_process_running() -> bool:
    process = _PROCESS
    return process is not None and process.poll() is None


def _owned_process_ready() -> bool:
    process = _PROCESS_READY
    return process is not None and process is _PROCESS and process.poll() is None


def _health(settings: dict, timeout: float = 2) -> bool:
    try:
        response = requests.get(
            f"{settings['origin']}/global/health",
            timeout=timeout,
        )
        return response.status_code == 200
    except requests.RequestException:
        return False


def _ensure_opencode(settings: dict) -> tuple[bool, str]:
    global _PROCESS, _PROCESS_READY
    started_process: subprocess.Popen | None = None
    workdir = settings["workdir"].resolve()

    with _PROCESS_LOCK:
        if _owned_process_ready():
            return True, ""
        if _health(settings):
            if _owned_process_running():
                _PROCESS_READY = _PROCESS
            return True, ""
        if _owned_process_running():
            _stop_owned_process(_PROCESS)

        try:
            binary, binary_env = _resolve_binary(
                settings["binary"],
                settings["config"],
            )
        except RuntimeError as err:
            return False, str(err)

        env = {
            **os.environ,
            **binary_env,
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

        settings["workdir"].mkdir(parents=True, exist_ok=True)
        settings["config_home"].mkdir(parents=True, exist_ok=True)
        settings["data_home"].mkdir(parents=True, exist_ok=True)
        settings["state_home"].mkdir(parents=True, exist_ok=True)
        settings["cache_home"].mkdir(parents=True, exist_ok=True)
        settings["home"].mkdir(parents=True, exist_ok=True)
        _ensure_project_files(settings)

        if _health(settings):
            return True, ""

        binary_abspath = binary.loaded_abspath
        if binary.loaded_binprovider is not None:
            binary_abspath = binary.loaded_binprovider._exec_bin_abspath(
                Path(binary.loaded_abspath),
            )
        cmd = [
            str(binary_abspath),
            "serve",
            "--hostname",
            settings["host"],
            "--port",
            str(settings["port"]),
        ]
        try:
            _PROCESS = subprocess.Popen(
                cmd,
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                start_new_session=True,
            )
            _PROCESS_READY = None
            started_process = _PROCESS
        except FileNotFoundError:
            return False, f"OpenCode binary not found: {settings['binary']}"

        deadline = time.monotonic() + settings["timeout"]
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if _health(settings, timeout=min(2, remaining)):
                if time.monotonic() <= deadline:
                    _PROCESS_READY = started_process
                    return True, ""
                break
            if started_process and started_process.poll() is not None:
                if _PROCESS is started_process:
                    _PROCESS = None
                if _PROCESS_READY is started_process:
                    _PROCESS_READY = None
                return False, "OpenCode exited before the web server became ready."
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.25, remaining))

        _stop_owned_process(started_process)
        return False, "Timed out waiting for OpenCode to start."


def _proxy_url(settings: dict, path: str | None) -> str:
    return settings["origin"] + "/" + (path or "").lstrip("/")


def _rewrite_text(body: bytes, settings: dict) -> bytes:
    text = body.decode("utf-8", errors="replace")
    text = text.replace(settings["origin"], _PROXY_PREFIX)
    # Configure the native router, not browser history or pathname. Minifier
    # identifiers change between builds; the public router prop does not.
    text = re.sub(
        r"(get component\(\)\{return [$\w]+\.router\?\?[$\w]+\},)",
        rf'\1base:"{_PROXY_PREFIX}",',
        text,
    )
    # Only the web entrypoint's default server needs the mount prefix. Changing
    # location.origin globally breaks the router's same-origin link interception.
    text = text.replace(
        '?"http://localhost:4096":location.origin',
        f'?"http://localhost:4096":location.origin+"{_PROXY_PREFIX}"',
    )
    text = text.replace('"/assets/', f'"{_PROXY_PREFIX}/assets/')
    text = text.replace("'/assets/", f"'{_PROXY_PREFIX}/assets/")
    text = re.sub(
        r'("modulepreload",[$\w]+=function\(([$\w]+)\)\{return")/("\+\2\})',
        rf"\1{_PROXY_PREFIX}/\3",
        text,
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


atexit.register(_stop_owned_process)


def agent_context(settings: dict) -> dict:
    ok, error = _ensure_opencode(settings)
    if not ok:
        raise RuntimeError(error)
    with _SESSION_LOCK:
        session_id = _ensure_default_session(settings)
    return {
        "title": "Agent",
        "proxy_url": _project_route(settings["workdir"], session_id),
        "proxy_prefix": _PROXY_PREFIX,
        "workdir": str(settings["workdir"].resolve()),
        "recent_session_id": session_id,
    }


async def _event_chunks(settings, path, method, params, headers):
    # Stream failures happen after the host returns response headers.
    try:
        if not _owned_process_ready():
            ok, error = await asyncio.to_thread(_ensure_opencode, settings)
            if not ok:
                raise RuntimeError(error)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings["timeout"], read=None),
            follow_redirects=False,
        ) as client:
            async with client.stream(
                method,
                _proxy_url(settings, path),
                params=params,
                headers=headers,
            ) as upstream:
                upstream.raise_for_status()
                async for chunk in upstream.aiter_raw(chunk_size=512):
                    yield chunk
    except Exception:
        _LOGGER.exception("OpenCode event stream failed")
        yield b'event: error\ndata: {"error":"OpenCode unavailable"}\n\n'


async def websocket_proxy(settings, path, query, protocols, receive, send):
    """Forward an authenticated WebSocket; the host consumes the connect event."""
    from websockets.asyncio.client import connect

    tasks = []
    try:
        ok, error = await asyncio.to_thread(_ensure_opencode, settings)
        if not ok:
            raise RuntimeError(error)
        url = _proxy_url(settings, path).replace("http", "ws", 1)
        if query:
            url += "?" + query.decode("ascii")
        async with connect(
            url,
            subprotocols=protocols or None,
            proxy=None,
            open_timeout=settings["timeout"],
        ) as upstream:
            await send(
                {"type": "websocket.accept", "subprotocol": upstream.subprotocol},
            )

            async def from_browser():
                while True:
                    event = await receive()
                    if event["type"] == "websocket.disconnect":
                        return
                    if event["type"] == "websocket.receive":
                        payload = event.get("bytes")
                        await upstream.send(
                            payload if payload is not None else event["text"],
                        )

            async def from_upstream():
                async for payload in upstream:
                    kind = "bytes" if isinstance(payload, bytes) else "text"
                    await send({"type": "websocket.send", kind: payload})
                await send({"type": "websocket.close", "code": 1000})

            tasks = [
                asyncio.create_task(from_browser()),
                asyncio.create_task(from_upstream()),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    except Exception:
        _LOGGER.exception("OpenCode WebSocket failed")
        await send({"type": "websocket.close", "code": 1011})
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def proxy(settings: dict, method: str, path: str, params, headers, body: bytes):
    """Forward a request after the host authenticates it and checks its origin."""
    forwarded = {
        key: headers[key]
        for key in ("Accept", "Accept-Language", "Content-Type", "Range", "User-Agent")
        if headers.get(key)
    }
    if method == "GET" and path.endswith("/event"):
        return (
            200,
            {
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
            _event_chunks(settings, path, method, params, forwarded),
        )

    if path == "global/health" or not _owned_process_ready():
        ok, error = _ensure_opencode(settings)
        if not ok:
            raise RuntimeError(error)

    with requests.request(
        method,
        _proxy_url(settings, path),
        params=params,
        data=body if method not in {"GET", "HEAD"} else None,
        headers=forwarded,
        timeout=settings["timeout"],
        allow_redirects=False,
    ) as upstream:
        content = upstream.content
        response_headers = _response_headers(upstream, settings)
        if any(
            upstream.headers.get("Content-Type", "").startswith(prefix)
            for prefix in _TEXT_CONTENT_TYPES
        ):
            content = _rewrite_text(content, settings)
        response_headers["Cache-Control"] = "no-store"
        return upstream.status_code, response_headers, content

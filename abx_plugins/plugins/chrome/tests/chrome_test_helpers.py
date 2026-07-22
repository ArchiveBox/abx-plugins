"""
Shared Chrome test helpers for plugin integration tests.

This module provides common utilities for Chrome-based plugin tests, reducing
duplication across test files. Chrome lifecycle functions delegate to
chrome_utils.js, while shared path helpers delegate to base/utils.js.

Function names match the JS equivalents in snake_case:
    JS: getMachineType()  -> Python: get_machine_type()
    JS: getLibDir()       -> Python: get_lib_dir()
    JS: getNodeModulesDir() -> Python: get_node_modules_dir()
    JS: getExtensionsDir() -> Python: get_extensions_dir()
    JS: findChromium()    -> Python: find_chromium()
    JS: killChrome()      -> Python: kill_chrome()
    JS: getTestEnv()      -> Python: get_test_env()

Usage:
    # Path helpers (delegate to base/utils.js):
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
        get_test_env,           # env dict with ABXPKG_LIB_DIR, NODE_MODULES_DIR, MACHINE_TYPE
        get_machine_type,       # e.g., 'x86_64-linux', 'arm64-darwin'
        get_lib_dir,            # Path to lib dir
        get_node_modules_dir,   # Path to node_modules
        get_extensions_dir,     # Path to chrome extensions
        find_chromium,          # Find Chrome/Chromium binary
        kill_chrome,            # Kill Chrome process by PID
    )

    # For Chrome session tests:
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
        chrome_session,         # Context manager (Full Chrome + tab setup with automatic cleanup)
    )

    # For extension tests:
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
        setup_test_env,         # Full dir structure + Chrome install
        launch_chromium_session, # Launch Chrome, return CDP URL
        kill_chromium_session,   # Cleanup Chrome
    )

    # Run hooks and parse JSONL:
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
        run_hook,               # Run hook, return (returncode, stdout, stderr)
        parse_jsonl_output,     # Parse JSONL from stdout
    )
"""

import json
import logging
import os
import signal
import fcntl
import re
import ssl
import subprocess
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, TextIO
from contextlib import contextmanager

import pytest
from _pytest.fixtures import FixtureLookupError
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from abx_plugins.plugins.base.testing import (
    assert_isolated_snapshot_env,
    get_hydrated_required_binaries,
    run_hook as _base_run_hook,
)
from abx_plugins.plugins.base.utils import build_binproviders, load_required_binary

# Plugin directory locations
CHROME_PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = CHROME_PLUGIN_DIR.parent

# Hook script locations
CHROME_LAUNCH_HOOK = CHROME_PLUGIN_DIR / "on_CrawlSetup__90_chrome_launch.daemon.bg.js"
CHROME_CRAWL_WAIT_HOOK = CHROME_PLUGIN_DIR / "on_CrawlSetup__91_chrome_wait.js"
CHROME_SNAPSHOT_LAUNCH_HOOK = (
    CHROME_PLUGIN_DIR / "on_Snapshot__09_chrome_launch.daemon.bg.js"
)
CHROME_TAB_HOOK = CHROME_PLUGIN_DIR / "on_Snapshot__10_chrome_tab.daemon.bg.js"
CHROME_WAIT_HOOK = CHROME_PLUGIN_DIR / "on_Snapshot__11_chrome_wait.js"
_CHROME_NAVIGATE_HOOK = next(
    CHROME_PLUGIN_DIR.glob("on_Snapshot__*_chrome_navigate.*"),
    None,
)
if _CHROME_NAVIGATE_HOOK is None:
    raise FileNotFoundError(
        f"Could not find chrome navigate hook in {CHROME_PLUGIN_DIR}",
    )
CHROME_NAVIGATE_HOOK = _CHROME_NAVIGATE_HOOK
CHROME_UTILS = CHROME_PLUGIN_DIR / "chrome_utils.js"
BASE_UTILS = PLUGINS_ROOT / "base" / "utils.js"
logger = logging.getLogger(__name__)

_CHROME_PROVIDER_ENV_CACHE: dict[tuple[str, ...], dict[str, str]] = {}
_CHROME_PROVIDER_ENV_KEYS = (
    "PATH",
    "NODE_BINARY",
    "CHROME_BINARY",
    "NODE_PATH",
    "NODE_MODULES_DIR",
    "NODE_MODULE_DIR",
    "PNPM_HOME",
    "PNPM_BIN_DIR",
    "NPM_BIN_DIR",
    "PLAYWRIGHT_BROWSERS_PATH",
    "PUPPETEER_CACHE_DIR",
    "CHROMEWEBSTORE_EXTENSIONS_DIR",
)


def require_chrome_runtime_impl() -> None:
    """Require chrome runtime prerequisites for integration tests."""
    try:
        env = get_test_env(install_required_binaries=True)
        chrome_binary = install_chromium_with_abxpkg(
            env,
            timeout=int(env.get("ABXPKG_INSTALL_TIMEOUT") or "300"),
        )
        os.environ["CHROME_BINARY"] = chrome_binary
        for key in (
            "ABXPKG_LIB_DIR",
            "NODE_BINARY",
            "NODE_MODULES_DIR",
            "NODE_MODULE_DIR",
            "NODE_PATH",
            "PNPM_HOME",
            "PNPM_BIN_DIR",
            "NPM_BIN_DIR",
            "PLAYWRIGHT_BROWSERS_PATH",
            "PUPPETEER_CACHE_DIR",
            "PATH",
        ):
            if env.get(key):
                os.environ[key] = env[key]
    except Exception as exc:
        logger.error("Chrome integration prerequisites unavailable: %s", exc)
        raise AssertionError(
            f"Chrome integration prerequisites unavailable: {exc}",
        )


require_chrome_runtime = pytest.fixture(scope="module")(require_chrome_runtime_impl)


# Prefer root-level URL fixtures if they exist, otherwise use pytest-httpserver.
_ROOT_URL_FIXTURE_NAMES = (
    "local_test_urls",
    "test_urls",
    "deterministic_urls",
    "local_http_url",
    "local_url",
    "test_url",
)


class LoggedPopen(subprocess.Popen[str]):
    _stdout_handle: TextIO
    _stderr_handle: TextIO
    _stdout_log: Path
    _stderr_log: Path
    _chrome_pid: int | None


def _configure_chrome_httpserver(httpserver) -> dict[str, str]:
    """Register deterministic Chrome test routes on pytest-httpserver."""
    origin = httpserver.url_for("/").rstrip("/")
    index_html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Example Domain</title>
  <meta name="description" content="Local deterministic test page for ArchiveBox plugin tests.">
  <meta property="og:title" content="Example Domain">
  <meta property="og:description" content="Local deterministic fixture page">
  <link rel="canonical" href="{origin}/">
</head>
<body>
  <main>
    <h1>Example Domain</h1>
    <h2>Deterministic Local Fixture</h2>
    <p>This page is served by the chrome test helper fixture.</p>
    <a href="{origin}/linked">Linked page</a>
    <a href="{origin}/redirect">Redirect endpoint</a>
  </main>
</body>
</html>"""
    httpserver.expect_request("/").respond_with_data(
        index_html,
        content_type="text/html; charset=utf-8",
    )
    httpserver.expect_request("/index.html").respond_with_data(
        index_html,
        content_type="text/html; charset=utf-8",
    )
    httpserver.expect_request("/linked").respond_with_data(
        "<html><head><title>Linked Page</title></head><body><h1>Linked Page</h1></body></html>",
    )

    slow_response_release = threading.Event()

    def slow_page(_request):
        slow_response_release.wait(timeout=5)
        return Response(
            """<!doctype html>
<html>
<head><meta charset="utf-8"><title>Slow Page</title></head>
<body>
  <main>
    <h1>Slow Page</h1>
    <p>delay_ms=5000</p>
  </main>
</body>
</html>""",
            content_type="text/html; charset=utf-8",
        )

    httpserver.expect_request("/slow").respond_with_handler(slow_page)
    httpserver.expect_request("/popup-child").respond_with_data(
        """<!doctype html>
<html>
<head><meta charset="utf-8"><title>Popup Child</title></head>
<body><h1>Popup Child</h1><p>This popup should not replace the canonical snapshot target.</p></body>
</html>""",
        content_type="text/html; charset=utf-8",
    )
    httpserver.expect_request("/popup-parent").respond_with_data(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Popup Parent</title>
</head>
<body>
  <main>
    <h1>Popup Parent</h1>
    <p id="status">main-page</p>
  </main>
  <script>
    const popupUrl = "{origin}/popup-child";
    let popup = null;
    function openAndRefocusPopup() {{
      if (!popup || popup.closed) {{
        popup = window.open(popupUrl, "abx-popup", "width=480,height=320");
      }}
      if (popup && !popup.closed) {{
        try {{ popup.focus(); }} catch (e) {{}}
      }}
      try {{ window.focus(); }} catch (e) {{}}
    }}
    window.addEventListener("load", () => {{
      openAndRefocusPopup();
      setTimeout(openAndRefocusPopup, 150);
      setTimeout(openAndRefocusPopup, 400);
      setTimeout(openAndRefocusPopup, 900);
    }});
  </script>
</body>
</html>""",
        content_type="text/html; charset=utf-8",
    )
    httpserver.expect_request("/redirect").respond_with_data(
        "",
        status=302,
        headers={"Location": "/"},
    )
    httpserver.expect_request(
        re.compile(r"^/(?:nonexistent-page-404|not-found)$"),
    ).respond_with_data(
        "<html><head><title>Not Found</title></head><body><h1>404 Not Found</h1></body></html>",
        status=404,
    )
    httpserver.expect_request("/static/test.txt").respond_with_data(
        "static fixture payload",
        content_type="text/plain; charset=utf-8",
    )
    httpserver.expect_request("/api/data.json").respond_with_data(
        '{"ok": true, "source": "deterministic-fixture"}',
        content_type="application/json",
    )
    httpserver.expect_request("/claudechrome").respond_with_data(
        """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Claude Chrome Test Page</title>
  <style>
    body { margin: 20px; font-family: sans-serif; }
    .hidden-content { display: none; }
    #expand-btn {
      padding: 10px 20px;
      font-size: 16px;
      cursor: pointer;
      background: #4a90d9;
      color: white;
      border: none;
      border-radius: 4px;
    }
  </style>
</head>
<body>
  <h1>Test Page for Claude Chrome</h1>
  <p>This page has a button that reveals hidden content.</p>
  <button id="expand-btn" onclick="document.getElementById('hidden').style.display='block'; this.textContent='Expanded!';">
    Show More
  </button>
  <div id="hidden" class="hidden-content">
    <p>This content was hidden and is now visible after clicking the button.</p>
  </div>
</body>
</html>""",
    )
    httpserver.expect_request("/ads").respond_with_data(
        """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Ad Fixture</title>
</head>
<body>
  <main>
    <h1>Ad Fixture</h1>
    <div class="ad-banner" style="display:none">hidden ad slot</div>
    <div id="sponsored-unit" style="visibility:hidden">hidden sponsored slot</div>
    <script src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js"></script>
  </main>
</body>
</html>""",
    )
    httpserver.expect_request(re.compile(r"^/snapshot-\d+$")).respond_with_data(
        "<html><head><title>Snapshot Page</title></head><body><h1>Snapshot Page</h1></body></html>",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data("", status=404)
    return _build_test_urls(origin)


def _create_https_test_server(tmp_path_factory) -> HTTPServer:
    cert_dir = tmp_path_factory.mktemp("chrome_test_https")
    cert_path = cert_dir / "localhost.crt"
    key_path = cert_dir / "localhost.key"
    openssl_config = cert_dir / "openssl.cnf"
    openssl_config.write_text(
        """[req]
distinguished_name=req_distinguished_name
x509_extensions=v3_req
prompt=no
[req_distinguished_name]
CN=localhost
[v3_req]
subjectAltName=@alt_names
[alt_names]
DNS.1=localhost
IP.1=127.0.0.1
""",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "1",
            "-config",
            str(openssl_config),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return HTTPServer(host="127.0.0.1", port=0, ssl_context=ssl_context)


def _build_test_urls(
    base_url: str,
    https_base_url: str | None = None,
) -> dict[str, str]:
    base = base_url.rstrip("/")
    urls = {
        "base_url": f"{base}/",
        "origin": base,
        "redirect_url": f"{base}/redirect",
        "not_found_url": f"{base}/nonexistent-page-404",
        "linked_url": f"{base}/linked",
        "slow_url": f"{base}/slow?delay=5000",
        "popup_parent_url": f"{base}/popup-parent",
        "popup_child_url": f"{base}/popup-child",
        "static_file_url": f"{base}/static/test.txt",
        "json_url": f"{base}/api/data.json",
        "claudechrome_url": f"{base}/claudechrome",
        "ad_url": f"{base}/ads",
    }
    if https_base_url:
        https_base = https_base_url.rstrip("/")
        urls["https_base_url"] = f"{https_base}/"
        urls["https_not_found_url"] = f"{https_base}/nonexistent-page-404"
    return urls


def _coerce_upstream_urls(value: Any) -> dict[str, str] | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return _build_test_urls(value)
    if not isinstance(value, dict):
        return None

    base_url = (
        value.get("base_url")
        or value.get("url")
        or value.get("local_url")
        or value.get("http_url")
    )
    if not isinstance(base_url, str) or not base_url.startswith(
        ("http://", "https://"),
    ):
        return None

    urls = _build_test_urls(base_url, value.get("https_base_url"))
    for key, candidate in value.items():
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            urls[key] = candidate
    return urls


def ensure_chromium_and_puppeteer_installed_impl(tmp_path_factory) -> str:
    """Install Chrome and Puppeteer once for test sessions that require Chrome."""
    os.environ["SNAP_DIR"] = str(tmp_path_factory.mktemp("chrome_test_data"))
    os.environ["PERSONAS_DIR"] = str(tmp_path_factory.mktemp("chrome_test_personas"))
    os.environ["ACTIVE_PERSONA"] = "Default"
    os.environ["HOME"] = str(tmp_path_factory.mktemp("chrome_test_home"))
    os.environ["XDG_CONFIG_HOME"] = str(Path(os.environ["HOME"]) / ".config")
    os.environ["XDG_CACHE_HOME"] = str(Path(os.environ["HOME"]) / ".cache")
    os.environ["XDG_DATA_HOME"] = str(Path(os.environ["HOME"]) / ".local" / "share")
    os.environ.setdefault(
        "ABXPKG_LIB_DIR",
        str(tmp_path_factory.mktemp("chrome_test_lib")),
    )
    for inherited_key in (
        "CHROME_DOWNLOADS_DIR",
        "CHROMEWEBSTORE_EXTENSIONS_DIR",
        "CHROME_USER_DATA_DIR",
        "COOKIES_FILE",
    ):
        os.environ.pop(inherited_key, None)

    for key in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "ABXPKG_LIB_DIR",
    ):
        Path(os.environ[key]).mkdir(parents=True, exist_ok=True)

    env = get_test_env()
    chrome_binary = install_chromium_with_abxpkg(env)
    if not chrome_binary:
        raise RuntimeError("Chrome not found after install")

    os.environ["CHROME_BINARY"] = chrome_binary
    for key in (
        "NODE_BINARY",
        "NODE_MODULES_DIR",
        "NODE_MODULE_DIR",
        "NODE_PATH",
        "PNPM_HOME",
        "PNPM_BIN_DIR",
        "NPM_BIN_DIR",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PUPPETEER_CACHE_DIR",
        "PATH",
    ):
        if env.get(key):
            os.environ[key] = env[key]
    return chrome_binary


ensure_chromium_and_puppeteer_installed = pytest.fixture(scope="session")(
    ensure_chromium_and_puppeteer_installed_impl,
)


@pytest.fixture
def chrome_test_urls(request, httpserver, tmp_path_factory):
    """Provide deterministic test URLs from pytest-httpserver."""
    for fixture_name in _ROOT_URL_FIXTURE_NAMES:
        try:
            upstream = request.getfixturevalue(fixture_name)
        except FixtureLookupError:
            continue
        urls = _coerce_upstream_urls(upstream)
        if urls:
            return urls

    urls = _configure_chrome_httpserver(httpserver)
    https_server = _create_https_test_server(tmp_path_factory)
    https_server.start()
    request.addfinalizer(https_server.stop)
    _configure_chrome_httpserver(https_server)
    urls.update(
        {
            key: value
            for key, value in _build_test_urls(
                urls["base_url"],
                https_server.url_for("/"),
            ).items()
            if key.startswith("https_")
        },
    )
    return urls


@pytest.fixture
def chrome_test_url(chrome_test_urls):
    return chrome_test_urls["base_url"]


@pytest.fixture
def chrome_test_https_url(chrome_test_urls):
    https_url = chrome_test_urls.get("https_base_url")
    assert https_url, (
        "HTTPS fixture unavailable; provide chrome_test_urls['https_base_url']"
    )
    return https_url


# =============================================================================
# Path helpers delegate to the runtime JavaScript utilities.
# Function names match JS: getMachineType -> get_machine_type, etc.
# =============================================================================


def _call_chrome_utils(
    command: str,
    *args: str,
    env: dict | None = None,
    resolve_required_binary_env: bool = True,
) -> tuple[int, str, str]:
    """Call the JS chrome utilities from Python test code.

    This is the bridge to the runtime single source of truth. Lifecycle-sensitive
    behavior such as browser discovery, session marker handling, and test env
    path calculation should stay in ``chrome_utils.js`` so Python tests exercise
    the same rules as production hooks instead of shadowing them.

    Args:
        command: The CLI command (e.g., 'findChromium', 'getTestEnv')
        *args: Additional command arguments
        env: Environment dict (default: current env)

    Returns:
        Tuple of (returncode, stdout, stderr)
    """
    # Callers pass complete subprocess environments. Treat them as
    # authoritative so deliberately removed isolation keys are not restored
    # from the pytest process environment.
    payload = os.environ.copy() if env is None else env.copy()

    if resolve_required_binary_env:
        returncode, provider_env, error = _resolve_chrome_required_binary_env(
            payload,
        )
        if returncode != 0:
            return returncode, "", error
        payload.update(provider_env)

    node_binary = payload.get("NODE_BINARY")
    if not node_binary:
        return 1, "", "NODE_BINARY was not resolved by abxpkg"
    cmd = [node_binary, str(CHROME_UTILS), command, *list(args)]
    result = subprocess.run(
        cmd,
        cwd=str(CHROME_PLUGIN_DIR),
        capture_output=True,
        text=True,
        timeout=30,
        env=payload,
    )
    return result.returncode, result.stdout, result.stderr


def _run_chrome_required_binary_env(
    env: dict,
    *,
    timeout: int = 300,
    install: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Resolve the provider-built env, installing only during explicit preflight."""
    payload = env.copy()
    for key in (
        "NODE_MODULES_DIR",
        "NODE_MODULE_DIR",
        "NODE_PATH",
        "PNPM_HOME",
        "PNPM_BIN_DIR",
        "NPM_BIN_DIR",
    ):
        payload.pop(key, None)

    # chrome_utils.js is a runtime helper, not an installer. Tests that call it
    # directly must still get the same provider-built env that Chrome hooks get
    # from their shebang/deps path. abxpkg env emits deltas relative to its
    # input env, so stale derived module paths from an earlier probe must not
    # make the complete provider-built NODE_PATH disappear from JSON output.
    command = ["abxpkg", "env"]
    if install:
        command.append("--install")
    command.extend(
        [
            "--json",
            "--deps-from=./config.json:required_binaries",
            "node",
        ],
    )
    return subprocess.run(
        command,
        cwd=str(CHROME_PLUGIN_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=payload,
    )


def _chrome_provider_env_cache_key(env: dict) -> tuple[str, ...]:
    return tuple(
        str(env.get(key) or "")
        for key in (
            "ABXPKG_LIB_DIR",
            "PATH",
            "NODE_BINARY",
            "CHROME_BINARY",
            "ABXPKG_BINPROVIDERS",
        )
    )


def _resolve_chrome_required_binary_env(
    env: dict,
    *,
    timeout: int = 300,
    install: bool = False,
) -> tuple[int, dict[str, str], str]:
    """Return the real provider env, reusing it for one unchanged test runtime."""
    cache_key = _chrome_provider_env_cache_key(env)
    if not install and cache_key in _CHROME_PROVIDER_ENV_CACHE:
        return 0, dict(_CHROME_PROVIDER_ENV_CACHE[cache_key]), ""

    env_result = _run_chrome_required_binary_env(
        env,
        timeout=timeout,
        install=install,
    )
    if env_result.returncode != 0:
        return env_result.returncode, {}, env_result.stderr or env_result.stdout

    resolved = _parse_abxpkg_env_delta(env_result.stdout, base_env=env)
    provider_env = {
        key: str(resolved[key]) for key in _CHROME_PROVIDER_ENV_KEYS if key in resolved
    }
    _CHROME_PROVIDER_ENV_CACHE[cache_key] = provider_env
    return 0, dict(provider_env), ""


def _parse_abxpkg_env_delta(
    stdout: str,
    *,
    base_env: dict,
) -> dict[str, str]:
    """Parse ``abxpkg env --json`` output back into a complete env fragment."""
    parsed = json.loads(stdout or "{}")
    for key in ("PATH", "NODE_PATH", "PYTHONPATH"):
        value = parsed.get(key)
        base_value = base_env.get(key)
        if not isinstance(value, str) or not base_value:
            continue
        # abxpkg emits shell-friendly path deltas. Python subprocess envs need
        # the complete value, otherwise a prefix like "/tmp/lib/env/bin:" drops
        # the caller's node/pnpm/python search paths before chrome_utils runs.
        if value.endswith(os.pathsep):
            parsed[key] = f"{value}{base_value}"
        elif value.startswith(os.pathsep):
            parsed[key] = f"{base_value}{value}"
    return parsed


def _call_base_utils(
    command: str,
    *args: str,
    env: dict | None = None,
) -> tuple[int, str, str]:
    """Call shared JS base utilities from Python test code."""
    payload = os.environ.copy() if env is None else env.copy()
    node_binary = payload.get("NODE_BINARY")
    if not node_binary:
        returncode, provider_env, error = _resolve_chrome_required_binary_env(
            payload,
            install=True,
        )
        if returncode != 0:
            return returncode, "", error
        payload.update(provider_env)
        node_binary = payload.get("NODE_BINARY")
    if not node_binary:
        return 1, "", "NODE_BINARY was not resolved by abxpkg"
    cmd = [node_binary, str(BASE_UTILS), command, *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        env=payload,
    )
    return result.returncode, result.stdout, result.stderr


def wait_for_extensions_metadata(
    chrome_dir: Path,
    timeout_seconds: int = 10,
) -> list[dict[str, Any]]:
    """Wait for ``browser.json`` to be published and return its parsed records.

    Extension-backed hooks should treat this as the post-launch readiness gate
    for extension runtime metadata. It is stronger than merely seeing
    ``chrome.pid`` or ``cdp_url.txt`` because the browser may still be in the
    startup window before extensions finish loading.
    """
    state = wait_for_chrome_session_state(
        chrome_dir,
        env=get_test_env(),
        timeout_seconds=timeout_seconds,
        require_browser_ready=True,
        require_connectable=True,
    )
    extensions = state.get("extensions")
    assert isinstance(extensions, list) and extensions, state
    return extensions


def wait_for_chrome_session_state(
    chrome_dir: Path,
    *,
    env: dict[str, str],
    timeout_seconds: int = 60,
    require_target_id: bool = False,
    require_browser_ready: bool = False,
    require_connectable: bool = True,
) -> dict[str, Any]:
    """Use Chrome's production file/CDP readiness gate exactly once."""
    script = r"""
const chromeUtils = require(process.argv[1]);
const chromeDir = process.argv[2];
const options = JSON.parse(process.argv[3]);
(async () => {
  if (options.requireTargetId || options.requireConnectable) {
    options.puppeteer = chromeUtils.resolvePuppeteerModule();
  }
  const state = await chromeUtils.waitForChromeSessionState(chromeDir, options);
  if (!state) throw new Error(`Chrome session did not become ready: ${chromeDir}`);
  process.stdout.write(JSON.stringify(state));
})().catch((error) => {
  console.error(error.stack || error.message);
  process.exit(1);
});
"""
    options = {
        "timeoutMs": timeout_seconds * 1000,
        "requireTargetId": require_target_id,
        "requireBrowserReady": require_browser_ready,
        "requireConnectable": require_connectable,
        "probeTimeoutMs": 1000,
    }
    result = subprocess.run(
        [
            env["NODE_BINARY"],
            "-e",
            script,
            str(CHROME_UTILS),
            str(chrome_dir),
            json.dumps(options),
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds + 10,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert isinstance(state, dict), state
    return state


def write_browser_metadata(
    chrome_dir: Path,
    extensions: list[dict[str, Any]] | None = None,
    *,
    env: dict[str, str],
) -> None:
    """Publish browser readiness metadata via the runtime JS implementation."""
    script = r"""
const chromeUtils = require(process.argv[1]);
const chromeDir = process.argv[2];
const extensions = JSON.parse(process.argv[3]);
chromeUtils.writeBrowserMetadata(chromeDir, extensions);
"""
    result = subprocess.run(
        [
            env["NODE_BINARY"],
            "-e",
            script,
            str(CHROME_UTILS),
            str(chrome_dir),
            json.dumps(extensions or []),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"writeBrowserMetadata failed for {chrome_dir}: {result.stderr or result.stdout}",
        )


def port_from_cdp_url(cdp_url: str) -> int:
    """Return the DevTools HTTP port for a browser websocket endpoint."""
    parsed = urllib.parse.urlparse(cdp_url)
    if not parsed.port:
        raise ValueError(f"CDP URL does not include a port: {cdp_url}")
    return parsed.port


def fetch_devtools_targets(cdp_url: str) -> list[dict[str, Any]]:
    """Read the live DevTools target list for a real browser endpoint."""
    port = port_from_cdp_url(cdp_url)
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/json/list",
        timeout=10,
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert isinstance(payload, list), payload
    return payload


def close_target_and_wait_destroyed(
    cdp_url: str,
    target_id: str,
    env: dict[str, str],
) -> None:
    """Close a target and wait for Chrome's matching targetdestroyed event."""
    script = r"""
const chromeUtils = require(process.argv[1]);
const cdpUrl = process.argv[2];
const expectedTargetId = process.argv[3];

(async () => {
  const puppeteer = chromeUtils.resolvePuppeteerModule();
  const browser = await chromeUtils.connectToBrowserEndpoint(puppeteer, cdpUrl, {
    defaultViewport: null,
  });
  try {
    const destroyed = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        reject(new Error(`Timed out waiting for targetdestroyed: ${expectedTargetId}`));
      }, 10000);
      browser.on("targetdestroyed", (target) => {
        if (chromeUtils.getTargetIdFromTarget(target) !== expectedTargetId) return;
        clearTimeout(timeout);
        resolve();
      });
    });
    const session = await browser.target().createCDPSession();
    const result = await session.send("Target.closeTarget", {
      targetId: expectedTargetId,
    });
    if (!result.success) {
      throw new Error(`Target.closeTarget rejected ${expectedTargetId}`);
    }
    await destroyed;
    await session.detach();
  } finally {
    await browser.disconnect().catch(() => {});
  }
})().catch((error) => {
  console.error(error && (error.stack || error.message || String(error)));
  process.exit(1);
});
"""
    result = subprocess.run(
        [env["NODE_BINARY"], "-e", script, str(CHROME_UTILS), cdp_url, target_id],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    assert result.returncode == 0, result.stderr


def create_target_via_cdp(cdp_url: str, url: str) -> dict[str, Any]:
    """Create a real page target through Chrome's HTTP endpoint."""
    port = port_from_cdp_url(cdp_url)
    encoded_url = urllib.parse.quote(url, safe="")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/new?{encoded_url}",
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert isinstance(payload, dict), payload
    return payload


def get_cookies_via_cdp(port: int, env: dict[str, str]) -> list[dict[str, Any]]:
    """Read browser cookies via the chrome_utils.js CLI helper."""
    returncode, stdout, stderr = _call_chrome_utils(
        "getCookiesViaCdp",
        str(port),
        env=env,
    )
    assert returncode == 0, (
        f"Failed to read cookies via CDP: {stderr}\nStdout: {stdout}"
    )
    payload = json.loads(stdout or "[]")
    assert isinstance(payload, list), payload
    return payload


def get_machine_type() -> str:
    """Get machine type string (e.g., 'x86_64-linux', 'arm64-darwin').

    Matches JS base/utils.js: getMachineType()

    Uses the same base/utils.js implementation as production hooks.
    """
    returncode, stdout, stderr = _call_base_utils("getMachineType")
    if returncode != 0 or not stdout.strip():
        raise RuntimeError(stderr or stdout or "base utils did not return MACHINE_TYPE")
    return stdout.strip()


def get_lib_dir() -> Path:
    """Get ABXPKG_LIB_DIR path for shared binaries and provider-managed artifacts.

    Matches JS base/utils.js: getLibDir()

    Uses the same base/utils.js implementation as production hooks.
    """
    returncode, stdout, stderr = _call_base_utils("getLibDir")
    if returncode != 0 or not stdout.strip():
        raise RuntimeError(
            stderr or stdout or "base utils did not return ABXPKG_LIB_DIR",
        )
    return Path(stdout.strip())


def get_node_modules_dir() -> Path:
    """Get NODE_MODULES_DIR path for pnpm packages.

    Matches JS chrome_utils.js: getNodeModulesDir()

    Uses the same chrome_utils.js implementation as production hooks.
    """
    returncode, stdout, stderr = _call_chrome_utils("getNodeModulesDir")
    if returncode != 0 or not stdout.strip():
        raise RuntimeError(
            stderr or stdout or "chrome utils did not return NODE_MODULES_DIR",
        )
    return Path(stdout.strip())


def get_extensions_dir(env: dict | None = None) -> str:
    """Get the provider-managed Chrome extension cache path."""
    payload = os.environ.copy() if env is None else env.copy()
    provider = build_binproviders(
        "chromewebstore",
        config=payload,
        environ=payload,
    )[0]
    return str(provider.ENV["CHROMEWEBSTORE_EXTENSIONS_DIR"])


def chrome_extension_install_env(tmpdir: str | Path) -> tuple[dict[str, str], Path]:
    """Build a minimal install-time env for chromewebstore-backed extensions."""
    install_root = Path(tmpdir).resolve()
    snap_dir = install_root / "snap"
    crawl_dir = install_root / "crawl"
    personas_dir = install_root / "personas"
    lib_dir = install_root / "lib"

    env = os.environ.copy()
    env.update(
        {
            "SNAP_DIR": str(snap_dir),
            "CRAWL_DIR": str(crawl_dir),
            "PERSONAS_DIR": str(personas_dir),
            "ACTIVE_PERSONA": "Default",
            "ABXPKG_LIB_DIR": str(lib_dir),
        },
    )
    for inherited_key in (
        "CHROME_DOWNLOADS_DIR",
        "CHROMEWEBSTORE_EXTENSIONS_DIR",
        "CHROME_USER_DATA_DIR",
        "COOKIES_FILE",
    ):
        env.pop(inherited_key, None)
    node_modules_dir = lib_dir / "pnpm" / "packages" / "chrome" / "node_modules"
    env.update(
        {
            "NODE_MODULES_DIR": str(node_modules_dir),
            "NODE_MODULE_DIR": str(node_modules_dir),
            "NODE_PATH": str(node_modules_dir),
            "PNPM_HOME": str(node_modules_dir / ".bin"),
        },
    )
    resolve_node_with_abxpkg(env)
    extensions_dir = Path(get_extensions_dir(env=env))

    snap_dir.mkdir(parents=True, exist_ok=True)
    crawl_dir.mkdir(parents=True, exist_ok=True)
    personas_dir.mkdir(parents=True, exist_ok=True)
    extensions_dir.mkdir(parents=True, exist_ok=True)
    return env, extensions_dir


def find_chromium() -> str | None:
    """Return the CHROME_BINARY path resolved by the abxpkg test fixture.

    Returns:
        Path to Chromium binary or None if not found
    """
    env = {**os.environ, **get_test_env()}
    returncode, stdout, stderr = _call_chrome_utils("findChromium", env=env)
    if returncode == 0 and stdout.strip():
        return stdout.strip()
    return None


def kill_chrome(pid: int, output_dir: str | None = None) -> bool:
    """Kill a Chrome process by PID.

    Matches JS: killChrome()

    Uses chrome_utils.js which handles:
    - SIGTERM then SIGKILL
    - Process group killing
    - Zombie process cleanup

    Args:
        pid: Process ID to kill
        output_dir: Optional chrome output directory for PID file cleanup

    Returns:
        True if the kill command succeeded
    """
    args = [str(pid)]
    if output_dir:
        args.append(str(output_dir))
    returncode, stdout, stderr = _call_chrome_utils("killChrome", *args)
    return returncode == 0


def is_pid_alive(pid: int) -> bool:
    """Return True if the process still exists."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def get_test_env(*, install_required_binaries: bool = False) -> dict:
    """Get the shared runtime-like environment dict for Chrome plugin tests.

    Matches JS: getTestEnv()

    Requires abxpkg's provider-built environment, then asks base/utils.js for
    runtime-derived values. Use this for subprocess calls in
    plugin tests so ``ABXPKG_LIB_DIR``, ``NODE_MODULES_DIR``, ``NODE_PATH``,
    and related settings match the JS runtime contract.
    """
    env = os.environ.copy()
    returncode, provider_env, error = _resolve_chrome_required_binary_env(
        env,
        install=install_required_binaries,
    )
    if returncode != 0:
        raise RuntimeError(error)
    env.update(provider_env)
    provider_node_path = env.get("NODE_PATH")

    returncode, stdout, stderr = _call_base_utils("getTestEnv", env=env)
    if returncode != 0 or not stdout.strip():
        raise RuntimeError(stderr or stdout or "base utils did not return test env")
    env.update(json.loads(stdout))
    returncode, node_modules_stdout, node_modules_stderr = _call_chrome_utils(
        "getNodeModulesDir",
        env=env,
        resolve_required_binary_env=False,
    )
    if returncode != 0 or not node_modules_stdout.strip():
        raise RuntimeError(node_modules_stderr or node_modules_stdout)
    env["NODE_MODULES_DIR"] = node_modules_stdout.strip()
    env["NODE_PATH"] = provider_node_path or env["NODE_MODULES_DIR"]
    returncode, extensions_stdout, extensions_stderr = _call_chrome_utils(
        "getExtensionsDir",
        env=env,
        resolve_required_binary_env=False,
    )
    if returncode != 0 or not extensions_stdout.strip():
        raise RuntimeError(extensions_stderr or extensions_stdout)
    env["CHROMEWEBSTORE_EXTENSIONS_DIR"] = extensions_stdout.strip()
    return env


# =============================================================================
# Hook Execution Helpers
# =============================================================================


def run_hook(
    hook_script: Path,
    url: str,
    snapshot_id: str,
    cwd: Path | None = None,
    env: dict | None = None,
    timeout: int = 60,
    extra_args: list[str] | None = None,
) -> tuple[int, str, str]:
    """Run a hook script and return (returncode, stdout, stderr).

    Chrome-aware wrapper: defaults env to get_test_env() (includes NODE_PATH etc.).
    """
    if env is None:
        env = get_test_env()
    return _base_run_hook(
        hook_script,
        url,
        snapshot_id,
        cwd=cwd,
        env=env,
        timeout=timeout,
        extra_args=extra_args,
    )


@contextmanager
def _chromium_install_lock(env: dict):
    """Serialize shared Chromium/Puppeteer installs across parallel test processes."""
    lib_dir = Path(env.get("ABXPKG_LIB_DIR") or get_lib_dir())
    lib_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lib_dir / ".chrome_install.lock"
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _has_node_module(env: dict, module_name: str) -> bool:
    """Return True if Node can resolve the requested package in this env."""
    probe_env = env.copy()
    node_binary = probe_env.get("NODE_BINARY", "")
    if not Path(node_binary).is_absolute() or not Path(node_binary).is_file():
        raise RuntimeError("NODE_BINARY was not resolved by abxpkg")
    node_modules_dir = probe_env.get("NODE_MODULES_DIR", "").strip()
    if node_modules_dir and not probe_env.get("NODE_PATH"):
        probe_env["NODE_PATH"] = node_modules_dir
    result = subprocess.run(
        [node_binary, "-e", "require.resolve(process.argv[1])", module_name],
        capture_output=True,
        text=True,
        timeout=20,
        env=probe_env,
    )
    return result.returncode == 0


def _has_puppeteer_module(env: dict) -> bool:
    """Return True if Node can resolve the puppeteer package in this env."""
    return _has_node_module(env, "puppeteer")


def _required_binary_record(
    plugin_dir: Path,
    name: str,
    env: dict[str, str],
) -> dict[str, Any]:
    for record in get_hydrated_required_binaries(plugin_dir, env=env):
        if record.get("name") == name:
            return record
    raise RuntimeError(
        f"{plugin_dir.name} config did not declare required_binaries entry for {name}",
    )


def _ensure_puppeteer_with_abxpkg(env: dict, timeout: int) -> None:
    """Install Chrome JS dependencies through plugin required_binaries.

    The Chrome JS hooks resolve their module paths from the provider-built env
    emitted by abxpkg/abx-dl/archivebox. Test setup has to use that same env
    because Chrome's required_binaries now span separate pnpm package roots
    such as playwright, abxbus, and @puppeteer/browsers.
    """
    env_result = _run_chrome_required_binary_env(
        env,
        timeout=timeout,
        install=True,
    )
    if env_result.returncode != 0:
        raise RuntimeError(
            f"Chrome dependency env preflight failed: {env_result.stderr or env_result.stdout}",
        )
    env.update(_parse_abxpkg_env_delta(env_result.stdout, base_env=env))

    if not _has_puppeteer_module(env):
        raise RuntimeError(
            "Chrome dependency env preflight completed but require.resolve('puppeteer') still fails",
        )
    if not _has_node_module(env, "abxbus"):
        raise RuntimeError(
            "Chrome dependency env preflight completed but require.resolve('abxbus') still fails",
        )


def resolve_node_with_abxpkg(env: dict) -> str:
    """Resolve Node through the Chrome plugin's declared abxpkg providers."""
    node_name = env.get("NODE_BINARY") or "node"
    node_record = _required_binary_record(CHROME_PLUGIN_DIR, node_name, env)
    loaded_node = load_required_binary(
        node_record,
        config=env,
        environ=env,
        install=True,
    )
    node_path = str(loaded_node.loaded_abspath or "")
    if not node_path or not Path(node_path).exists():
        raise RuntimeError(f"Node binary not found after install: {node_path}")
    env["NODE_BINARY"] = node_path
    return node_path


def install_chromium_with_abxpkg(env: dict, timeout: int = 300) -> str:
    """Resolve/install Chrome and its runtime dependencies through abxpkg."""
    with _chromium_install_lock(env):
        resolve_node_with_abxpkg(env)
        _ensure_puppeteer_with_abxpkg(env, timeout=timeout)

        chrome_name = env.get("CHROME_BINARY") or "chromium"
        chrome_record = _required_binary_record(CHROME_PLUGIN_DIR, chrome_name, env)
        loaded_chrome = load_required_binary(
            chrome_record,
            config=env,
            environ=env,
            install=True,
        )

        chrome_path = str(loaded_chrome.loaded_abspath or "")
        if not chrome_path or not Path(chrome_path).exists():
            raise RuntimeError(
                f"Chrome binary not found after install: {chrome_path}",
            )

        env["CHROME_BINARY"] = chrome_path
        return chrome_path


# =============================================================================
# Extension Test Helpers
# Used by extension tests (ublock, istilldontcareaboutcookies, twocaptcha)
# =============================================================================


def setup_test_env(tmpdir: Path) -> dict:
    """Set up an isolated, runtime-like Chrome test environment.

    The resulting env mirrors the production contract closely enough that
    extension tests can exercise the real launch/session lifecycle:
    - crawl state lives under ``<tmpdir>/crawl``
    - snapshot state lives under ``<tmpdir>/snap``
    - extension install locations come from abxpkg provider load/install state
    - persona-scoped Chrome state is derived by runtime config from
      ``PERSONAS_DIR``/``ACTIVE_PERSONA`` unless explicitly overridden
    - Chrome + pnpm dependencies are installed through hooks, not hand-written
      test setup

    Returns env dict with ``SNAP_DIR``, ``CRAWL_DIR``, ``PERSONAS_DIR``,
    ``ABXPKG_LIB_DIR``, ``NODE_MODULES_DIR``, ``NODE_PATH``, ``CHROME_BINARY``, etc.

    Args:
        tmpdir: Base temporary directory for the test

    Returns:
        Environment dict with all paths set.
    """

    tmpdir = Path(tmpdir).resolve()

    # Keep crawl/snap state rooted in the caller's tmpdir so every test is isolated.
    snap_dir = tmpdir / "snap"
    lib_dir = get_lib_dir()
    pnpm_dir = lib_dir / "pnpm" / "packages" / "chrome"
    pnpm_bin_dir = pnpm_dir / "node_modules" / ".bin"
    node_modules_dir = pnpm_dir / "node_modules"

    personas_dir = tmpdir / "personas"
    extensions_dir = tmpdir / "chromewebstore" / "extensions"
    home_dir = tmpdir / "home"
    xdg_config_home = home_dir / ".config"
    xdg_cache_home = home_dir / ".cache"
    xdg_data_home = home_dir / ".local" / "share"
    crawl_dir = tmpdir / "crawl"

    # Build complete env dict
    env = os.environ.copy()
    env.update(
        {
            "SNAP_DIR": str(snap_dir),
            "CRAWL_DIR": str(crawl_dir),
            "PERSONAS_DIR": str(personas_dir),
            "ACTIVE_PERSONA": "Default",
            "ABXPKG_CHROMEWEBSTORE_ROOT": str(extensions_dir.parent),
            "CHROMEWEBSTORE_EXTENSIONS_DIR": str(extensions_dir),
            "ABXPKG_LIB_DIR": str(lib_dir),
            "MACHINE_TYPE": get_machine_type(),
            "PNPM_BIN_DIR": str(pnpm_bin_dir),
            "NPM_BIN_DIR": str(pnpm_bin_dir),
            "NODE_MODULES_DIR": str(node_modules_dir),
            "HOME": str(home_dir),
            "XDG_CONFIG_HOME": str(xdg_config_home),
            "XDG_CACHE_HOME": str(xdg_cache_home),
            "XDG_DATA_HOME": str(xdg_data_home),
        },
    )
    for inherited_key in (
        "CHROME_DOWNLOADS_DIR",
        "CHROME_USER_DATA_DIR",
        "COOKIES_FILE",
    ):
        env.pop(inherited_key, None)
    assert Path(get_extensions_dir(env=env)).resolve() == extensions_dir

    # Create all directories
    node_modules_dir.mkdir(parents=True, exist_ok=True)
    pnpm_bin_dir.mkdir(parents=True, exist_ok=True)
    extensions_dir.mkdir(parents=True, exist_ok=True)
    personas_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    xdg_config_home.mkdir(parents=True, exist_ok=True)
    xdg_cache_home.mkdir(parents=True, exist_ok=True)
    xdg_data_home.mkdir(parents=True, exist_ok=True)
    snap_dir.mkdir(parents=True, exist_ok=True)
    crawl_dir.mkdir(parents=True, exist_ok=True)

    # Only set headless if not already in environment (allow override for debugging)
    if "CHROME_HEADLESS" not in os.environ:
        env["CHROME_HEADLESS"] = "true"

    assert_isolated_snapshot_env(env)

    try:
        install_chromium_with_abxpkg(env)
    except RuntimeError as e:
        raise RuntimeError(str(e))
    return env


def launch_chromium_session(
    env: dict,
    chrome_dir: Path,
    crawl_id: str,
    timeout: int = 120,
) -> tuple[LoggedPopen, str]:
    """Launch the crawl-level Chrome hook and return ``(process, cdp_url)``.

    This waits for the crawl hook to publish a browser-ready session in the
    crawl's ``chrome`` dir, not just for a child process to exist. Snapshot tab
    hooks require the same browser-ready contract before they attach.

    Args:
        env: Environment dict (from setup_test_env)
        chrome_dir: Directory for Chrome to write its files (cdp_url.txt, chrome.pid, etc.)
        crawl_id: ID for the crawl
        timeout: Maximum seconds to wait for cdp_url.txt

    Returns:
        Tuple of (chrome_launch_process, cdp_url)

    Raises:
        RuntimeError: If Chrome fails to launch or CDP URL not available after timeout
    """
    chrome_dir = Path(chrome_dir).resolve()
    crawl_dir = chrome_dir.parent
    crawl_dir.mkdir(parents=True, exist_ok=True)
    chrome_dir.mkdir(parents=True, exist_ok=True)

    # chrome_launch always writes to <CRAWL_DIR>/chrome, so force env/cwd to match.
    launch_env = env.copy()
    launch_env["CRAWL_DIR"] = str(crawl_dir)
    env["CRAWL_DIR"] = str(crawl_dir)
    stdout_log = chrome_dir / "chrome_launch.stdout.log"
    stderr_log = chrome_dir / "chrome_launch.stderr.log"
    stdout_handle = open(stdout_log, "w+", encoding="utf-8")
    stderr_handle = open(stderr_log, "w+", encoding="utf-8")

    chrome_launch_process = LoggedPopen(
        [str(CHROME_LAUNCH_HOOK), f"--crawl-id={crawl_id}"],
        cwd=str(chrome_dir),
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
        env=launch_env,
    )
    chrome_launch_process._stdout_handle = stdout_handle
    chrome_launch_process._stderr_handle = stderr_handle
    chrome_launch_process._stdout_log = stdout_log
    chrome_launch_process._stderr_log = stderr_log

    try:
        state = wait_for_chrome_session_state(
            chrome_dir,
            env=launch_env,
            timeout_seconds=timeout,
            require_browser_ready=True,
            require_connectable=True,
        )
    except (AssertionError, subprocess.TimeoutExpired) as exc:
        chrome_launch_process.send_signal(signal.SIGTERM)
        chrome_launch_process.wait(timeout=10)
        stdout_handle.flush()
        stderr_handle.flush()
        launch_stdout = stdout_log.read_text(encoding="utf-8", errors="replace")
        launch_stderr = stderr_log.read_text(encoding="utf-8", errors="replace")
        stdout_handle.close()
        stderr_handle.close()
        raise RuntimeError(
            f"Chromium launch failed:\nStdout: {launch_stdout}\nStderr: {launch_stderr}",
        ) from exc

    chrome_launch_process._chrome_pid = state.get("pid")
    cdp_url = str(state["cdpUrl"])
    return chrome_launch_process, cdp_url


def kill_chromium_session(
    chrome_launch_process: subprocess.Popen[str],
    chrome_dir: Path,
) -> None:
    """Clean up Chromium process launched by launch_chromium_session.

    Uses chrome_utils.js killChrome for proper process group handling.

    Args:
        chrome_launch_process: The Popen object from launch_chromium_session
        chrome_dir: The chrome directory containing chrome.pid
    """
    chrome_pid = getattr(chrome_launch_process, "_chrome_pid", None)
    if chrome_pid is None:
        chrome_pid_file = chrome_dir / "chrome.pid"
        if chrome_pid_file.exists():
            try:
                chrome_pid = int(chrome_pid_file.read_text().strip())
            except (ValueError, FileNotFoundError):
                chrome_pid = None

    cleanup_error: BaseException | None = None
    try:
        if chrome_launch_process.poll() is None:
            chrome_launch_process.send_signal(signal.SIGTERM)
        chrome_launch_process.wait(timeout=15)
    except BaseException as error:
        cleanup_error = error
    finally:
        try:
            if chrome_pid is not None and is_pid_alive(chrome_pid):
                assert kill_chrome(chrome_pid, str(chrome_dir))
            if chrome_pid is not None:
                assert not is_pid_alive(chrome_pid)
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error

        try:
            if chrome_launch_process.poll() is None:
                chrome_launch_process.kill()
            chrome_launch_process.wait(timeout=5)
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
        finally:
            for attr in ("_stdout_handle", "_stderr_handle"):
                handle = getattr(chrome_launch_process, attr, None)
                if handle:
                    handle.close()

    if cleanup_error is not None:
        raise cleanup_error


def launch_snapshot_tab(
    *,
    snapshot_chrome_dir: Path,
    tab_env: dict[str, str],
    test_url: str,
    snapshot_id: str,
    crawl_id: str,
    timeout: int = 60,
    require_pid: bool | None = None,
) -> LoggedPopen:
    """Launch the snapshot tab hook and wait for snapshot-level session markers.

    This waits only for tab/session marker publication. By default it requires
    ``chrome.pid`` only when the environment represents a local browser
    (`CHROME_IS_LOCAL` true and no `CHROME_CDP_URL`). Navigation is still a
    separate lifecycle step handled by the navigate hook and signaled later via
    ``navigation.json``.
    """
    stdout_log = snapshot_chrome_dir / "chrome_tab.stdout.log"
    stderr_log = snapshot_chrome_dir / "chrome_tab.stderr.log"
    stdout_handle = open(stdout_log, "w+", encoding="utf-8")
    stderr_handle = open(stderr_log, "w+", encoding="utf-8")
    tab_process = LoggedPopen(
        [
            str(CHROME_TAB_HOOK),
            f"--url={test_url}",
            f"--snapshot-id={snapshot_id}",
            f"--crawl-id={crawl_id}",
        ],
        cwd=str(snapshot_chrome_dir),
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
        env=tab_env,
    )
    tab_process._stdout_handle = stdout_handle
    tab_process._stderr_handle = stderr_handle

    if require_pid is None:
        cdp_url_override = (tab_env.get("CHROME_CDP_URL") or "").strip()
        is_local = (tab_env.get("CHROME_IS_LOCAL") or "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        require_pid = is_local and not cdp_url_override

    try:
        wait_for_chrome_session_state(
            snapshot_chrome_dir,
            env=tab_env,
            timeout_seconds=timeout,
            require_target_id=True,
            require_connectable=True,
        )
        if require_pid:
            assert (snapshot_chrome_dir / "chrome.pid").is_file()
        assert tab_process.poll() is None
        return tab_process
    except (AssertionError, subprocess.TimeoutExpired) as exc:
        tab_process.send_signal(signal.SIGTERM)
        tab_process.wait(timeout=10)
        stdout_handle.flush()
        stderr_handle.flush()
        stdout = stdout_log.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_log.read_text(encoding="utf-8", errors="replace")
        stdout_handle.close()
        stderr_handle.close()
        raise RuntimeError(
            f"Tab creation failed:\nStdout: {stdout}\nStderr: {stderr}",
        ) from exc


@contextmanager
def chrome_session(
    tmpdir: Path,
    crawl_id: str = "test-crawl",
    snapshot_id: str = "test-snapshot",
    test_url: str = "about:blank",
    navigate: bool = True,
    timeout: int = 15,
    env_overrides: dict[str, str] | None = None,
):
    """Context manager for the full crawl -> snapshot -> optional navigate flow.

    It models the real plugin lifecycle in miniature:
    1. provision crawl/snapshot dirs and runtime env
    2. launch the crawl-level shared browser
    3. wait for crawl readiness markers (including ``chrome.pid`` / ``cdp_url``)
    4. create a snapshot tab with its own session markers
    5. optionally run the navigate hook and wait for its outputs

    Runtime paths such as ``CHROME_BINARY`` and ``NODE_MODULES_DIR`` are
    consumed from the environment exported by the shared abxpkg fixture.

    Usage:
        with chrome_session(tmpdir, test_url='https://example.com') as (process, pid, chrome_dir, env):
            # Run tests with chrome session
            pass
        # Chrome automatically cleaned up

    Args:
        tmpdir: Temporary directory for test files
        crawl_id: ID to use for the crawl
        snapshot_id: ID to use for the snapshot
        test_url: URL to navigate to (if navigate=True)
        navigate: Whether to navigate to the URL after creating tab
        timeout: Seconds to wait for Chrome to start
        env_overrides: Runtime env values to preserve from caller setup

    Yields:
        Tuple of (chrome_launch_process, chrome_pid, snapshot_chrome_dir, env)

    Raises:
        RuntimeError: If Chrome fails to start or tab creation fails
    """
    chrome_launch_process = None
    tab_process = None
    chrome_pid = None
    chrome_dir: Path | None = None
    try:
        tmpdir = Path(tmpdir).resolve()
        # Model real runtime layout: one crawl root + one snapshot root per session.
        crawl_dir = tmpdir / "crawl" / crawl_id
        snap_dir = tmpdir / "snap" / snapshot_id
        personas_dir = tmpdir / "personas"
        extensions_dir = tmpdir / "chromewebstore" / "extensions"
        home_dir = tmpdir / "home"
        xdg_config_home = home_dir / ".config"
        xdg_cache_home = home_dir / ".cache"
        xdg_data_home = home_dir / ".local" / "share"
        env = os.environ.copy()

        # Consume the complete runtime paths exported by the shared abxpkg fixture.
        lib_dir = Path(env["ABXPKG_LIB_DIR"])
        node_modules_dir = Path(env["NODE_MODULES_DIR"])
        node_path = env["NODE_PATH"]
        pnpm_home = env["PNPM_HOME"]

        # Create crawl and snapshot directories
        crawl_dir.mkdir(parents=True, exist_ok=True)
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir(parents=True, exist_ok=True)

        # Build env with tmpdir-specific paths
        snap_dir.mkdir(parents=True, exist_ok=True)
        personas_dir.mkdir(parents=True, exist_ok=True)
        extensions_dir.mkdir(parents=True, exist_ok=True)
        home_dir.mkdir(parents=True, exist_ok=True)
        xdg_config_home.mkdir(parents=True, exist_ok=True)
        xdg_cache_home.mkdir(parents=True, exist_ok=True)
        xdg_data_home.mkdir(parents=True, exist_ok=True)

        env.update(
            {
                "SNAP_DIR": str(snap_dir),
                "CRAWL_DIR": str(crawl_dir),
                "PERSONAS_DIR": str(personas_dir),
                "ACTIVE_PERSONA": "Default",
                "ABXPKG_CHROMEWEBSTORE_ROOT": str(extensions_dir.parent),
                "CHROMEWEBSTORE_EXTENSIONS_DIR": str(extensions_dir),
                "ABXPKG_LIB_DIR": str(lib_dir),
                "MACHINE_TYPE": get_machine_type(),
                "NODE_MODULES_DIR": str(node_modules_dir),
                "NODE_PATH": node_path,
                "PNPM_HOME": pnpm_home,
                "PNPM_BIN_DIR": pnpm_home,
                "NPM_BIN_DIR": pnpm_home,
                "HOME": str(home_dir),
                "XDG_CONFIG_HOME": str(xdg_config_home),
                "XDG_CACHE_HOME": str(xdg_cache_home),
                "XDG_DATA_HOME": str(xdg_data_home),
                "CHROME_HEADLESS": "true",
            },
        )
        for inherited_key in (
            "CHROME_DOWNLOADS_DIR",
            "CHROME_USER_DATA_DIR",
            "COOKIES_FILE",
        ):
            env.pop(inherited_key, None)
        if env_overrides:
            env.update(env_overrides)
        chrome_timeout = int(env.get("CHROME_TIMEOUT") or "60")
        startup_timeout = max(int(timeout), chrome_timeout + 15)
        env.setdefault("CHROME_DEBUG_PORT_TIMEOUT_MS", str(startup_timeout * 1000))

        chrome_launch_process, _cdp_url = launch_chromium_session(
            env=env,
            chrome_dir=chrome_dir,
            crawl_id=crawl_id,
            timeout=startup_timeout,
        )
        chrome_pid = int((chrome_dir / "chrome.pid").read_text().strip())

        # Create snapshot directory structure
        snap_dir.mkdir(parents=True, exist_ok=True)
        snapshot_chrome_dir = snap_dir / "chrome"
        snapshot_chrome_dir.mkdir(parents=True, exist_ok=True)

        # Create tab. We explicitly pin both CRAWL_DIR and SNAP_DIR so hook state
        # files land in this session's isolated tmp tree.
        tab_env = env.copy()
        tab_env["CRAWL_DIR"] = str(crawl_dir)
        tab_env["SNAP_DIR"] = str(snap_dir)
        try:
            tab_process = launch_snapshot_tab(
                snapshot_chrome_dir=snapshot_chrome_dir,
                tab_env=tab_env,
                test_url=test_url,
                snapshot_id=snapshot_id,
                crawl_id=crawl_id,
                timeout=max(60, chrome_timeout + 15),
            )
        except RuntimeError:
            kill_chromium_session(chrome_launch_process, chrome_dir)
            raise

        # Navigate to URL if requested
        if navigate and CHROME_NAVIGATE_HOOK and test_url != "about:blank":
            try:
                result = subprocess.run(
                    [
                        str(CHROME_NAVIGATE_HOOK),
                        f"--url={test_url}",
                        f"--snapshot-id={snapshot_id}",
                    ],
                    cwd=str(snapshot_chrome_dir),
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=tab_env,
                )
                if result.returncode != 0:
                    kill_chromium_session(chrome_launch_process, chrome_dir)
                    raise RuntimeError(f"Navigation failed: {result.stderr}")
            except subprocess.TimeoutExpired:
                kill_chromium_session(chrome_launch_process, chrome_dir)
                raise RuntimeError("Navigation timed out after 120s")

        yield chrome_launch_process, chrome_pid, snapshot_chrome_dir, env
    finally:
        if tab_process:
            tab_process.send_signal(signal.SIGTERM)
            tab_process.wait(timeout=10)
        for attr in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(tab_process, attr, None) if tab_process else None
            if handle:
                handle.close()
        if chrome_launch_process and chrome_dir:
            kill_chromium_session(chrome_launch_process, chrome_dir)

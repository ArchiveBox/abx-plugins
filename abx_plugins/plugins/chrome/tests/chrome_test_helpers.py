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
        get_test_env,           # env dict with LIB_DIR, NODE_MODULES_DIR, MACHINE_TYPE
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
import platform
import signal
import fcntl
import re
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, TextIO
from contextlib import contextmanager

import pytest
from _pytest.fixtures import FixtureLookupError
from pytest_httpserver import HTTPServer
from werkzeug import Response

from abx_plugins.plugins.base.test_utils import (
    assert_isolated_snapshot_env,
    get_hydrated_required_binaries,
    run_hook as _base_run_hook,
)
from abx_plugins.plugins.base.utils import get_personas_dir, load_required_binary

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


def require_chrome_runtime_impl() -> None:
    """Require chrome runtime prerequisites for integration tests."""
    try:
        env = get_test_env()
        node_name = env.get("NODE_BINARY") or "node"
        node_record = _required_binary_record(CHROME_PLUGIN_DIR, node_name, env)
        load_required_binary(node_record, config=env, environ=env)
        chrome_binary = install_chromium_with_abxpkg(
            env,
            timeout=int(env.get("ABXPKG_INSTALL_TIMEOUT") or "300"),
        )
        existing_chrome_binary = os.environ.get("CHROME_BINARY")
        if not existing_chrome_binary or not Path(existing_chrome_binary).exists():
            os.environ["CHROME_BINARY"] = chrome_binary
    except Exception as exc:
        logger.error("Chrome integration prerequisites unavailable: %s", exc)
        pytest.fail(
            f"Chrome integration prerequisites unavailable: {exc}",
            pytrace=False,
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

    def slow_response(request):
        delay_ms = int(request.args.get("delay", "0") or "0")
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)
        return Response(
            f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Slow Page</title></head>
<body>
  <main>
    <h1>Slow Page</h1>
    <p>delay_ms={delay_ms}</p>
  </main>
</body>
</html>""",
            status=200,
            content_type="text/html; charset=utf-8",
        )

    httpserver.expect_request("/slow").respond_with_handler(slow_response)
    httpserver.expect_request("/popup-child").respond_with_data(
        """<!doctype html>
<html>
<head><meta charset="utf-8"><title>Popup Child</title></head>
<body><h1>Popup Child</h1><p>This popup should not replace the canonical snapshot target.</p></body>
</html>""",
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


def _add_https_test_urls(
    urls: dict[str, str],
    request,
    tmp_path_factory,
) -> dict[str, str]:
    """Add deterministic HTTPS URLs when an upstream fixture did not provide them."""
    if urls.get("https_base_url"):
        return urls

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


def ensure_chromium_and_puppeteer_installed_impl(tmp_path_factory) -> str:
    """Install Chrome and Puppeteer once for test sessions that require Chrome."""
    os.environ["SNAP_DIR"] = str(tmp_path_factory.mktemp("chrome_test_data"))
    os.environ["PERSONAS_DIR"] = str(tmp_path_factory.mktemp("chrome_test_personas"))
    os.environ["ACTIVE_PERSONA"] = "Default"
    os.environ["HOME"] = str(tmp_path_factory.mktemp("chrome_test_home"))
    os.environ["XDG_CONFIG_HOME"] = str(Path(os.environ["HOME"]) / ".config")
    os.environ["XDG_CACHE_HOME"] = str(Path(os.environ["HOME"]) / ".cache")
    os.environ["XDG_DATA_HOME"] = str(Path(os.environ["HOME"]) / ".local" / "share")
    os.environ.setdefault("LIB_DIR", str(tmp_path_factory.mktemp("chrome_test_lib")))
    for inherited_key in (
        "CHROME_DOWNLOADS_DIR",
        "CHROME_EXTENSIONS_DIR",
        "CHROME_USER_DATA_DIR",
        "COOKIES_FILE",
    ):
        os.environ.pop(inherited_key, None)

    for key in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "LIB_DIR",
    ):
        Path(os.environ[key]).mkdir(parents=True, exist_ok=True)

    env = get_test_env()
    chrome_binary = install_chromium_with_abxpkg(env)
    if not chrome_binary:
        raise RuntimeError("Chrome not found after install")

    existing_chrome_binary = os.environ.get("CHROME_BINARY")
    if not existing_chrome_binary or not Path(existing_chrome_binary).exists():
        os.environ["CHROME_BINARY"] = chrome_binary
    for key in ("NODE_MODULES_DIR", "NODE_PATH", "PATH"):
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
            return _add_https_test_urls(urls, request, tmp_path_factory)

    urls = _configure_chrome_httpserver(httpserver)
    return _add_https_test_urls(urls, request, tmp_path_factory)


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
# Path Helpers - delegates to chrome_utils.js with Python fallback
# Function names match JS: getMachineType -> get_machine_type, etc.
# =============================================================================


def _call_chrome_utils(
    command: str,
    *args: str,
    env: dict | None = None,
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
    cmd = [str(CHROME_UTILS), command] + list(args)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        env=env or os.environ.copy(),
    )
    return result.returncode, result.stdout, result.stderr


def _call_base_utils(
    command: str,
    *args: str,
    env: dict | None = None,
) -> tuple[int, str, str]:
    """Call shared JS base utilities from Python test code."""
    cmd = ["node", str(BASE_UTILS), command] + list(args)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        env=env or os.environ.copy(),
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
    deadline = time.time() + timeout_seconds
    last_parsed: Any = None

    while time.time() < deadline:
        timeout_ms = max(1, int((deadline - time.time()) * 1000))
        returncode, stdout, stderr = _call_chrome_utils(
            "readBrowserMetadata",
            str(chrome_dir),
            str(timeout_ms),
        )
        if returncode != 0:
            raise AssertionError(
                f"readBrowserMetadata failed for {chrome_dir}: {stderr or stdout}",
            )
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"Invalid JSON from readBrowserMetadata: {stdout}",
            ) from exc
        last_parsed = parsed
        if not isinstance(parsed, dict) or parsed.get("ready") is not True:
            raise AssertionError(
                f"Expected ready browser metadata for {chrome_dir}, got: {parsed}",
            )
        extensions = parsed.get("extensions")
        if isinstance(extensions, list) and extensions:
            return extensions
        time.sleep(0.1)

    raise AssertionError(
        f"Expected non-empty extension metadata list for {chrome_dir}, got: {last_parsed}",
    )


def write_browser_metadata(
    chrome_dir: Path,
    extensions: list[dict[str, Any]] | None = None,
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
            "node",
            "-e",
            script,
            str(CHROME_UTILS),
            str(chrome_dir),
            json.dumps(extensions or []),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=os.environ.copy(),
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


def close_target_via_cdp(cdp_url: str, target_id: str) -> None:
    """Close a real DevTools target through Chrome's HTTP endpoint."""
    port = port_from_cdp_url(cdp_url)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/close/{target_id}",
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=10):
        return


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
    result = subprocess.run(
        [str(CHROME_UTILS), "getCookiesViaCdp", str(port)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, (
        f"Failed to read cookies via CDP: {result.stderr}\nStdout: {result.stdout}"
    )
    payload = json.loads(result.stdout or "[]")
    assert isinstance(payload, list), payload
    return payload


def get_machine_type() -> str:
    """Get machine type string (e.g., 'x86_64-linux', 'arm64-darwin').

    Matches JS base/utils.js: getMachineType()

    Tries base/utils.js first, falls back to Python computation.
    """
    returncode, stdout, stderr = _call_base_utils("getMachineType")
    if returncode == 0 and stdout.strip():
        return stdout.strip()

    # Fallback to Python computation
    if os.environ.get("MACHINE_TYPE"):
        return os.environ["MACHINE_TYPE"]

    machine = platform.machine().lower()
    system = platform.system().lower()
    if machine in ("arm64", "aarch64"):
        machine = "arm64"
    elif machine in ("x86_64", "amd64"):
        machine = "x86_64"
    return f"{machine}-{system}"


def get_lib_dir() -> Path:
    """Get LIB_DIR path for shared binaries and provider-managed artifacts.

    Matches JS base/utils.js: getLibDir()

    Tries base/utils.js first, falls back to Python computation.
    """
    returncode, stdout, stderr = _call_base_utils("getLibDir")
    if returncode == 0 and stdout.strip():
        return Path(stdout.strip())

    # Fallback to Python
    if os.environ.get("LIB_DIR"):
        return Path(os.environ["LIB_DIR"])
    if platform.system().lower() == "darwin":
        return Path.home() / "Library" / "Application Support" / "abx" / "lib"
    if platform.system().lower() == "windows":
        return (
            Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
            / "abx"
            / "lib"
        )
    return (
        Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        / "abx"
        / "lib"
    )


def get_node_modules_dir() -> Path:
    """Get NODE_MODULES_DIR path for pnpm packages.

    Matches JS base/utils.js: getNodeModulesDir()

    Tries base/utils.js first, falls back to Python computation.
    """
    returncode, stdout, stderr = _call_base_utils("getNodeModulesDir")
    if returncode == 0 and stdout.strip():
        return Path(stdout.strip())

    # Fallback to Python
    if os.environ.get("NODE_MODULES_DIR"):
        return Path(os.environ["NODE_MODULES_DIR"])
    lib_dir = get_lib_dir()
    return lib_dir / "pnpm" / "packages" / "chrome" / "node_modules"


def get_extensions_dir(env: dict | None = None) -> str:
    """Get the Chrome extensions directory path.

    Matches JS base/utils.js: getChromeExtensionsDir()
    """
    returncode, stdout, stderr = _call_base_utils("getChromeExtensionsDir", env=env)
    if returncode != 0 or not stdout.strip():
        raise RuntimeError(
            f"base utils failed to resolve Chrome extensions dir: {stderr or stdout}",
        )
    return stdout.strip()


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
            "LIB_DIR": str(lib_dir),
        },
    )
    for inherited_key in (
        "CHROME_DOWNLOADS_DIR",
        "CHROME_EXTENSIONS_DIR",
        "CHROME_USER_DATA_DIR",
        "COOKIES_FILE",
    ):
        env.pop(inherited_key, None)
    extensions_dir = Path(get_extensions_dir(env=env))

    snap_dir.mkdir(parents=True, exist_ok=True)
    crawl_dir.mkdir(parents=True, exist_ok=True)
    personas_dir.mkdir(parents=True, exist_ok=True)
    extensions_dir.mkdir(parents=True, exist_ok=True)
    return env, extensions_dir


def find_chromium() -> str | None:
    """Find the Chromium binary path.

    Matches JS: findChromium()

    Uses chrome_utils.js which checks:
    - CHROME_BINARY env var
    - host Chromium locations
    - abxpkg-managed Puppeteer/Playwright provider shims under LIB_DIR

    Returns:
        Path to Chromium binary or None if not found
    """
    env = os.environ.copy()
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


def wait_for_pid_exit(pid: int, timeout_seconds: float = 15.0) -> bool:
    """Wait for a process to exit and return True if it did."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.25)
    return not is_pid_alive(pid)


def get_test_env() -> dict:
    """Get the shared runtime-like environment dict for Chrome plugin tests.

    Matches JS: getTestEnv()

    Tries ``base/utils.js`` first for path values, then builds an env dict on
    top of the current process environment. Use this for subprocess calls in
    plugin tests so ``LIB_DIR``, ``NODE_MODULES_DIR``, ``NODE_PATH``,
    ``CHROME_EXTENSIONS_DIR``, and related settings match the JS runtime
    contract.
    """
    env = os.environ.copy()

    returncode, stdout, stderr = _call_base_utils("getTestEnv")
    if returncode == 0 and stdout.strip():
        try:
            js_env = json.loads(stdout)
            env.update(js_env)
            return env
        except json.JSONDecodeError:
            pass

    # Fallback to Python computation
    lib_dir = get_lib_dir()
    env["LIB_DIR"] = str(lib_dir)
    env["NODE_MODULES_DIR"] = str(get_node_modules_dir())
    env["MACHINE_TYPE"] = get_machine_type()
    env.setdefault("SNAP_DIR", str(Path.cwd()))
    env.setdefault("CRAWL_DIR", str(Path.cwd()))
    env.setdefault("PERSONAS_DIR", str(get_personas_dir()))
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
    lib_dir = Path(env.get("LIB_DIR") or get_lib_dir())
    lib_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lib_dir / ".chrome_install.lock"
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _resolve_existing_chromium(env: dict) -> str | None:
    """Return an existing Chromium path if already installed and valid."""
    from_env = env.get("CHROME_BINARY")
    if from_env and Path(from_env).exists() and _is_supported_chromium(from_env, env):
        return from_env
    returncode, stdout, _stderr = _call_chrome_utils("findChromium", env=env)
    if returncode == 0 and stdout.strip():
        candidate = stdout.strip()
        if Path(candidate).exists() and _is_supported_chromium(candidate, env):
            return candidate
    return None


def _is_supported_chromium(binary_path: str, env: dict) -> bool:
    returncode, stdout, _stderr = _call_chrome_utils(
        "isSupportedChromiumBinary",
        binary_path,
        env=env,
    )
    return returncode == 0 and stdout.strip().lower() == "true"


def _has_node_module(env: dict, module_name: str) -> bool:
    """Return True if Node can resolve the requested package in this env."""
    probe_env = env.copy()
    node_modules_dir = probe_env.get("NODE_MODULES_DIR", "").strip()
    if node_modules_dir and not probe_env.get("NODE_PATH"):
        probe_env["NODE_PATH"] = node_modules_dir
    result = subprocess.run(
        ["node", "-e", "require.resolve(process.argv[1])", module_name],
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

    The Chrome JS hooks resolve Puppeteer from the shared runtime loader even
    when a browser binary already exists on disk, so test setup must ensure the
    package lifecycle is complete before attempting any launch/install flow.
    """
    lib_dir = Path(
        env.get("LIB_DIR") or os.environ.get("LIB_DIR") or get_lib_dir(),
    )
    pnpm_root = lib_dir / "pnpm" / "packages" / "chrome"

    node_modules_dir = pnpm_root / "node_modules"
    env.setdefault("NODE_MODULES_DIR", str(node_modules_dir))
    env.setdefault("NODE_PATH", str(node_modules_dir))
    pnpm_bin_dir = node_modules_dir / ".bin"
    env.setdefault("PNPM_BIN_DIR", str(pnpm_bin_dir))
    env.setdefault("NPM_BIN_DIR", str(pnpm_bin_dir))
    env["PATH"] = os.pathsep.join(
        [
            str(pnpm_bin_dir),
            *[
                part
                for part in env.get("PATH", os.environ.get("PATH", "")).split(
                    os.pathsep,
                )
                if part
            ],
        ],
    )

    if not _has_puppeteer_module(env):
        browsers_record = _required_binary_record(CHROME_PLUGIN_DIR, "browsers", env)
        load_required_binary(
            browsers_record,
            config=env,
            environ=env,
            install=True,
        )

    if not _has_node_module(env, "abxbus"):
        abxbus_record = _required_binary_record(CHROME_PLUGIN_DIR, "abxbus", env)
        load_required_binary(
            abxbus_record,
            config=env,
            environ=env,
            install=True,
        )

    if not _has_puppeteer_module(env):
        raise RuntimeError(
            "Puppeteer dependency preflight completed but require.resolve('puppeteer') still fails",
        )
    if not _has_node_module(env, "abxbus"):
        raise RuntimeError(
            "Chrome abxbus dependency preflight completed but require.resolve('abxbus') still fails",
        )


def install_chromium_with_abxpkg(env: dict, timeout: int = 300) -> str:
    """Install Chrome via abxpkg providers.

    The order matters:
    1. ensure the ``puppeteer`` JS package exists
    2. reuse an existing Chrome if one is already valid for this env
    3. otherwise install Chrome with the Puppeteer provider

    Returns absolute path to Chrome binary.
    """
    with _chromium_install_lock(env):
        # Always ensure JS dependency exists, even if Chrome already exists
        # on the host. chrome_launch resolves Puppeteer at runtime.
        _ensure_puppeteer_with_abxpkg(env, timeout=timeout)

        existing = _resolve_existing_chromium(env)
        if existing:
            env["CHROME_BINARY"] = existing
            return existing

        chrome_name = env.get("CHROME_BINARY") or "chromium"
        chrome_record = _required_binary_record(CHROME_PLUGIN_DIR, chrome_name, env)

        from abxpkg import Binary, PuppeteerProvider, SemVer

        lib_dir = Path(
            env.get("LIB_DIR") or os.environ.get("LIB_DIR") or get_lib_dir(),
        )
        chrome_binary = Binary(
            name=chrome_record.get("name", "chrome"),
            min_version=SemVer(chrome_record["min_version"])
            if chrome_record.get("min_version")
            else None,
            binproviders=[
                PuppeteerProvider(
                    install_root=lib_dir / "puppeteer",
                    install_timeout=timeout,
                ),
            ],
            postinstall_scripts=chrome_record.get("postinstall_scripts"),
            overrides=chrome_record.get("overrides") or {},
        )
        loaded_chrome = chrome_binary.install()

        chrome_path = str(loaded_chrome.loaded_abspath or "")
        if not chrome_path or not Path(chrome_path).exists():
            raise RuntimeError(
                f"Chrome binary not found after install: {chrome_path}",
            )

        env["CHROME_BINARY"] = chrome_path

        resolved = _resolve_existing_chromium(env)
        if resolved:
            env["CHROME_BINARY"] = resolved
            return resolved
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
    - install-scoped ``chromewebstore/extensions`` is resolved by the Chrome
      config helper
    - persona-scoped Chrome state is derived by runtime config from
      ``PERSONAS_DIR``/``ACTIVE_PERSONA`` unless explicitly overridden
    - Chrome + pnpm dependencies are installed through hooks, not hand-written
      test setup

    Returns env dict with ``SNAP_DIR``, ``CRAWL_DIR``, ``PERSONAS_DIR``,
    ``LIB_DIR``, ``NODE_MODULES_DIR``, ``NODE_PATH``, ``CHROME_BINARY``, etc.

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
            "LIB_DIR": str(lib_dir),
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
        "CHROME_EXTENSIONS_DIR",
        "CHROME_USER_DATA_DIR",
        "COOKIES_FILE",
    ):
        env.pop(inherited_key, None)
    chrome_extensions_dir = Path(get_extensions_dir(env=env))

    # Create all directories
    node_modules_dir.mkdir(parents=True, exist_ok=True)
    pnpm_bin_dir.mkdir(parents=True, exist_ok=True)
    personas_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    xdg_config_home.mkdir(parents=True, exist_ok=True)
    xdg_cache_home.mkdir(parents=True, exist_ok=True)
    xdg_data_home.mkdir(parents=True, exist_ok=True)
    chrome_extensions_dir.mkdir(parents=True, exist_ok=True)
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

    cdp_url = None
    launch_exit_code = None
    launch_stdout = ""
    launch_stderr = ""

    for _ in range(timeout):
        cdp_file = chrome_dir / "cdp_url.txt"
        browser_file = chrome_dir / "browser.json"
        browser_ready = False
        if browser_file.exists():
            try:
                browser_ready = bool(json.loads(browser_file.read_text()).get("ready"))
            except (json.JSONDecodeError, OSError):
                browser_ready = False
        if cdp_file.exists() and browser_ready:
            cdp_url = cdp_file.read_text().strip()
            if cdp_url:
                break
        process_status = chrome_launch_process.poll()
        if process_status is not None:
            stdout_handle.flush()
            stderr_handle.flush()
            if cdp_file.exists() and browser_ready:
                cdp_url = cdp_file.read_text().strip()
                if cdp_url:
                    break
            launch_exit_code = process_status
        time.sleep(1)

    if cdp_url:
        chrome_pid_file = chrome_dir / "chrome.pid"
        if chrome_pid_file.exists():
            try:
                chrome_launch_process._chrome_pid = int(
                    chrome_pid_file.read_text().strip(),
                )
            except (ValueError, FileNotFoundError):
                chrome_launch_process._chrome_pid = None
        else:
            chrome_launch_process._chrome_pid = None
        return chrome_launch_process, cdp_url

    if launch_exit_code is None:
        chrome_launch_process.kill()
        stdout_handle.flush()
        stderr_handle.flush()
        launch_stdout = stdout_log.read_text(encoding="utf-8", errors="replace")
        launch_stderr = stderr_log.read_text(encoding="utf-8", errors="replace")
    else:
        launch_stdout = stdout_log.read_text(encoding="utf-8", errors="replace")
        launch_stderr = stderr_log.read_text(encoding="utf-8", errors="replace")

    stdout_handle.close()
    stderr_handle.close()

    if launch_exit_code is not None:
        raise RuntimeError(
            f"Chromium launch failed:\nStdout: {launch_stdout}\nStderr: {launch_stderr}",
        )

    raise RuntimeError(
        f"Chromium CDP URL not found after {timeout}s\nStdout: {launch_stdout}\nStderr: {launch_stderr}",
    )


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

    # First try to terminate the launch process gracefully
    try:
        chrome_launch_process.send_signal(signal.SIGTERM)
        chrome_launch_process.wait(timeout=5)
    except Exception:
        pass

    if (
        chrome_pid is not None
        and is_pid_alive(chrome_pid)
        and not wait_for_pid_exit(chrome_pid)
    ):
        kill_chrome(chrome_pid, str(chrome_dir))

    for attr in ("_stdout_handle", "_stderr_handle"):
        handle = getattr(chrome_launch_process, attr, None)
        if handle:
            handle.close()


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

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tab_process.poll() is not None:
            stdout_handle.flush()
            stderr_handle.flush()
            stdout = stdout_log.read_text(encoding="utf-8", errors="replace")
            stderr = stderr_log.read_text(encoding="utf-8", errors="replace")
            stdout_handle.close()
            stderr_handle.close()
            raise RuntimeError(
                f"Tab creation exited early:\nStdout: {stdout}\nStderr: {stderr}",
            )
        cdp_ready = (snapshot_chrome_dir / "cdp_url.txt").exists()
        target_ready = (snapshot_chrome_dir / "target_id.txt").exists()
        pid_ready = (snapshot_chrome_dir / "chrome.pid").exists()
        if cdp_ready and target_ready and (pid_ready or not require_pid):
            return tab_process
        time.sleep(0.2)

    try:
        tab_process.send_signal(signal.SIGTERM)
        tab_process.wait(timeout=10)
    except Exception:
        pass
    stdout_handle.flush()
    stderr_handle.flush()
    stdout = stdout_log.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_log.read_text(encoding="utf-8", errors="replace")
    stdout_handle.close()
    stderr_handle.close()
    raise RuntimeError(
        f"Tab creation timed out after {timeout}s\nStdout: {stdout}\nStderr: {stderr}",
    )


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

    Runtime overrides such as an already-exported ``CHROME_BINARY`` or
    ``NODE_MODULES_DIR`` remain authoritative so test harnesses can inject
    preinstalled browsers without fighting the helper.

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
        home_dir = tmpdir / "home"
        xdg_config_home = home_dir / ".config"
        xdg_cache_home = home_dir / ".cache"
        xdg_data_home = home_dir / ".local" / "share"
        env = os.environ.copy()

        # Prefer an already-provisioned NODE_MODULES_DIR (set by session-level chrome fixture)
        # so we don't force per-test reinstall under tmp LIB_DIR paths.
        existing_node_modules = env.get("NODE_MODULES_DIR")
        if existing_node_modules and Path(existing_node_modules).exists():
            node_modules_dir = Path(existing_node_modules).resolve()
            pnpm_dir = node_modules_dir.parent.parent
            lib_dir = pnpm_dir.parent.parent
        else:
            lib_dir = get_lib_dir()
            pnpm_dir = lib_dir / "pnpm" / "packages" / "chrome"
            node_modules_dir = pnpm_dir / "node_modules"
        # Create lib structure for puppeteer installation
        node_modules_dir.mkdir(parents=True, exist_ok=True)

        # Create crawl and snapshot directories
        crawl_dir.mkdir(parents=True, exist_ok=True)
        chrome_dir = crawl_dir / "chrome"
        chrome_dir.mkdir(parents=True, exist_ok=True)

        # Build env with tmpdir-specific paths
        snap_dir.mkdir(parents=True, exist_ok=True)
        personas_dir.mkdir(parents=True, exist_ok=True)
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
                "LIB_DIR": str(lib_dir),
                "MACHINE_TYPE": get_machine_type(),
                "NODE_MODULES_DIR": str(node_modules_dir),
                "NODE_PATH": str(node_modules_dir),
                "PNPM_BIN_DIR": str(node_modules_dir / ".bin"),
                "NPM_BIN_DIR": str(node_modules_dir / ".bin"),
                "HOME": str(home_dir),
                "XDG_CONFIG_HOME": str(xdg_config_home),
                "XDG_CACHE_HOME": str(xdg_cache_home),
                "XDG_DATA_HOME": str(xdg_data_home),
                "CHROME_HEADLESS": "true",
            },
        )
        for inherited_key in (
            "CHROME_DOWNLOADS_DIR",
            "CHROME_EXTENSIONS_DIR",
            "CHROME_USER_DATA_DIR",
            "COOKIES_FILE",
        ):
            env.pop(inherited_key, None)
        if env_overrides:
            env.update(env_overrides)
        chrome_extensions_dir = Path(get_extensions_dir(env=env))
        chrome_extensions_dir.mkdir(parents=True, exist_ok=True)
        chrome_timeout = int(env.get("CHROME_TIMEOUT") or "60")
        startup_timeout = max(int(timeout), chrome_timeout + 15)
        env.setdefault("CHROME_DEBUG_PORT_TIMEOUT_MS", str(startup_timeout * 1000))

        # Reuse already-provisioned Chromium when available (session fixture sets CHROME_BINARY).
        # Falling back to install on each test is slow and can hang on flaky networks.
        chrome_binary = env.get("CHROME_BINARY")
        if not chrome_binary or not Path(chrome_binary).exists():
            chrome_binary = install_chromium_with_abxpkg(env)
            env["CHROME_BINARY"] = chrome_binary

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
        if tab_process and tab_process.poll() is None:
            try:
                tab_process.send_signal(signal.SIGTERM)
                tab_process.wait(timeout=10)
            except Exception:
                pass
        for attr in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(tab_process, attr, None) if tab_process else None
            if handle:
                handle.close()
        if chrome_launch_process and chrome_dir:
            kill_chromium_session(chrome_launch_process, chrome_dir)

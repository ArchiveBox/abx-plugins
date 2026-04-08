"""
Shared Chrome test helpers for plugin integration tests.

This module provides common utilities for Chrome-based plugin tests, reducing
duplication across test files. Functions delegate to chrome_utils.js (the single
source of truth) with Python fallbacks.

Function names match the JS equivalents in snake_case:
    JS: getMachineType()  -> Python: get_machine_type()
    JS: getLibDir()       -> Python: get_lib_dir()
    JS: getNodeModulesDir() -> Python: get_node_modules_dir()
    JS: getExtensionsDir() -> Python: get_extensions_dir()
    JS: findChromium()    -> Python: find_chromium()
    JS: killChrome()      -> Python: kill_chrome()
    JS: getTestEnv()      -> Python: get_test_env()

Usage:
    # Path helpers (delegate to chrome_utils.js):
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
        cleanup_chrome,         # Manual cleanup by PID (rarely needed)
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
import os
import platform
import signal
import ssl
import fcntl
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO
from contextlib import contextmanager

import pytest
from _pytest.fixtures import FixtureLookupError

from abx_plugins.plugins.base.test_utils import (
    assert_isolated_snapshot_env,
    get_hydrated_required_binaries,
    parse_jsonl_output,
    parse_jsonl_records,
    run_hook as _base_run_hook,
    run_hook_and_parse as _base_run_hook_and_parse,
)
from abx_plugins.plugins.base.utils import get_personas_dir

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
PUPPETEER_BINARY_HOOK = PLUGINS_ROOT / "puppeteer" / "on_BinaryRequest__12_puppeteer.py"
NPM_BINARY_HOOK = PLUGINS_ROOT / "npm" / "on_BinaryRequest__10_npm.py"


# Prefer root-level URL fixtures if they exist, otherwise fall back to a local server.
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


class _DeterministicTestRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves predictable pages for Chrome-dependent tests."""

    server_version = "ABXDeterministicHTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep pytest output clean unless a test fails.
        return

    def _origin(self) -> str:
        host = self.headers.get("Host", "127.0.0.1")
        scheme = "https" if isinstance(self.connection, ssl.SSLSocket) else "http"
        return f"{scheme}://{host}"

    def _write(
        self,
        status: int,
        body: str,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path or "/"
        origin = self._origin()

        if path in ("/", "/index.html"):
            html = f"""<!doctype html>
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
            self._write(200, html)
            return

        if path == "/linked":
            self._write(
                200,
                "<html><head><title>Linked Page</title></head><body><h1>Linked Page</h1></body></html>",
            )
            return

        if path == "/slow":
            delay_ms = 5000
            try:
                delay_ms = max(
                    0,
                    int(urllib.parse.parse_qs(parsed.query).get("delay", ["5000"])[0]),
                )
            except Exception:
                delay_ms = 5000
            time.sleep(delay_ms / 1000.0)
            self._write(
                200,
                f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Slow Page {delay_ms}</title></head>
<body>
  <main>
    <h1>Slow Page</h1>
    <p>delay_ms={delay_ms}</p>
  </main>
</body>
</html>""",
            )
            return

        if path == "/popup-child":
            self._write(
                200,
                """<!doctype html>
<html>
<head><meta charset="utf-8"><title>Popup Child</title></head>
<body><h1>Popup Child</h1><p>This popup should not replace the canonical snapshot target.</p></body>
</html>""",
            )
            return

        if path == "/popup-parent":
            html = f"""<!doctype html>
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
</html>"""
            self._write(200, html)
            return

        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return

        if path in ("/nonexistent-page-404", "/not-found"):
            self._write(
                404,
                "<html><head><title>Not Found</title></head><body><h1>404 Not Found</h1></body></html>",
            )
            return

        if path == "/static/test.txt":
            self._write(
                200,
                "static fixture payload",
                content_type="text/plain; charset=utf-8",
            )
            return

        if path == "/api/data.json":
            self._write(
                200,
                '{"ok": true, "source": "deterministic-fixture"}',
                content_type="application/json",
            )
            return

        if path == "/claudechrome":
            self._write(
                200,
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
            return

        self._write(
            404,
            "<html><head><title>Not Found</title></head><body><h1>404</h1></body></html>",
        )


def _start_local_server(
    *,
    use_tls: bool = False,
    cert_file: Path | None = None,
    key_file: Path | None = None,
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _DeterministicTestRequestHandler)
    server.daemon_threads = True
    if use_tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
        server.socket = context.wrap_socket(server.socket, server_side=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _generate_self_signed_cert(tmpdir: Path) -> tuple[Path, Path] | None:
    cert_file = tmpdir / "local-test-cert.pem"
    key_file = tmpdir / "local-test-key.pem"
    command = [
        "openssl",
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "2",
        "-subj",
        "/CN=127.0.0.1",
        "-addext",
        "subjectAltName=DNS:localhost,IP:127.0.0.1",
        "-keyout",
        str(key_file),
        "-out",
        str(cert_file),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        fallback = [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=127.0.0.1",
            "-keyout",
            str(key_file),
            "-out",
            str(cert_file),
        ]
        result = subprocess.run(fallback, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return cert_file, key_file


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
    """Install Chromium and Puppeteer once for test sessions that require Chrome."""
    os.environ["SNAP_DIR"] = str(tmp_path_factory.mktemp("chrome_test_data"))
    os.environ["PERSONAS_DIR"] = str(tmp_path_factory.mktemp("chrome_test_personas"))
    os.environ["HOME"] = str(tmp_path_factory.mktemp("chrome_test_home"))
    os.environ["XDG_CONFIG_HOME"] = str(Path(os.environ["HOME"]) / ".config")
    os.environ["XDG_CACHE_HOME"] = str(Path(os.environ["HOME"]) / ".cache")
    os.environ["XDG_DATA_HOME"] = str(Path(os.environ["HOME"]) / ".local" / "share")

    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        Path(os.environ[key]).mkdir(parents=True, exist_ok=True)

    env = get_test_env()
    chromium_binary = install_chromium_with_hooks(env)
    if not chromium_binary:
        raise RuntimeError("Chromium not found after install")

    existing_chrome_binary = os.environ.get("CHROME_BINARY")
    if not existing_chrome_binary or not Path(existing_chrome_binary).exists():
        os.environ["CHROME_BINARY"] = chromium_binary
    for key in ("NODE_MODULES_DIR", "NODE_PATH", "PATH"):
        if env.get(key):
            os.environ[key] = env[key]
    return chromium_binary


ensure_chromium_and_puppeteer_installed = pytest.fixture(scope="session")(
    ensure_chromium_and_puppeteer_installed_impl,
)


@pytest.fixture(scope="session")
def chrome_test_urls(request, tmp_path_factory):
    """Provide deterministic test URLs, preferring a root conftest fixture when available."""
    for fixture_name in _ROOT_URL_FIXTURE_NAMES:
        try:
            upstream = request.getfixturevalue(fixture_name)
        except FixtureLookupError:
            continue
        urls = _coerce_upstream_urls(upstream)
        if urls:
            return urls

    server_tmpdir = tmp_path_factory.mktemp("chrome_test_server")
    http_server, _http_thread = _start_local_server()
    https_server = None
    https_urls = None

    cert_pair = _generate_self_signed_cert(server_tmpdir)
    if cert_pair:
        cert_file, key_file = cert_pair
        https_server, _https_thread = _start_local_server(
            use_tls=True,
            cert_file=cert_file,
            key_file=key_file,
        )
        https_urls = f"https://chrome-test.localhost:{https_server.server_port}"

    urls = _build_test_urls(
        f"http://chrome-test.localhost:{http_server.server_port}",
        https_urls,
    )
    try:
        yield urls
    finally:
        # Cleanly end the background servers so later test sessions do not reuse stale ports.
        http_server.shutdown()
        http_server.server_close()
        if https_server:
            https_server.shutdown()
            https_server.server_close()


@pytest.fixture(scope="session")
def chrome_test_url(chrome_test_urls):
    return chrome_test_urls["base_url"]


@pytest.fixture(scope="session")
def chrome_test_https_url(chrome_test_urls):
    https_url = chrome_test_urls.get("https_base_url")
    assert https_url, "Local HTTPS fixture unavailable (openssl required)"
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


def wait_for_extensions_metadata(
    chrome_dir: Path,
    timeout_seconds: int = 10,
) -> list[dict[str, Any]]:
    """Wait for ``extensions.json`` to be published and return its parsed records.

    Extension-backed hooks should treat this as the post-launch readiness gate
    for extension discovery metadata. It is stronger than merely seeing
    ``chrome.pid`` or ``cdp_url.txt`` because the browser may still be in the
    startup window before extensions finish loading.
    """
    timeout_ms = max(1, int(timeout_seconds * 1000))
    returncode, stdout, stderr = _call_chrome_utils(
        "readExtensionsMetadata",
        str(chrome_dir),
        str(timeout_ms),
    )
    if returncode != 0:
        raise AssertionError(
            f"readExtensionsMetadata failed for {chrome_dir}: {stderr or stdout}",
        )
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"Invalid JSON from readExtensionsMetadata: {stdout}",
        ) from exc
    if not isinstance(parsed, list) or not parsed:
        raise AssertionError(
            f"Expected non-empty extension metadata list for {chrome_dir}, got: {parsed}",
        )
    return parsed


def get_machine_type() -> str:
    """Get machine type string (e.g., 'x86_64-linux', 'arm64-darwin').

    Matches JS: getMachineType()

    Tries chrome_utils.js first, falls back to Python computation.
    """
    # Try JS first (single source of truth)
    returncode, stdout, stderr = _call_chrome_utils("getMachineType")
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
    """Get LIB_DIR path for shared binaries and caches.

    Matches JS: getLibDir()

    Tries chrome_utils.js first, falls back to Python computation.
    """
    # Try JS first
    returncode, stdout, stderr = _call_chrome_utils("getLibDir")
    if returncode == 0 and stdout.strip():
        return Path(stdout.strip())

    # Fallback to Python
    if os.environ.get("LIB_DIR"):
        return Path(os.environ["LIB_DIR"])
    return Path.home() / ".config" / "abx" / "lib"


def get_node_modules_dir() -> Path:
    """Get NODE_MODULES_DIR path for npm packages.

    Matches JS: getNodeModulesDir()

    Tries chrome_utils.js first, falls back to Python computation.
    """
    # Try JS first
    returncode, stdout, stderr = _call_chrome_utils("getNodeModulesDir")
    if returncode == 0 and stdout.strip():
        return Path(stdout.strip())

    # Fallback to Python
    if os.environ.get("NODE_MODULES_DIR"):
        return Path(os.environ["NODE_MODULES_DIR"])
    lib_dir = get_lib_dir()
    return lib_dir / "npm" / "node_modules"


def get_extensions_dir() -> str:
    """Get the Chrome extensions directory path.

    Matches JS: getExtensionsDir()

    Tries chrome_utils.js first, falls back to Python computation.
    """
    try:
        returncode, stdout, stderr = _call_chrome_utils("getExtensionsDir")
        if returncode == 0 and stdout.strip():
            return stdout.strip()
    except subprocess.TimeoutExpired:
        pass  # Fall through to default computation

    # Fallback to default computation if JS call fails
    personas_dir = os.environ.get("PERSONAS_DIR") or str(
        Path.home() / ".config" / "abx" / "personas",
    )
    persona = os.environ.get("ACTIVE_PERSONA", "Default")
    return str(Path(personas_dir) / persona / "chrome_extensions")


def link_puppeteer_cache(lib_dir: Path) -> None:
    """Best-effort symlink from system Puppeteer cache into test lib_dir.

    Avoids repeated Chromium downloads across tests by reusing the
    default Puppeteer cache directory.
    """
    cache_dir = lib_dir / "puppeteer" / "chrome"
    cache_dir.mkdir(parents=True, exist_ok=True)

    candidates = [
        Path.home() / "Library" / "Caches" / "puppeteer",
        Path.home() / ".cache" / "puppeteer",
    ]
    for src_root in candidates:
        if not src_root.exists():
            continue
        for item in src_root.iterdir():
            dst = cache_dir / item.name
            if dst.exists():
                continue
            try:
                os.symlink(item, dst, target_is_directory=item.is_dir())
            except Exception:
                # Best-effort only; if symlink fails, leave as-is.
                pass


def find_chromium(data_dir: str | None = None) -> str | None:
    """Find the Chromium binary path.

    Matches JS: findChromium()

    Uses chrome_utils.js which checks:
    - CHROME_BINARY env var
    - hook-managed LIB_DIR install locations
    - @puppeteer/browsers / Puppeteer cache locations
    - System Chromium locations

    Args:
        data_dir: Optional SNAP_DIR override

    Returns:
        Path to Chromium binary or None if not found
    """
    env = os.environ.copy()
    if data_dir:
        env["SNAP_DIR"] = str(data_dir)
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


def _is_pid_alive(pid: int) -> bool:
    """Return True if the process still exists."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    return True


def _wait_for_pid_exit(pid: int, timeout_seconds: float = 15.0) -> bool:
    """Wait for a process to exit and return True if it did."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(0.25)
    return not _is_pid_alive(pid)


def get_test_env() -> dict:
    """Get the shared runtime-like environment dict for Chrome plugin tests.

    Matches JS: getTestEnv()

    Tries ``chrome_utils.js`` first for path values, then builds an env dict on
    top of the current process environment. Use this for subprocess calls in
    plugin tests so ``LIB_DIR``, ``NODE_MODULES_DIR``, ``NODE_PATH``,
    ``CHROME_EXTENSIONS_DIR``, and related settings match the JS runtime
    contract.
    """
    env = os.environ.copy()

    # Try to get all paths from JS (single source of truth)
    returncode, stdout, stderr = _call_chrome_utils("getTestEnv")
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


# Backward compatibility aliases (deprecated, use new names)
find_chromium_binary = find_chromium
kill_chrome_via_js = kill_chrome
get_machine_type_from_js = get_machine_type
get_test_env_from_js = get_test_env


# =============================================================================
# Module-level constants (lazy-loaded on first access)
# Import these directly: from chrome_test_helpers import LIB_DIR, NODE_MODULES_DIR
# =============================================================================

# These are computed once when first accessed
_LIB_DIR: Path | None = None
_NODE_MODULES_DIR: Path | None = None


def _get_lib_dir_cached() -> Path:
    global _LIB_DIR
    if _LIB_DIR is None:
        _LIB_DIR = get_lib_dir()
    return _LIB_DIR


def _get_node_modules_dir_cached() -> Path:
    global _NODE_MODULES_DIR
    if _NODE_MODULES_DIR is None:
        _NODE_MODULES_DIR = get_node_modules_dir()
    return _NODE_MODULES_DIR


# Module-level constants that can be imported directly
# Usage: from chrome_test_helpers import LIB_DIR, NODE_MODULES_DIR
class _LazyPath:
    """Lazy path that computes value on first access."""

    def __init__(self, getter):
        self._getter = getter
        self._value = None

    def __fspath__(self):
        if self._value is None:
            self._value = self._getter()
        return str(self._value)

    def __truediv__(self, other):
        if self._value is None:
            self._value = self._getter()
        return self._value / other

    def __str__(self):
        return self.__fspath__()

    def __repr__(self):
        return f"<LazyPath: {self.__fspath__()}>"


LIB_DIR = _LazyPath(_get_lib_dir_cached)
NODE_MODULES_DIR = _LazyPath(_get_node_modules_dir_cached)


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


def apply_machine_updates(records: list[dict[str, Any]], env: dict) -> None:
    """Apply Machine update records to env dict in-place."""
    for record in records:
        if record.get("type") != "Machine":
            continue
        config = record.get("config")
        if not isinstance(config, dict):
            continue
        env.update(config)


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
    if from_env and Path(from_env).exists():
        return from_env
    returncode, stdout, _stderr = _call_chrome_utils("findChromium", env=env)
    if returncode == 0 and stdout.strip():
        candidate = stdout.strip()
        if Path(candidate).exists():
            return candidate
    return None


def _has_puppeteer_module(env: dict) -> bool:
    """Return True if Node can resolve the puppeteer package in this env."""
    probe_env = env.copy()
    node_modules_dir = probe_env.get("NODE_MODULES_DIR", "").strip()
    if node_modules_dir and not probe_env.get("NODE_PATH"):
        probe_env["NODE_PATH"] = node_modules_dir
    result = subprocess.run(
        ["node", "-e", "require.resolve('puppeteer')"],
        capture_output=True,
        text=True,
        timeout=20,
        env=probe_env,
    )
    return result.returncode == 0


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


def _ensure_puppeteer_with_hooks(env: dict, timeout: int) -> None:
    """Install the JS ``puppeteer`` package through the plugin hook chain.

    The Chrome JS hooks resolve Puppeteer from the shared runtime loader even
    when a browser binary already exists on disk, so test setup must ensure the
    npm package lifecycle is complete before attempting any launch/install flow.
    """
    if _has_puppeteer_module(env):
        return

    puppeteer_record = _required_binary_record(
        PLUGINS_ROOT / "puppeteer",
        "puppeteer",
        env,
    )

    npm_cmd = [
        str(NPM_BINARY_HOOK),
        "--name=puppeteer",
        f"--binproviders={puppeteer_record.get('binproviders', '*')}",
    ]
    puppeteer_overrides = puppeteer_record.get("overrides")
    if puppeteer_overrides:
        npm_cmd.append(f"--overrides={json.dumps(puppeteer_overrides)}")

    npm_result = subprocess.run(
        npm_cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if npm_result.returncode != 0:
        raise RuntimeError(
            f"Npm puppeteer install failed:\nstdout: {npm_result.stdout}\nstderr: {npm_result.stderr}",
        )

    apply_machine_updates(parse_jsonl_records(npm_result.stdout), env)
    if env.get("NODE_MODULES_DIR") and not env.get("NODE_PATH"):
        env["NODE_PATH"] = env["NODE_MODULES_DIR"]

    if not _has_puppeteer_module(env):
        raise RuntimeError(
            "Puppeteer dependency preflight completed but require.resolve('puppeteer') still fails",
        )


def install_chromium_with_hooks(env: dict, timeout: int = 300) -> str:
    """Install Chromium via the same hook sequence used by runtime code.

    The order matters:
    1. ensure the ``puppeteer`` JS package exists
    2. reuse an existing Chromium if one is already valid for this env
    3. otherwise emit the Chrome BinaryRequest record and satisfy it via the Puppeteer
       binary hook

    Any Machine updates emitted by hooks are folded back into ``env`` so later
    subprocesses inherit the resolved ``CHROME_BINARY`` / npm path settings.

    Returns absolute path to Chromium binary.
    """
    with _chromium_install_lock(env):
        # Always ensure JS dependency exists, even if Chromium already exists
        # on the host. chrome_launch resolves Puppeteer at runtime.
        _ensure_puppeteer_with_hooks(env, timeout=timeout)

        existing = _resolve_existing_chromium(env)
        if existing:
            env["CHROME_BINARY"] = existing
            return existing

        chrome_name = env.get("CHROME_BINARY") or "chromium"
        chrome_record = _required_binary_record(CHROME_PLUGIN_DIR, chrome_name, env)

        chromium_cmd = [
            str(PUPPETEER_BINARY_HOOK),
            f"--name={chrome_record.get('name', 'chromium')}",
            f"--binproviders={chrome_record.get('binproviders', '*')}",
        ]
        chrome_overrides = chrome_record.get("overrides")
        if chrome_overrides:
            chromium_cmd.append(f"--overrides={json.dumps(chrome_overrides)}")

        result = subprocess.run(
            chromium_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Puppeteer chromium install failed: {result.stderr}")

        records = parse_jsonl_records(result.stdout)
        chromium_record = None
        for record in records:
            if record.get("type") == "Binary" and record.get("name") == "chromium":
                chromium_record = record
                break
        if not chromium_record:
            for record in records:
                if record.get("type") == "Binary" and record.get("name") == "chrome":
                    chromium_record = record
                    break
        if not chromium_record:
            chromium_record = parse_jsonl_output(
                result.stdout,
                record_type="Binary",
            )
        if not chromium_record:
            raise RuntimeError("Chromium Binary record not found after install")

        chromium_path = chromium_record.get("abspath")
        if not isinstance(chromium_path, str) or not Path(chromium_path).exists():
            raise RuntimeError(
                f"Chromium binary not found after install: {chromium_path}",
            )

        apply_machine_updates(records, env)
        env["CHROME_BINARY"] = chromium_path

        resolved = _resolve_existing_chromium(env)
        if resolved:
            env["CHROME_BINARY"] = resolved
            return resolved
        return chromium_path


def run_hook_and_parse(
    hook_script: Path,
    url: str,
    snapshot_id: str,
    cwd: Path | None = None,
    env: dict | None = None,
    timeout: int = 60,
    extra_args: list[str] | None = None,
) -> tuple[int, dict[str, Any] | None, str]:
    """Run a hook and parse its JSONL output.

    Chrome-aware wrapper: defaults env to get_test_env().
    """
    if env is None:
        env = get_test_env()
    return _base_run_hook_and_parse(
        hook_script,
        url,
        snapshot_id,
        cwd=cwd,
        env=env,
        timeout=timeout,
        extra_args=extra_args,
    )


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
    - persona-scoped ``chrome_extensions``, ``chrome_downloads``, and
      ``chrome_user_data`` dirs are provisioned together
    - Chromium + npm dependencies are installed through hooks, not hand-written
      test setup

    Returns env dict with ``SNAP_DIR``, ``CRAWL_DIR``, ``PERSONAS_DIR``,
    ``LIB_DIR``, ``NODE_MODULES_DIR``, ``NODE_PATH``, ``CHROME_BINARY``, etc.

    Args:
        tmpdir: Base temporary directory for the test

    Returns:
        Environment dict with all paths set.
    """

    # Determine machine type (matches archivebox.config.paths.get_machine_type())
    machine = platform.machine().lower()
    system = platform.system().lower()
    if machine in ("arm64", "aarch64"):
        machine = "arm64"
    elif machine in ("x86_64", "amd64"):
        machine = "x86_64"
    machine_type = f"{machine}-{system}"

    tmpdir = Path(tmpdir).resolve()

    # Keep crawl/snap state rooted in the caller's tmpdir so every test is isolated.
    snap_dir = tmpdir / "snap"
    lib_dir = get_lib_dir()
    npm_dir = lib_dir / "npm"
    npm_bin_dir = npm_dir / ".bin"
    node_modules_dir = npm_dir / "node_modules"

    personas_dir = tmpdir / "personas"
    home_dir = tmpdir / "home"
    xdg_config_home = home_dir / ".config"
    xdg_cache_home = home_dir / ".cache"
    xdg_data_home = home_dir / ".local" / "share"
    chrome_extensions_dir = personas_dir / "Default" / "chrome_extensions"
    chrome_downloads_dir = personas_dir / "Default" / "chrome_downloads"
    chrome_user_data_dir = personas_dir / "Default" / "chrome_user_data"

    # Create all directories
    node_modules_dir.mkdir(parents=True, exist_ok=True)
    npm_bin_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    xdg_config_home.mkdir(parents=True, exist_ok=True)
    xdg_cache_home.mkdir(parents=True, exist_ok=True)
    xdg_data_home.mkdir(parents=True, exist_ok=True)
    chrome_extensions_dir.mkdir(parents=True, exist_ok=True)
    chrome_downloads_dir.mkdir(parents=True, exist_ok=True)
    chrome_user_data_dir.mkdir(parents=True, exist_ok=True)
    snap_dir.mkdir(parents=True, exist_ok=True)
    crawl_dir = tmpdir / "crawl"
    crawl_dir.mkdir(parents=True, exist_ok=True)

    # Build complete env dict
    env = os.environ.copy()
    env.update(
        {
            "SNAP_DIR": str(snap_dir),
            "CRAWL_DIR": str(crawl_dir),
            "PERSONAS_DIR": str(personas_dir),
            "LIB_DIR": str(lib_dir),
            "MACHINE_TYPE": machine_type,
            "NPM_BIN_DIR": str(npm_bin_dir),
            "NODE_MODULES_DIR": str(node_modules_dir),
            "HOME": str(home_dir),
            "XDG_CONFIG_HOME": str(xdg_config_home),
            "XDG_CACHE_HOME": str(xdg_cache_home),
            "XDG_DATA_HOME": str(xdg_data_home),
            "CHROME_EXTENSIONS_DIR": str(chrome_extensions_dir),
            "CHROME_DOWNLOADS_DIR": str(chrome_downloads_dir),
            "CHROME_USER_DATA_DIR": str(chrome_user_data_dir),
        },
    )

    # Only set headless if not already in environment (allow override for debugging)
    if "CHROME_HEADLESS" not in os.environ:
        env["CHROME_HEADLESS"] = "true"

    assert_isolated_snapshot_env(env)

    try:
        install_chromium_with_hooks(env)
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

    This waits for the crawl hook to publish ``cdp_url.txt`` in the crawl's
    ``chrome`` dir, not just for a child process to exist. That keeps tests on
    the same readiness contract as production snapshot hooks.

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
        if cdp_file.exists():
            cdp_url = cdp_file.read_text().strip()
            if cdp_url:
                break
        process_status = chrome_launch_process.poll()
        if process_status is not None:
            stdout_handle.flush()
            stderr_handle.flush()
            if cdp_file.exists():
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
        and _is_pid_alive(chrome_pid)
        and not _wait_for_pid_exit(chrome_pid)
    ):
        kill_chrome(chrome_pid, str(chrome_dir))

    for attr in ("_stdout_handle", "_stderr_handle"):
        handle = getattr(chrome_launch_process, attr, None)
        if handle:
            handle.close()


@contextmanager
def chromium_session(env: dict, chrome_dir: Path, crawl_id: str):
    """Context manager for Chromium sessions with automatic cleanup.

    Usage:
        with chromium_session(env, chrome_dir, 'test-crawl') as (process, cdp_url):
            # Use cdp_url to connect with puppeteer
            pass
        # Chromium automatically cleaned up

    Args:
        env: Environment dict (from setup_test_env)
        chrome_dir: Directory for Chrome files
        crawl_id: ID for the crawl

    Yields:
        Tuple of (chrome_launch_process, cdp_url)
    """
    chrome_launch_process = None
    try:
        chrome_launch_process, cdp_url = launch_chromium_session(
            env,
            chrome_dir,
            crawl_id,
        )
        yield chrome_launch_process, cdp_url
    finally:
        if chrome_launch_process:
            kill_chromium_session(chrome_launch_process, chrome_dir)


# =============================================================================
# Tab-based Test Helpers
# Used by tab-based tests (infiniscroll, modalcloser)
# =============================================================================


def cleanup_chrome(
    chrome_launch_process: subprocess.Popen,
    chrome_pid: int,
    chrome_dir: Path | None = None,
) -> None:
    """Clean up Chrome processes using chrome_utils.js killChrome.

    Uses the centralized kill logic from chrome_utils.js which handles:
    - SIGTERM then SIGKILL
    - Process group killing
    - Zombie process cleanup

    Args:
        chrome_launch_process: The Popen object for the chrome launch hook
        chrome_pid: The PID of the Chrome process
        chrome_dir: Optional path to chrome output directory
    """
    # First try to terminate the launch process gracefully
    try:
        chrome_launch_process.send_signal(signal.SIGTERM)
        chrome_launch_process.wait(timeout=5)
    except Exception:
        pass

    if _is_pid_alive(chrome_pid) and not _wait_for_pid_exit(chrome_pid):
        kill_chrome(chrome_pid, str(chrome_dir) if chrome_dir else None)


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
        startup_timeout = max(int(timeout), 45)

        # Create proper directory structure in tmpdir
        machine = platform.machine().lower()
        system = platform.system().lower()
        if machine in ("arm64", "aarch64"):
            machine = "arm64"
        elif machine in ("x86_64", "amd64"):
            machine = "x86_64"
        machine_type = f"{machine}-{system}"

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
        if env_overrides:
            env.update(env_overrides)

        # Prefer an already-provisioned NODE_MODULES_DIR (set by session-level chrome fixture)
        # so we don't force per-test reinstall under tmp LIB_DIR paths.
        existing_node_modules = env.get("NODE_MODULES_DIR")
        if existing_node_modules and Path(existing_node_modules).exists():
            node_modules_dir = Path(existing_node_modules).resolve()
            npm_dir = node_modules_dir.parent
            lib_dir = npm_dir.parent
        else:
            lib_dir = get_lib_dir()
            npm_dir = lib_dir / "npm"
            node_modules_dir = npm_dir / "node_modules"
        puppeteer_cache_dir = lib_dir / "puppeteer" / "chrome"

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
                "LIB_DIR": str(lib_dir),
                "MACHINE_TYPE": machine_type,
                "NODE_MODULES_DIR": str(node_modules_dir),
                "NODE_PATH": str(node_modules_dir),
                "NPM_BIN_DIR": str(npm_dir / ".bin"),
                "HOME": str(home_dir),
                "XDG_CONFIG_HOME": str(xdg_config_home),
                "XDG_CACHE_HOME": str(xdg_cache_home),
                "XDG_DATA_HOME": str(xdg_data_home),
                "CHROME_HEADLESS": "true",
                "PUPPETEER_CACHE_DIR": str(puppeteer_cache_dir),
            },
        )
        env.setdefault("CHROME_DEBUG_PORT_TIMEOUT_MS", str(startup_timeout * 1000))

        # Reuse system Puppeteer cache to avoid redundant Chromium downloads
        link_puppeteer_cache(lib_dir)

        # Reuse already-provisioned Chromium when available (session fixture sets CHROME_BINARY).
        # Falling back to hook-based install on each test is slow and can hang on flaky networks.
        chrome_binary = env.get("CHROME_BINARY")
        if not chrome_binary or not Path(chrome_binary).exists():
            chrome_binary = install_chromium_with_hooks(env)
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
                timeout=60,
            )
        except RuntimeError:
            cleanup_chrome(chrome_launch_process, chrome_pid)
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
                    cleanup_chrome(
                        chrome_launch_process,
                        chrome_pid,
                        chrome_dir=chrome_dir,
                    )
                    raise RuntimeError(f"Navigation failed: {result.stderr}")
            except subprocess.TimeoutExpired:
                cleanup_chrome(chrome_launch_process, chrome_pid, chrome_dir=chrome_dir)
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
        if chrome_launch_process and chrome_pid:
            cleanup_chrome(chrome_launch_process, chrome_pid, chrome_dir=chrome_dir)

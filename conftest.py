from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest_plugins = ["abx_plugins.plugins.chrome.tests.chrome_test_helpers"]


# ---------------------------------------------------------------------------
# Well-known Chromium binary locations (checked in order)
# ---------------------------------------------------------------------------
_CHROMIUM_SEARCH_PATHS = [
    # ABX lib install directory
    "{lib_dir}/chrome-linux/chrome",
    "{lib_dir}/browsers/chrome/chrome",
    # Puppeteer cache (root)
    os.path.expanduser("~/.cache/puppeteer/chrome/linux-*/chrome-linux64/chrome"),
    os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
    # System locations
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


def _find_chromium_binary(lib_dir: str = "") -> str | None:
    """Search well-known locations for a Chromium binary.

    Returns the absolute path or None.
    """
    import glob

    for pattern in _CHROMIUM_SEARCH_PATHS:
        expanded = pattern.format(lib_dir=lib_dir) if "{lib_dir}" in pattern else pattern
        for candidate in glob.glob(expanded):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    # Fallback: shutil.which
    for name in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


@pytest.fixture(autouse=True)
def isolated_test_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """Apply per-test env overrides and let monkeypatch restore global state after each test."""
    test_root = tmp_path / "abx_plugins_env"
    home_dir = test_root / "home"
    run_dir = test_root / "run"
    lib_dir = test_root / "lib"
    personas_dir = test_root / "personas"

    for directory in (home_dir, run_dir, lib_dir, personas_dir):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home_dir))
    # Mirror abx-dl runtime semantics: both resolve to the current run directory.
    monkeypatch.setenv("CRAWL_DIR", str(run_dir))
    monkeypatch.setenv("SNAP_DIR", str(run_dir))

    # Respect explicit env overrides from the calling shell/CI, otherwise root under test tmp state.
    if "LIB_DIR" not in os.environ:
        monkeypatch.setenv("LIB_DIR", str(lib_dir))
    if "PERSONAS_DIR" not in os.environ:
        monkeypatch.setenv("PERSONAS_DIR", str(personas_dir))
    if "TWOCAPTCHA_API_KEY" not in os.environ and "API_KEY_2CAPTCHA" not in os.environ:
        print("WARNING: TWOCAPTCHA_API_KEY not found in env, 2captcha tests will fail")

    return {
        "root": test_root,
        "home": home_dir,
        "crawl": run_dir,
        "snap": run_dir,
        "lib": Path(os.environ["LIB_DIR"]),
        "personas": Path(os.environ["PERSONAS_DIR"]),
    }


@pytest.fixture
def local_http_base_url(httpserver) -> str:
    """Stable local URL entrypoint for tests that need deterministic in-process HTTP endpoints."""
    return httpserver.url_for("/")


@pytest.fixture(scope="session")
def ensure_chrome_test_prereqs(ensure_chromium_and_puppeteer_installed):
    """Install shared Chromium/Puppeteer deps when explicitly requested by tests."""
    return ensure_chromium_and_puppeteer_installed


@pytest.fixture(scope="session")
def ensure_chromium_and_puppeteer_installed(tmp_path_factory):
    """Install Chromium and Puppeteer once for test sessions that require Chrome.

    Overrides the default from chrome_test_helpers to handle environments where
    the npm binary owner UID has no passwd entry (e.g. containers).  Falls back
    to scanning well-known paths for a pre-installed Chromium when the hook-based
    install fails.
    """
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
        get_test_env,
        _has_puppeteer_module,
    )

    if not os.environ.get("SNAP_DIR"):
        os.environ["SNAP_DIR"] = str(tmp_path_factory.mktemp("chrome_test_data"))
    if not os.environ.get("PERSONAS_DIR"):
        os.environ["PERSONAS_DIR"] = str(
            tmp_path_factory.mktemp("chrome_test_personas")
        )

    env = get_test_env()

    # Disable Chrome sandbox when running as root (common in containers/CI)
    if os.geteuid() == 0:
        os.environ.setdefault("CHROME_SANDBOX", "false")
        env.setdefault("CHROME_SANDBOX", "false")

    # --- Chromium binary ---
    # Try CHROME_BINARY from env first, then scan well-known locations
    chromium_binary = env.get("CHROME_BINARY", "")
    if not chromium_binary or not Path(chromium_binary).exists():
        chromium_binary = _find_chromium_binary(lib_dir=env.get("LIB_DIR", ""))

    if not chromium_binary:
        # Last resort: try the hook-based install (may fail on UID issues)
        from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
            install_chromium_with_hooks,
        )
        chromium_binary = install_chromium_with_hooks(env)

    if not chromium_binary:
        raise RuntimeError(
            "Chromium not found.  Set CHROME_BINARY or install Chromium."
        )

    env["CHROME_BINARY"] = chromium_binary
    os.environ["CHROME_BINARY"] = chromium_binary

    # --- Puppeteer module ---
    if not _has_puppeteer_module(env):
        # Try hook-based install
        from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
            install_chromium_with_hooks,
        )
        install_chromium_with_hooks(env)

    if not _has_puppeteer_module(env):
        raise RuntimeError(
            "puppeteer module not found after install.  "
            f"NODE_MODULES_DIR={env.get('NODE_MODULES_DIR')}"
        )

    # Propagate env vars for the rest of the session
    for key in ("NODE_MODULES_DIR", "NODE_PATH", "PATH", "CHROME_BINARY"):
        if env.get(key):
            os.environ[key] = env[key]

    return chromium_binary


@pytest.fixture(scope="session")
def ensure_claude_code_prereqs():
    """Ensure Claude Code CLI is installed and ANTHROPIC_API_KEY is set.

    Used by Claude Code integration tests.  Fails immediately with a clear
    message when prerequisites are missing.
    """
    # Check claude binary
    claude_bin = shutil.which("claude")
    if not claude_bin:
        pytest.fail(
            "Claude Code CLI ('claude') not found in PATH.  "
            "Install with: npm install -g @anthropic-ai/claude-code"
        )

    # Check API key
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        pytest.fail(
            "ANTHROPIC_API_KEY not set.  Claude Code integration tests "
            "require a valid API key."
        )

    # Quick smoke test: claude --version
    result = subprocess.run(
        [claude_bin, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.fail(f"'claude --version' failed (rc={result.returncode}): {result.stderr}")

    return claude_bin


@pytest.fixture(scope="module")
def require_chrome_runtime():
    """Require chrome runtime prerequisites for integration tests.

    Validates that node and npm resolve through abx-pkg before running
    Chrome-based integration tests like dns, dom, and headers. Previously
    duplicated in dns/dom/headers conftest files.
    """
    from abx_pkg import Binary, EnvProvider

    try:
        Binary(name="node", binproviders=[EnvProvider()]).load()
        Binary(name="npm", binproviders=[EnvProvider()]).load()
    except Exception as exc:
        pytest.fail(f"Chrome integration prerequisites unavailable: {exc}")

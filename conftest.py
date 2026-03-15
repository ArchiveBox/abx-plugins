from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest_plugins = ["abx_plugins.plugins.chrome.tests.chrome_test_helpers"]


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

    # Resolve LIB_DIR BEFORE monkeypatching HOME, so path helpers
    # (chrome_utils.js / Path.home()) see the real home directory.
    if "LIB_DIR" not in os.environ:
        from abx_plugins.plugins.chrome.tests.chrome_test_helpers import get_lib_dir

        resolved_lib = get_lib_dir()

    monkeypatch.setenv("HOME", str(home_dir))
    # Mirror abx-dl runtime semantics: both resolve to the current run directory.
    monkeypatch.setenv("CRAWL_DIR", str(run_dir))
    monkeypatch.setenv("SNAP_DIR", str(run_dir))

    if "LIB_DIR" not in os.environ:
        monkeypatch.setenv("LIB_DIR", str(resolved_lib))
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
    """Install Chromium and Puppeteer once via hook-based install.

    Overrides the default from chrome_test_helpers only to auto-disable
    the Chrome sandbox when running as root (common in containers/CI).
    """
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
        get_test_env,
        install_chromium_with_hooks,
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

    chromium_binary = install_chromium_with_hooks(env)
    if not chromium_binary:
        raise RuntimeError("Chromium not found after hook-based install")

    # Only stash CHROME_BINARY so _resolve_existing_chromium() can skip
    # re-download.  Do NOT propagate NODE_MODULES_DIR / NODE_PATH / PATH —
    # chrome_session() calls get_test_env() itself and must not depend on
    # session fixture execution order.
    os.environ["CHROME_BINARY"] = chromium_binary

    return chromium_binary


@pytest.fixture(scope="session")
def ensure_claude_code_prereqs():
    """Ensure Claude Code CLI is installed and ANTHROPIC_API_KEY is set.

    Used by Claude Code integration tests.  Fails immediately with a clear
    message when prerequisites are missing.
    """
    # Check claude binary (honor CLAUDECODE_BINARY env var)
    claude_bin = os.environ.get("CLAUDECODE_BINARY") or shutil.which("claude")
    if not claude_bin:
        pytest.fail(
            "Claude Code CLI not found in PATH and CLAUDECODE_BINARY not set.  "
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


@pytest.fixture(scope="session")
def ensure_anthropic_api_key():
    """Ensure ANTHROPIC_API_KEY is set.

    Used by plugins that call the Anthropic API directly (e.g. claudechrome).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        pytest.fail(
            "ANTHROPIC_API_KEY not set.  Integration tests that call the "
            "Anthropic API require a valid API key."
        )
    return api_key


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

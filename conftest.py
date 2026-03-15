from __future__ import annotations

import os
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


@pytest.fixture(scope="session", autouse=True)
def _auto_detect_chromium():
    """Auto-detect Chromium binary once per session.

    Sets CHROME_BINARY env var if a Playwright/Puppeteer-installed Chromium
    is found, so session-scoped install fixtures can skip downloading.
    Also sets CHROME_ARGS for root environments.
    """
    if "CHROME_BINARY" not in os.environ:
        for candidate in (
            Path("/root/.cache/ms-playwright/chromium-1194/chrome-linux/chrome"),
            Path.home() / ".cache" / "ms-playwright" / "chromium-1194" / "chrome-linux" / "chrome",
        ):
            if candidate.is_file():
                os.environ["CHROME_BINARY"] = str(candidate)
                break

    if os.geteuid() == 0 and "CHROME_ARGS" not in os.environ:
        os.environ["CHROME_ARGS"] = '["--no-sandbox"]'


@pytest.fixture(scope="session")
def ensure_chrome_test_prereqs(ensure_chromium_and_puppeteer_installed):
    """Install shared Chromium/Puppeteer deps when explicitly requested by tests."""
    return ensure_chromium_and_puppeteer_installed


@pytest.fixture(scope="module")
def require_chrome_runtime():
    """Require chrome runtime prerequisites for integration tests.

    Validates that NpmProvider can actually locate npm and node binaries
    (needed by Chrome-based plugins like dns, dom, headers).
    Previously duplicated in dns/dom/headers conftest files.
    """
    from abx_pkg import NpmProvider

    try:
        provider = NpmProvider()
    except Exception as exc:
        pytest.fail(f"Chrome integration prerequisites unavailable: {exc}")

    if not provider.INSTALLER_BIN:
        pytest.fail(
            "npm not found on PATH. Chrome-based plugins require npm to install "
            "node dependencies (puppeteer, etc.)."
        )

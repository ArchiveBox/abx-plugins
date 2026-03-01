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


@pytest.fixture(scope="session")
def ensure_chrome_test_prereqs(ensure_chromium_and_puppeteer_installed):
    """Install shared Chromium/Puppeteer deps when explicitly requested by tests."""
    return ensure_chromium_and_puppeteer_installed

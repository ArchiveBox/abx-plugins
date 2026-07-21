from __future__ import annotations

import fcntl
import logging
import os
import shlex
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import (
    assert_isolated_snapshot_env,
)

pytest_plugins = ["abx_plugins.plugins.chrome.tests.chrome_test_helpers"]

logger = logging.getLogger(__name__)


REPO_ROOT = Path(__file__).resolve().parent
PLUGINS_ROOT = REPO_ROOT / "abx_plugins" / "plugins"
CLAUDECODE_CONFIG = PLUGINS_ROOT / "claudecode" / "config.json"

existing_pythonpath = os.environ.get("PYTHONPATH", "")
pythonpath_entries = [str(REPO_ROOT)]
if existing_pythonpath:
    pythonpath_entries.append(existing_pythonpath)
os.environ["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)


def _tee_subprocess_output_enabled() -> bool:
    return os.environ.get("ABX_PYTEST_TEE_SUBPROCESS_OUTPUT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _format_subprocess_args(args: object) -> str:
    if isinstance(args, (list, tuple)):
        return shlex.join(str(arg) for arg in args)
    return str(args)


def _normalize_subprocess_stream(stream: object) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return str(stream)


def _format_subprocess_output(args: object, stdout: object, stderr: object) -> str:
    cmd_display = _format_subprocess_args(args)
    stdout_text = _normalize_subprocess_stream(stdout)
    stderr_text = _normalize_subprocess_stream(stderr)
    chunks: list[str] = []

    if stdout_text:
        chunk = f"\n[subprocess stdout] {cmd_display}\n{stdout_text}"
        if not stdout_text.endswith("\n"):
            chunk += "\n"
        chunks.append(chunk)

    if stderr_text:
        chunk = f"\n[subprocess stderr] {cmd_display}\n{stderr_text}"
        if not stderr_text.endswith("\n"):
            chunk += "\n"
        chunks.append(chunk)

    return "".join(chunks)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


@pytest.fixture(autouse=True)
def tee_captured_subprocess_output_on_failure(
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    # Pytest only auto-shows output it captured itself. Many tests in this repo
    # call subprocess.run(..., capture_output=True), which hides child-process
    # stdout/stderr from pytest entirely unless the test manually includes it in
    # an assertion message. In CI, buffer that captured subprocess output and
    # dump it only when the owning test fails.
    if not _tee_subprocess_output_enabled():
        yield
        return

    monkeypatch = pytest.MonkeyPatch()
    real_run = subprocess.run
    subprocess_output_log: list[str] = []

    def wrapped_run(*args, **kwargs):
        result = real_run(*args, **kwargs)
        cmd_args = kwargs.get("args")
        if cmd_args is None and args:
            cmd_args = args[0]
        formatted = _format_subprocess_output(cmd_args, result.stdout, result.stderr)
        if formatted:
            subprocess_output_log.append(formatted)
        return result

    monkeypatch.setattr(subprocess, "run", wrapped_run)
    try:
        yield
    finally:
        monkeypatch.undo()
        rep_setup = getattr(request.node, "rep_setup", None)
        rep_call = getattr(request.node, "rep_call", None)
        rep_teardown = getattr(request.node, "rep_teardown", None)
        # Match pytest's default ergonomics: keep passing tests quiet, but emit
        # the buffered child-process output for failures in setup/call/teardown.
        failed = any(
            report is not None and report.failed
            for report in (rep_setup, rep_call, rep_teardown)
        )
        if failed and subprocess_output_log:
            sys.stdout.write("".join(subprocess_output_log))
            sys.stdout.flush()


@pytest.fixture(autouse=True)
def isolated_test_env(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    """Apply per-test env overrides and let monkeypatch restore global state after each test."""
    # Keep runtime HOME/cache state outside any test-owned snapshot tmp_path so
    # hook subprocesses cannot pollute SNAP_DIR with uv/pnpm/browser artifacts.
    test_root = tmp_path_factory.mktemp("abx_plugins_env")
    home_dir = test_root / "home"
    run_dir = test_root / "run"
    lib_dir = test_root / "lib"
    personas_dir = test_root / "personas"

    for directory in (home_dir, run_dir, lib_dir, personas_dir):
        directory.mkdir(parents=True, exist_ok=True)

    resolved_lib = (
        Path(os.environ["ABXPKG_LIB_DIR"])
        if "ABXPKG_LIB_DIR" in os.environ
        else lib_dir
    )
    resolved_uv_cache = Path(
        os.environ.get(
            "UV_CACHE_DIR",
            str(
                Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
                / "uv",
            ),
        ),
    )
    resolved_uv_cache.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home_dir))
    # Isolated plugin tests use one run directory for crawl and snapshot state.
    monkeypatch.setenv("CRAWL_DIR", str(run_dir))
    monkeypatch.setenv("SNAP_DIR", str(run_dir))
    monkeypatch.setenv("UV_CACHE_DIR", str(resolved_uv_cache))

    if "ABXPKG_LIB_DIR" not in os.environ:
        monkeypatch.setenv("ABXPKG_LIB_DIR", str(resolved_lib))
    if "PERSONAS_DIR" not in os.environ:
        monkeypatch.setenv("PERSONAS_DIR", str(personas_dir))
    if "TWOCAPTCHA_API_KEY" not in os.environ and "API_KEY_2CAPTCHA" not in os.environ:
        print("WARNING: TWOCAPTCHA_API_KEY not found in env, 2captcha tests will fail")

    assert_isolated_snapshot_env(
        {
            "HOME": str(home_dir),
            "SNAP_DIR": str(run_dir),
            "ABXPKG_LIB_DIR": os.environ["ABXPKG_LIB_DIR"],
            "PERSONAS_DIR": os.environ["PERSONAS_DIR"],
        },
    )

    return {
        "root": test_root,
        "home": home_dir,
        "crawl": run_dir,
        "snap": run_dir,
        "lib": Path(os.environ["ABXPKG_LIB_DIR"]),
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


def ensure_chromium_and_puppeteer_installed_impl(tmp_path_factory) -> str:
    """Install Chromium and Puppeteer once via abxpkg.

    Overrides the default from chrome_test_helpers only to auto-disable
    the Chrome sandbox when running as root (common in containers/CI).
    """
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
        get_test_env,
        install_chromium_with_abxpkg,
    )

    if not os.environ.get("SNAP_DIR"):
        os.environ["SNAP_DIR"] = str(tmp_path_factory.mktemp("chrome_test_data"))
    if not os.environ.get("PERSONAS_DIR"):
        os.environ["PERSONAS_DIR"] = str(
            tmp_path_factory.mktemp("chrome_test_personas"),
        )
    os.environ.setdefault(
        "ABXPKG_LIB_DIR",
        str(tmp_path_factory.mktemp("chrome_test_lib")),
    )

    env = get_test_env(install_required_binaries=True)

    # Disable Chrome sandbox when running as root (common in containers/CI)
    if os.geteuid() == 0:
        os.environ.setdefault("CHROME_SANDBOX", "false")
        env.setdefault("CHROME_SANDBOX", "false")

    chromium_binary = install_chromium_with_abxpkg(env)
    if not chromium_binary:
        raise RuntimeError("Chromium not found after abxpkg install")

    os.environ["CHROME_BINARY"] = chromium_binary
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

    return chromium_binary


ensure_chromium_and_puppeteer_installed = pytest.fixture(scope="session")(
    ensure_chromium_and_puppeteer_installed_impl,
)


@pytest.fixture(scope="session")
def installed_claude_code_prereqs(tmp_path_factory):
    """Install Claude Code once without changing the pytest process environment."""
    from abxpkg import BinProvider
    from abx_plugins.plugins.base.utils import load_required_binary_from_config

    env = os.environ.copy()
    env["ABXPKG_LIB_DIR"] = str(tmp_path_factory.mktemp("claudecode_test_lib"))
    env["CRAWL_DIR"] = str(tmp_path_factory.mktemp("claudecode_test_data"))
    env["CLAUDECODE_ENABLED"] = "true"

    lib_dir = Path(env["ABXPKG_LIB_DIR"])
    lib_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lib_dir / ".claudecode_install.lock"

    try:
        with lock_path.open("w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

            binary_name = env.get("CLAUDECODE_BINARY", "claude")
            loaded = load_required_binary_from_config(
                binary_name,
                CLAUDECODE_CONFIG,
                global_config=env,
                environ=env,
                install=True,
            )
    except Exception as exc:
        raise AssertionError(f"Claude Code CLI install via abxpkg failed: {exc}")

    claude_bin = str(loaded.loaded_abspath or "")
    if not claude_bin or not Path(claude_bin).exists():
        raise AssertionError(
            f"Claude Code binary not found after abxpkg install: {claude_bin}",
        )

    provider = loaded.loaded_binprovider
    exec_env = (
        BinProvider.build_exec_env(providers=[provider], base_env=env)
        if provider is not None
        else env
    )
    exec_env["CLAUDECODE_BINARY"] = claude_bin

    # Check auth. Claude Code accepts both API-key auth and the OAuth token used
    # by the official action; plugin tests should exercise either real path.
    api_key = exec_env.get("ANTHROPIC_API_KEY", "")
    oauth_token = exec_env.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not api_key and not oauth_token:
        raise AssertionError(
            "ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN not set. Claude Code "
            "integration tests require real Claude Code auth.",
        )

    # Smoke-test the exact environment that dependent tests will receive.
    result = subprocess.run(
        [claude_bin, "--version"],
        capture_output=True,
        text=True,
        env=exec_env,
        timeout=10,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"'claude --version' failed (rc={result.returncode}): {result.stderr}",
        )

    return claude_bin, exec_env


@pytest.fixture
def ensure_claude_code_prereqs(installed_claude_code_prereqs, monkeypatch):
    """Project Claude's execution environment only into each dependent test."""
    claude_bin, exec_env = installed_claude_code_prereqs
    for key, value in exec_env.items():
        monkeypatch.setenv(key, value)

    return claude_bin


@pytest.fixture(scope="session")
def ensure_anthropic_api_key():
    """Ensure ANTHROPIC_API_KEY is set.

    Used by plugins that call the Anthropic API directly (e.g. claudechrome).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise AssertionError(
            "ANTHROPIC_API_KEY not set.  Integration tests that call the "
            "Anthropic API require a valid API key.",
        )
    return api_key

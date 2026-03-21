from __future__ import annotations

import fcntl
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import (
    assert_isolated_snapshot_env,
    parse_jsonl_output,
    parse_jsonl_records,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import get_lib_dir

pytest_plugins = ["abx_plugins.plugins.chrome.tests.chrome_test_helpers"]

logger = logging.getLogger(__name__)


PLUGINS_ROOT = Path(__file__).resolve().parent / "abx_plugins" / "plugins"
CLAUDECODE_INSTALL_HOOK = (
    PLUGINS_ROOT / "claudecode" / "on_Crawl__35_claudecode_install.finite.bg.py"
)
NPM_BINARY_HOOK = PLUGINS_ROOT / "npm" / "on_Binary__10_npm_install.py"


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
    # hook subprocesses cannot pollute SNAP_DIR with uv/npm/browser artifacts.
    test_root = tmp_path_factory.mktemp("abx_plugins_env")
    home_dir = test_root / "home"
    run_dir = test_root / "run"
    lib_dir = test_root / "lib"
    personas_dir = test_root / "personas"

    for directory in (home_dir, run_dir, lib_dir, personas_dir):
        directory.mkdir(parents=True, exist_ok=True)

    # Resolve LIB_DIR BEFORE monkeypatching HOME, so path helpers
    # (chrome_utils.js / Path.home()) see the real home directory.
    resolved_lib = (
        Path(os.environ["LIB_DIR"]) if "LIB_DIR" in os.environ else get_lib_dir()
    )

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

    assert_isolated_snapshot_env(
        {
            "HOME": str(home_dir),
            "SNAP_DIR": str(run_dir),
            "LIB_DIR": os.environ["LIB_DIR"],
            "PERSONAS_DIR": os.environ["PERSONAS_DIR"],
        },
    )

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


def ensure_chromium_and_puppeteer_installed_impl(tmp_path_factory) -> str:
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
            tmp_path_factory.mktemp("chrome_test_personas"),
        )

    env = get_test_env()

    # Disable Chrome sandbox when running as root (common in containers/CI)
    if os.geteuid() == 0:
        os.environ.setdefault("CHROME_SANDBOX", "false")
        env.setdefault("CHROME_SANDBOX", "false")

    chromium_binary = install_chromium_with_hooks(env)
    if not chromium_binary:
        raise RuntimeError("Chromium not found after hook-based install")

    # Default tests to the hook-installed Puppeteer Chrome, but keep any
    # explicit runtime CHROME_BINARY override authoritative.
    # Do NOT propagate NODE_MODULES_DIR / NODE_PATH / PATH — chrome_session()
    # calls get_test_env() itself and must not depend on session fixture
    # execution order.
    os.environ.setdefault("CHROME_BINARY", chromium_binary)

    return chromium_binary


ensure_chromium_and_puppeteer_installed = pytest.fixture(scope="session")(
    ensure_chromium_and_puppeteer_installed_impl,
)


@pytest.fixture(scope="session")
def ensure_claude_code_prereqs(tmp_path_factory):
    """Ensure Claude Code CLI is installed and ANTHROPIC_API_KEY is set.

    Used by Claude Code integration tests. Skips the dependent tests when
    live Anthropic credentials are unavailable.
    """

    def apply_machine_updates(records: list[dict], env: dict[str, str]) -> None:
        for record in records:
            if record.get("type") != "Machine":
                continue
            config = record.get("config")
            if isinstance(config, dict):
                env.update({str(key): str(value) for key, value in config.items()})

    def install_claude_code_with_hooks() -> str:
        env = os.environ.copy()
        env.setdefault("LIB_DIR", str(get_lib_dir()))
        env.setdefault(
            "CRAWL_DIR",
            str(tmp_path_factory.mktemp("claudecode_test_data")),
        )
        env["CLAUDECODE_ENABLED"] = "true"

        lib_dir = Path(env["LIB_DIR"])
        lib_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lib_dir / ".claudecode_install.lock"

        with lock_path.open("w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

            install_result = subprocess.run(
                [str(CLAUDECODE_INSTALL_HOOK)],
                capture_output=True,
                text=True,
                timeout=300,
                env=env,
            )
            if install_result.returncode != 0:
                raise RuntimeError(
                    f"Claude Code install hook failed: {install_result.stderr or install_result.stdout}",
                )

            binary_record = (
                parse_jsonl_output(install_result.stdout, record_type="Binary") or {}
            )
            if binary_record.get("name") != "claude":
                raise RuntimeError(
                    "Claude Code install hook did not emit a claude Binary record",
                )

            npm_cmd = [
                str(NPM_BINARY_HOOK),
                "--machine-id=test-machine",
                "--binary-id=test-claude",
                "--plugin-name=claudecode",
                "--hook-name=on_Crawl__35_claudecode_install.finite.bg",
                "--name=claude",
                f"--binproviders={binary_record.get('binproviders', '*')}",
            ]
            overrides = binary_record.get("overrides")
            if overrides:
                npm_cmd.append(f"--overrides={json.dumps(overrides)}")

            npm_result = subprocess.run(
                npm_cmd,
                capture_output=True,
                text=True,
                timeout=600,
                env=env,
            )
            if npm_result.returncode != 0:
                raise RuntimeError(
                    f"Claude Code npm install failed:\nstdout: {npm_result.stdout}\nstderr: {npm_result.stderr}",
                )

            records = parse_jsonl_records(npm_result.stdout)
            apply_machine_updates(records, env)

            claude_record = next(
                (
                    record
                    for record in records
                    if record.get("type") == "Binary" and record.get("name") == "claude"
                ),
                None,
            )
            if not claude_record:
                raise RuntimeError(
                    "Claude Code npm install did not emit a resolved claude Binary record",
                )

            claude_bin = claude_record.get("abspath")
            if not isinstance(claude_bin, str) or not Path(claude_bin).exists():
                raise RuntimeError(
                    f"Claude Code binary not found after install: {claude_bin}",
                )

            os.environ.update(env)
            os.environ["CLAUDECODE_BINARY"] = claude_bin
            return claude_bin

    # Check claude binary (honor CLAUDECODE_BINARY env var), otherwise install via hooks.
    claude_bin = os.environ.get("CLAUDECODE_BINARY")
    if not claude_bin or not Path(claude_bin).exists():
        claude_bin = shutil.which("claude")
    if not claude_bin:
        try:
            claude_bin = install_claude_code_with_hooks()
        except Exception as exc:
            pytest.fail(f"Claude Code CLI install via hooks failed: {exc}")

    # Check API key
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        pytest.fail(
            "ANTHROPIC_API_KEY not set.  Claude Code integration tests "
            "require a valid API key.",
        )

    # Quick smoke test: claude --version
    result = subprocess.run(
        [claude_bin, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.fail(
            f"'claude --version' failed (rc={result.returncode}): {result.stderr}",
        )

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
            "Anthropic API require a valid API key.",
        )
    return api_key


def require_chrome_runtime_impl() -> None:
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
        logger.error("Chrome integration prerequisites unavailable: %s", exc)
        pytest.fail(
            f"Chrome integration prerequisites unavailable: {exc}",
            pytrace=False,
        )


require_chrome_runtime = pytest.fixture(scope="module")(require_chrome_runtime_impl)

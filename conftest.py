from __future__ import annotations

import fcntl
import logging
import os
import shutil
import subprocess
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


@pytest.fixture
def real_staticfile_output(ensure_chrome_test_prereqs):
    """Run the shipped staticfile lifecycle and preserve its real hook log."""
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session
    from abx_plugins.plugins.staticfile.tests.test_staticfile import (
        run_staticfile_capture,
    )

    def run(root: Path, url: str, snapshot_id: str) -> Path:
        with chrome_session(
            root,
            crawl_id=f"crawl-{snapshot_id}",
            snapshot_id=snapshot_id,
            test_url=url,
            navigate=False,
            timeout=45,
        ) as (_process, _pid, chrome_dir, env):
            staticfile_dir = chrome_dir.parent / "staticfile"
            staticfile_dir.mkdir()
            result = run_staticfile_capture(
                staticfile_dir,
                chrome_dir,
                env,
                url,
                snapshot_id,
            )
            hook_code, stdout, stderr, navigate, archive_result = result[:5]
            assert hook_code == 0, stderr
            assert navigate.returncode == 0, navigate.stderr
            assert archive_result is not None, stdout
            (staticfile_dir / "stdout.log").write_text(stdout, encoding="utf-8")
            return chrome_dir.parent

    return run


@pytest.fixture(scope="session")
def real_html_snapshot(ensure_chrome_test_prereqs):
    """Capture title and DOM through their shipped hooks against a live page."""
    from abx_plugins.plugins.base.testing import get_hook_script, parse_jsonl_output
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

    def run(root: Path, url: str, snapshot_id: str) -> Path:
        with chrome_session(
            root,
            crawl_id=f"crawl-{snapshot_id}",
            snapshot_id=snapshot_id,
            test_url=url,
            navigate=True,
            timeout=45,
        ) as (_process, _pid, chrome_dir, env):
            snapshot_dir = chrome_dir.parent
            for plugin_name, pattern in (
                ("title", "on_Snapshot__*_title.*"),
                ("dom", "on_Snapshot__*_dom.*"),
            ):
                plugin_dir = PLUGINS_ROOT / plugin_name
                hook = get_hook_script(plugin_dir, pattern)
                assert hook is not None
                output_dir = snapshot_dir / plugin_name
                output_dir.mkdir()
                result = subprocess.run(
                    [str(hook), f"--url={url}", f"--snapshot-id={snapshot_id}"],
                    cwd=output_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert result.returncode == 0, result.stderr
                record = parse_jsonl_output(result.stdout)
                assert record is not None and record["status"] == "succeeded", (
                    result.stdout,
                    result.stderr,
                )
                (output_dir / "stdout.log").write_text(result.stdout, encoding="utf-8")
            return snapshot_dir

    return run


@pytest.fixture
def real_competing_html_snapshot(real_html_snapshot):
    """Produce real SingleFile and DOM outputs from distinct live pages."""
    from abx_plugins.plugins.base.testing import parse_jsonl_output
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session
    from abx_plugins.plugins.singlefile.tests.test_singlefile import (
        SNAPSHOT_HOOK,
        ensure_singlefile_extension_installed,
    )

    def run(root: Path, snapshot_id: str) -> Path:
        singlefile_root = root / "singlefile-capture"
        install_state = ensure_singlefile_extension_installed(root)
        with chrome_session(
            tmpdir=singlefile_root,
            crawl_id=f"singlefile-{snapshot_id}",
            snapshot_id=snapshot_id,
            test_url="https://archivebox.io",
            navigate=False,
            timeout=30,
            env_overrides={
                "ABXPKG_LIB_DIR": str(install_state["abxpkg_lib_dir"]),
            },
        ) as (_process, _pid, chrome_dir, env):
            snapshot_dir = chrome_dir.parent
            output_dir = snapshot_dir / "singlefile"
            output_dir.mkdir()
            env["SINGLEFILE_ENABLED"] = "true"
            result = subprocess.run(
                [str(SNAPSHOT_HOOK), "--url=https://archivebox.io"],
                cwd=output_dir,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            record = parse_jsonl_output(result.stdout)
            assert result.returncode == 0, result.stderr
            assert record is not None and record["status"] == "succeeded", record

        dom_snapshot = real_html_snapshot(
            root / "dom-capture",
            "https://example.com",
            f"dom-{snapshot_id}",
        )
        shutil.move(dom_snapshot / "dom", snapshot_dir / "dom")
        return snapshot_dir

    return run


@pytest.fixture(autouse=True)
def isolated_test_env(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Path]]:
    """Apply and restore per-test environment overrides."""
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
    resolved_personas = Path(os.environ.get("PERSONAS_DIR", str(personas_dir)))

    overrides = {
        "HOME": str(home_dir),
        "CRAWL_DIR": str(run_dir),
        "SNAP_DIR": str(run_dir),
        "UV_CACHE_DIR": str(resolved_uv_cache),
        "ABXPKG_LIB_DIR": str(resolved_lib),
        "PERSONAS_DIR": str(resolved_personas),
    }
    original = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
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

    try:
        yield {
            "root": test_root,
            "home": home_dir,
            "crawl": run_dir,
            "snap": run_dir,
            "lib": Path(os.environ["ABXPKG_LIB_DIR"]),
            "personas": Path(os.environ["PERSONAS_DIR"]),
        }
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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
def ensure_claude_code_prereqs(
    installed_claude_code_prereqs,
) -> Iterator[str]:
    """Project Claude's execution environment only into each dependent test."""
    claude_bin, exec_env = installed_claude_code_prereqs
    original = {key: os.environ.get(key) for key in exec_env}
    os.environ.update(exec_env)
    try:
        yield claude_bin
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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

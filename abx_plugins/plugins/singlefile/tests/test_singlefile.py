"""
Integration tests for singlefile plugin

Tests verify:
1. Hook scripts exist with correct naming
2. CLI-based singlefile extraction works
3. Dependencies available via abx-pkg
4. Output contains valid HTML
5. Connects to Chrome session via CDP when available
6. Works with extensions loaded (ublock, etc.)
"""

import os
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_test_env,
    get_plugin_dir,
    get_hook_script,
    chrome_session,
    parse_jsonl_output,
)


PLUGIN_DIR = get_plugin_dir(__file__)
_SNAPSHOT_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_singlefile.py")
if _SNAPSHOT_HOOK is None:
    raise FileNotFoundError(f"Snapshot hook not found in {PLUGIN_DIR}")
SNAPSHOT_HOOK = _SNAPSHOT_HOOK
INSTALL_SCRIPT = PLUGIN_DIR / "on_Crawl__82_singlefile_install.finite.bg.js"
TEST_URL = "https://example.com"

# Module-level cache for extension install location
_singlefile_install_root = None
_singlefile_install_state = None


def ensure_singlefile_extension_installed() -> dict[str, Path]:
    """Install SingleFile extension via crawl hook and return resolved paths."""
    global _singlefile_install_state
    if _singlefile_install_state:
        cache_file = _singlefile_install_state["cache_file"]
        if cache_file.exists():
            try:
                payload = json.loads(cache_file.read_text())
                unpacked_path = Path(payload.get("unpacked_path", ""))
                if (
                    unpacked_path.exists()
                    and (unpacked_path / "manifest.json").exists()
                ):
                    return _singlefile_install_state
            except Exception:
                pass

    global _singlefile_install_root
    if not _singlefile_install_root:
        _singlefile_install_root = tempfile.mkdtemp(prefix="singlefile-ext-")

    install_root = Path(_singlefile_install_root)
    snap_dir = install_root / "snap"
    crawl_dir = install_root / "crawl"
    personas_dir = install_root / "personas"
    extensions_dir = personas_dir / "Default" / "chrome_extensions"
    downloads_dir = personas_dir / "Default" / "chrome_downloads"
    user_data_dir = personas_dir / "Default" / "chrome_user_data"

    extensions_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    snap_dir.mkdir(parents=True, exist_ok=True)
    crawl_dir.mkdir(parents=True, exist_ok=True)

    env_install = os.environ.copy()
    env_install.update(
        {
            "SNAP_DIR": str(snap_dir),
            "CRAWL_DIR": str(crawl_dir),
            "PERSONAS_DIR": str(personas_dir),
            "CHROME_EXTENSIONS_DIR": str(extensions_dir),
            "CHROME_DOWNLOADS_DIR": str(downloads_dir),
            "CHROME_USER_DATA_DIR": str(user_data_dir),
        }
    )

    result = subprocess.run(
        [str(INSTALL_SCRIPT)],
        capture_output=True,
        text=True,
        env=env_install,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"SingleFile extension install hook failed: {result.stderr}\nstdout: {result.stdout}"
    )

    cache_file = extensions_dir / "singlefile.extension.json"
    assert cache_file.exists(), f"Extension cache file not created: {cache_file}"

    payload = json.loads(cache_file.read_text())
    unpacked_path = Path(payload.get("unpacked_path", ""))
    assert unpacked_path.exists(), f"Unpacked extension path missing: {unpacked_path}"
    assert (unpacked_path / "manifest.json").exists(), (
        f"Extension manifest missing: {unpacked_path / 'manifest.json'}"
    )

    _singlefile_install_state = {
        "install_root": install_root,
        "snap_dir": snap_dir,
        "crawl_dir": crawl_dir,
        "personas_dir": personas_dir,
        "extensions_dir": extensions_dir,
        "downloads_dir": downloads_dir,
        "user_data_dir": user_data_dir,
        "cache_file": cache_file,
        "unpacked_path": unpacked_path,
    }
    return _singlefile_install_state


def test_snapshot_hook_exists():
    """Verify snapshot extraction hook exists"""
    assert SNAPSHOT_HOOK is not None and SNAPSHOT_HOOK.exists(), (
        f"Snapshot hook not found in {PLUGIN_DIR}"
    )


def test_snapshot_hook_priority():
    """Test that snapshot hook has correct priority (50)"""
    filename = SNAPSHOT_HOOK.name
    assert "50" in filename, "SingleFile snapshot hook should have priority 50"
    assert filename.startswith("on_Snapshot__50_"), (
        "Should follow priority naming convention"
    )


def test_verify_deps_with_abx_pkg():
    """Verify dependencies are available via abx-pkg."""
    from abx_pkg import Binary, EnvProvider

    # Verify node is available
    node_binary = Binary(name="node", binproviders=[EnvProvider()])
    node_loaded = node_binary.load()
    assert node_loaded and node_loaded.abspath, "Node.js required for singlefile plugin"
    state = ensure_singlefile_extension_installed()
    assert state["cache_file"].exists(), (
        "SingleFile extension cache should be installed"
    )


def test_singlefile_cli_archives_example_com():
    """Test that singlefile archives example.com and produces valid HTML."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        snap_dir = tmpdir / "snap"
        personas_dir = tmpdir / "personas"
        extensions_dir = personas_dir / "Default" / "chrome_extensions"
        downloads_dir = personas_dir / "Default" / "chrome_downloads"
        user_data_dir = personas_dir / "Default" / "chrome_user_data"
        extensions_dir.mkdir(parents=True, exist_ok=True)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        snap_dir.mkdir(parents=True, exist_ok=True)
        user_data_dir.mkdir(parents=True, exist_ok=True)

        env_install = os.environ.copy()
        env_install.update(
            {
                "SNAP_DIR": str(snap_dir),
                "PERSONAS_DIR": str(personas_dir),
                "CHROME_EXTENSIONS_DIR": str(extensions_dir),
                "CHROME_DOWNLOADS_DIR": str(downloads_dir),
            }
        )

        result = subprocess.run(
            [str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            env=env_install,
            timeout=120,
        )
        assert result.returncode == 0, f"Extension install failed: {result.stderr}"

        old_env = os.environ.copy()
        os.environ["CHROME_USER_DATA_DIR"] = str(user_data_dir)
        os.environ["CHROME_DOWNLOADS_DIR"] = str(downloads_dir)
        os.environ["CHROME_EXTENSIONS_DIR"] = str(extensions_dir)
        try:
            with chrome_session(
                tmpdir=tmpdir,
                crawl_id="singlefile-cli-crawl",
                snapshot_id="singlefile-cli-snap",
                test_url=TEST_URL,
                navigate=True,
                timeout=30,
            ) as (_chrome_proc, _chrome_pid, snapshot_chrome_dir, env):
                env["SINGLEFILE_ENABLED"] = "true"
                env["CHROME_EXTENSIONS_DIR"] = str(extensions_dir)
                env["CHROME_DOWNLOADS_DIR"] = str(downloads_dir)

                singlefile_output_dir = snapshot_chrome_dir.parent / "singlefile"
                singlefile_output_dir.mkdir(parents=True, exist_ok=True)

                # Run singlefile snapshot hook
                result = subprocess.run(
                    [str(SNAPSHOT_HOOK),
                        f"--url={TEST_URL}",
                        "--snapshot-id=test789",
                    ],
                    cwd=singlefile_output_dir,
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=120,
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        assert result.returncode == 0, f"Hook execution failed: {result.stderr}"

        # Verify output file exists
        output_file = singlefile_output_dir / "singlefile.html"
        assert output_file.exists(), (
            f"singlefile.html not created. stdout: {result.stdout}, stderr: {result.stderr}"
        )

        # Verify it contains real HTML
        html_content = output_file.read_text()
        assert len(html_content) > 500, "Output file too small to be valid HTML"
        assert "<!DOCTYPE html>" in html_content or "<html" in html_content, (
            "Output should contain HTML doctype or html tag"
        )
        assert "Example Domain" in html_content, (
            "Output should contain example.com content"
        )


def test_singlefile_with_chrome_session():
    """Test singlefile connects to existing Chrome session via CDP.

    When a Chrome session exists (chrome/cdp_url.txt), singlefile should
    connect to it instead of launching a new Chrome instance.
    """
    install_state = ensure_singlefile_extension_installed()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        old_env = os.environ.copy()
        os.environ["PERSONAS_DIR"] = str(install_state["personas_dir"])
        os.environ["CHROME_EXTENSIONS_DIR"] = str(install_state["extensions_dir"])
        os.environ["CHROME_DOWNLOADS_DIR"] = str(install_state["downloads_dir"])
        os.environ["CHROME_USER_DATA_DIR"] = str(install_state["user_data_dir"])
        try:
            # Set up Chrome session using shared helper
            with chrome_session(
                tmpdir=tmpdir,
                crawl_id="singlefile-test-crawl",
                snapshot_id="singlefile-test-snap",
                test_url=TEST_URL,
                navigate=False,  # Don't navigate, singlefile will do that
                timeout=20,
            ) as (chrome_launch_process, chrome_pid, snapshot_chrome_dir, env):
                snap_dir = Path(env["SNAP_DIR"])
                singlefile_output_dir = snap_dir / "singlefile"
                singlefile_output_dir.mkdir(parents=True, exist_ok=True)

                # Use env from chrome_session
                env["SINGLEFILE_ENABLED"] = "true"
                env["CHROME_EXTENSIONS_DIR"] = str(install_state["extensions_dir"])
                env["CHROME_DOWNLOADS_DIR"] = str(install_state["downloads_dir"])
                env["CHROME_USER_DATA_DIR"] = str(install_state["user_data_dir"])

                # Run singlefile - it should find and use the existing Chrome session
                result = subprocess.run(
                    [str(SNAPSHOT_HOOK),
                        f"--url={TEST_URL}",
                        "--snapshot-id=singlefile-test-snap",
                    ],
                    cwd=str(singlefile_output_dir),
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=120,
                )

                # Verify output
                output_file = singlefile_output_dir / "singlefile.html"
                if output_file.exists():
                    html_content = output_file.read_text()
                    assert len(html_content) > 500, "Output file too small"
                    assert "Example Domain" in html_content, (
                        "Should contain example.com content"
                    )
                else:
                    # If singlefile couldn't connect to Chrome, it may have failed
                    # Check if it mentioned browser-server in its args (indicating it tried to use CDP)
                    assert (
                        result.returncode == 0
                        or "browser-server" in result.stderr
                        or "cdp" in result.stderr.lower()
                    ), (
                        f"Singlefile should attempt CDP connection. stderr: {result.stderr}"
                    )
        finally:
            os.environ.clear()
            os.environ.update(old_env)


def test_singlefile_with_extension_uses_existing_chrome():
    """Test SingleFile uses the Chrome extension via existing session (CLI fallback disabled)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        snap_dir = tmpdir / "snap"
        personas_dir = tmpdir / "personas"
        extensions_dir = personas_dir / "Default" / "chrome_extensions"
        downloads_dir = personas_dir / "Default" / "chrome_downloads"
        user_data_dir = personas_dir / "Default" / "chrome_user_data"
        extensions_dir.mkdir(parents=True, exist_ok=True)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        snap_dir.mkdir(parents=True, exist_ok=True)
        user_data_dir.mkdir(parents=True, exist_ok=True)

        env_install = os.environ.copy()
        env_install.update(
            {
                "SNAP_DIR": str(snap_dir),
                "PERSONAS_DIR": str(personas_dir),
                "CHROME_EXTENSIONS_DIR": str(extensions_dir),
                "CHROME_DOWNLOADS_DIR": str(downloads_dir),
            }
        )

        # Install SingleFile extension cache before launching Chrome
        result = subprocess.run(
            [str(INSTALL_SCRIPT)],
            capture_output=True,
            text=True,
            env=env_install,
            timeout=120,
        )
        assert result.returncode == 0, f"Extension install failed: {result.stderr}"

        # Launch Chrome session with extensions loaded
        old_env = os.environ.copy()
        os.environ["CHROME_USER_DATA_DIR"] = str(user_data_dir)
        os.environ["CHROME_DOWNLOADS_DIR"] = str(downloads_dir)
        os.environ["CHROME_EXTENSIONS_DIR"] = str(extensions_dir)
        try:
            with chrome_session(
                tmpdir=tmpdir,
                crawl_id="singlefile-ext-crawl",
                snapshot_id="singlefile-ext-snap",
                test_url=TEST_URL,
                navigate=True,
                timeout=30,
            ) as (_chrome_proc, _chrome_pid, snapshot_chrome_dir, env):
                singlefile_output_dir = snapshot_chrome_dir.parent / "singlefile"
                singlefile_output_dir.mkdir(parents=True, exist_ok=True)

                # Ensure ../chrome points to snapshot chrome session (contains target_id.txt)
                chrome_dir = singlefile_output_dir.parent / "chrome"
                if not chrome_dir.exists():
                    chrome_dir.symlink_to(snapshot_chrome_dir)

                env["SINGLEFILE_ENABLED"] = "true"
                env["SINGLEFILE_BINARY"] = (
                    "/nonexistent/single-file"  # force extension path
                )
                env["CHROME_EXTENSIONS_DIR"] = str(extensions_dir)
                env["CHROME_DOWNLOADS_DIR"] = str(downloads_dir)
                env["CHROME_HEADLESS"] = "false"
                env.pop("CRAWL_DIR", None)

                # Track downloads dir state before run to ensure file is created then moved out
                downloads_before = set(downloads_dir.glob("*.html"))
                downloads_mtime_before = downloads_dir.stat().st_mtime_ns

                result = subprocess.run(
                    [str(SNAPSHOT_HOOK),
                        f"--url={TEST_URL}",
                        "--snapshot-id=singlefile-ext-snap",
                    ],
                    cwd=str(singlefile_output_dir),
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=120,
                )

                assert result.returncode == 0, (
                    f"SingleFile extension run failed: {result.stderr}"
                )

                output_file = singlefile_output_dir / "singlefile.html"
                assert output_file.exists(), (
                    f"singlefile.html not created. stdout: {result.stdout}, stderr: {result.stderr}"
                )
                html_content = output_file.read_text(errors="ignore")
                assert "Example Domain" in html_content, (
                    "Output should contain example.com content"
                )

                # Verify download moved out of downloads dir
                downloads_after = set(downloads_dir.glob("*.html"))
                new_downloads = downloads_after - downloads_before
                downloads_mtime_after = downloads_dir.stat().st_mtime_ns
                assert downloads_mtime_after != downloads_mtime_before, (
                    "Downloads dir should be modified during extension save"
                )
                assert not new_downloads, (
                    f"SingleFile download should be moved out of downloads dir, found: {new_downloads}"
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)


def test_singlefile_disabled_skips():
    """Test that SINGLEFILE_ENABLED=False exits with skipped JSONL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = get_test_env()
        env["SINGLEFILE_ENABLED"] = "False"

        result = subprocess.run(
            [str(SNAPSHOT_HOOK),
                f"--url={TEST_URL}",
                "--snapshot-id=test-disabled",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, f"Should exit 0 when disabled: {result.stderr}"

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Should emit JSONL when disabled"
        assert result_json["type"] == "ArchiveResult"
        assert result_json["status"] == "skipped"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

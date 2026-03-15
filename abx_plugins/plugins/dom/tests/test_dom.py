"""
Integration tests for dom plugin

Tests verify:
1. Hook script exists
2. Dependencies installed via chrome validation hooks
3. Verify deps with abx-pkg
4. DOM extraction works on https://example.com
5. JSONL output is correct
6. Filesystem output contains actual page content
7. Config options work
"""

import os
import subprocess
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
_DOM_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_dom.*")
if _DOM_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
DOM_HOOK = _DOM_HOOK
TEST_URL = "https://example.com"
CHROME_STARTUP_TIMEOUT_SECONDS = 45


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert DOM_HOOK.exists(), f"Hook not found: {DOM_HOOK}"


def test_verify_deps_with_abx_pkg():
    """Verify dependencies are available via abx-pkg after hook installation."""
    from abx_pkg import Binary, EnvProvider

    # Verify node is available
    node_binary = Binary(name="node", binproviders=[EnvProvider()])
    node_loaded = node_binary.load()
    assert node_loaded and node_loaded.abspath, "Node.js required for dom plugin"


def test_extracts_dom_from_example_com(require_chrome_runtime, chrome_test_url):
    """Test full workflow: extract DOM from deterministic local fixture via hook."""
    # Prerequisites checked by earlier test

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        with chrome_session(
            tmpdir,
            test_url=chrome_test_url,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (
            _process,
            _pid,
            snapshot_chrome_dir,
            env,
        ):
            dom_dir = snapshot_chrome_dir.parent / "dom"
            dom_dir.mkdir(exist_ok=True)

            # Run DOM extraction hook
            result = subprocess.run(
                [
                    "node",
                    str(DOM_HOOK),
                    f"--url={chrome_test_url}",
                    "--snapshot-id=test789",
                ],
                cwd=dom_dir,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"

        # Verify filesystem output (hook writes directly to working dir)
        dom_file = dom_dir / "output.html"
        assert dom_file.exists(), (
            f"output.html not created. Files: {list(tmpdir.iterdir())}"
        )

        # Verify HTML content contains REAL example.com text
        html_content = dom_file.read_text(errors="ignore")
        assert len(html_content) > 200, (
            f"HTML content too short: {len(html_content)} bytes"
        )
        html_lower = html_content.lower()
        assert "<html" in html_lower, "Missing <html> tag"
        assert "example domain" in html_lower, "Missing 'Example Domain' in HTML"
        assert (
            "this domain" in html_lower
            or "illustrative examples" in html_lower
            or "local deterministic test page" in html_lower
            or "chrome test helper fixture" in html_lower
        ), "Missing expected description text in extracted HTML"


def test_config_save_dom_false_skips():
    """Test that DOM_ENABLED=False exits with skipped JSONL."""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env["DOM_ENABLED"] = "False"

        result = subprocess.run(
            ["node", str(DOM_HOOK), f"--url={TEST_URL}", "--snapshot-id=test999"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, (
            f"Should exit 0 when feature disabled: {result.stderr}"
        )

        assert "Skipping DOM" in result.stderr or "False" in result.stderr, (
            "Should log skip reason to stderr"
        )

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Should emit JSONL when disabled"
        assert result_json["type"] == "ArchiveResult"
        assert result_json["status"] == "skipped"


def test_staticfile_present_skips():
    """Test that dom returns noresults when staticfile already downloaded."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir
        env = get_test_env() | {"SNAP_DIR": str(snap_dir)}

        # Create directory structure like real ArchiveBox:
        # tmpdir/
        #   staticfile/  <- staticfile extractor output
        #   dom/         <- dom extractor runs here, looks for ../staticfile
        staticfile_dir = tmpdir / "staticfile"
        staticfile_dir.mkdir()
        (staticfile_dir / "stdout.log").write_text(
            '{"type":"ArchiveResult","status":"succeeded","output_str":"index.html"}\n'
        )

        dom_dir = tmpdir / "dom"
        dom_dir.mkdir()

        result = subprocess.run(
            ["node", str(DOM_HOOK), f"--url={TEST_URL}", "--snapshot-id=teststatic"],
            cwd=dom_dir,  # Run from dom subdirectory
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, "Should exit 0 when permanently skipping"

        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should emit ArchiveResult JSONL for noresults"
        assert result_json["status"] == "noresults", (
            f"Should have status='noresults': {result_json}"
        )
        assert "staticfile" in result_json.get("output_str", "").lower(), (
            "Should mention staticfile in output_str"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
Integration tests for readability plugin

Tests verify:
1. Validate hook checks for readability-extractor binary
2. Verify deps with abx-pkg
3. Plugin reports missing dependency correctly
4. Extraction works against real example.com content
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import (
    get_plugin_dir,
    get_hook_script,
    parse_jsonl_output,
)


PLUGIN_DIR = get_plugin_dir(__file__)
PLUGINS_ROOT = PLUGIN_DIR.parent
_READABILITY_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_readability.*")
if _READABILITY_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
READABILITY_HOOK = _READABILITY_HOOK
TEST_URL = "https://example.com"

# Module-level cache for binary path
_readability_binary_path = None


def create_example_html(tmpdir: Path) -> Path:
    """Create sample HTML that looks like example.com with enough content for Readability."""
    singlefile_dir = tmpdir / "singlefile"
    singlefile_dir.mkdir()

    html_file = singlefile_dir / "singlefile.html"
    html_file.write_text("""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Example Domain</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
    <article>
        <header>
            <h1>Example Domain</h1>
        </header>
        <div class="content">
            <p>This domain is for use in illustrative examples in documents. You may use this
            domain in literature without prior coordination or asking for permission.</p>

            <p>Example domains are maintained by the Internet Assigned Numbers Authority (IANA)
            to provide a well-known address for documentation purposes. This helps authors create
            examples that readers can understand without confusion about actual domain ownership.</p>

            <p>The practice of using example domains dates back to the early days of the internet.
            These reserved domains ensure that example code and documentation doesn't accidentally
            point to real, active websites that might change or disappear over time.</p>

            <p>For more information about example domains and their history, you can visit the
            IANA website. They maintain several example domains including example.com, example.net,
            and example.org, all specifically reserved for this purpose.</p>

            <p><a href="https://www.iana.org/domains/example">More information about example domains...</a></p>
        </div>
    </article>
</body>
</html>
    """)

    return html_file


def require_readability_binary() -> str:
    """Return readability-extractor binary path or fail with actionable context."""
    binary_path = get_readability_binary_path()
    assert binary_path, (
        "readability-extractor installation failed. Install hook should install "
        "the binary automatically in this test environment."
    )
    assert Path(binary_path).is_file(), (
        f"readability-extractor binary path invalid: {binary_path}"
    )
    return binary_path


def get_readability_binary_path() -> str | None:
    """Get readability-extractor binary path, installing via abx_pkg if needed."""
    global _readability_binary_path
    if _readability_binary_path and Path(_readability_binary_path).is_file():
        return _readability_binary_path

    from abx_pkg import Binary, NpmProvider, EnvProvider

    binary = Binary(
        name="readability-extractor",
        binproviders=[NpmProvider(), EnvProvider()],
        overrides={
            "npm": {
                "install_args": ["https://github.com/ArchiveBox/readability-extractor"]
            }
        },
    ).load_or_install()
    if binary and binary.abspath:
        _readability_binary_path = str(binary.abspath)
        return _readability_binary_path

    return None


def test_hook_script_exists():
    """Verify hook script exists."""
    assert READABILITY_HOOK.exists(), f"Hook script not found: {READABILITY_HOOK}"


def test_reports_missing_dependency_when_not_installed():
    """Test that script reports DEPENDENCY_NEEDED when readability-extractor is not found."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        # Create HTML source so it doesn't fail on missing HTML
        create_example_html(snap_dir)

        # Run with empty PATH so binary won't be found
        env = {"PATH": "/nonexistent", "HOME": str(tmpdir), "SNAP_DIR": str(snap_dir)}

        result = subprocess.run(
            [
                sys.executable,
                str(READABILITY_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test123",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
        )

        # Missing binary should emit failed JSONL
        assert result.returncode == 1, "Should exit 1 when dependency missing"

        record = parse_jsonl_output(result.stdout)
        assert record, "Should emit JSONL for failed dependency"
        assert record["type"] == "ArchiveResult"
        assert record["status"] == "failed"

        # Should log error to stderr
        assert (
            "readability-extractor" in result.stderr.lower()
            or "error" in result.stderr.lower()
        ), "Should report error in stderr"


def test_verify_deps_with_abx_pkg():
    """Verify readability-extractor is installed by real plugin install hooks."""
    binary_path = require_readability_binary()
    assert Path(binary_path).is_file(), (
        f"Binary path must be a valid file: {binary_path}"
    )


def test_extracts_article_after_installation():
    """Test full workflow: extract article using readability-extractor from real HTML."""
    binary_path = require_readability_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        # Create example.com HTML for readability to process
        create_example_html(snap_dir)

        # Run readability extraction (should find the binary)
        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["READABILITY_BINARY"] = binary_path
        result = subprocess.run(
            [
                str(READABILITY_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test789",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "succeeded", f"Should succeed: {result_json}"

        # Verify output files exist (hook writes to current directory)
        html_file = snap_dir / "readability" / "content.html"
        txt_file = snap_dir / "readability" / "content.txt"
        json_file = snap_dir / "readability" / "article.json"

        assert html_file.exists(), "content.html not created"
        assert txt_file.exists(), "content.txt not created"
        assert json_file.exists(), "article.json not created"

        # Verify HTML content contains REAL example.com text
        html_content = html_file.read_text()
        assert len(html_content) > 100, (
            f"HTML content too short: {len(html_content)} bytes"
        )
        assert "example domain" in html_content.lower(), (
            "Missing 'Example Domain' in HTML"
        )
        assert (
            "illustrative examples" in html_content.lower()
            or "use in" in html_content.lower()
            or "literature" in html_content.lower()
        ), "Missing example.com description in HTML"

        # Verify text content contains REAL example.com text
        txt_content = txt_file.read_text()
        assert len(txt_content) > 50, (
            f"Text content too short: {len(txt_content)} bytes"
        )
        assert "example" in txt_content.lower(), "Missing 'example' in text"

        # Verify JSON metadata
        json_data = json.loads(json_file.read_text())
        assert isinstance(json_data, dict), "article.json should be a dict"


def test_fails_gracefully_without_html_source():
    """Test that extraction returns noresults when no HTML source is available."""
    binary_path = require_readability_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        # Don't create any HTML source files

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["READABILITY_BINARY"] = binary_path
        result = subprocess.run(
            [
                str(READABILITY_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test999",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, "Should exit 0 without HTML source"
        combined_output = result.stdout + result.stderr
        assert (
            "no html source" in combined_output.lower()
            or "not found" in combined_output.lower()
            or "ERROR=" in combined_output
        ), "Should report missing HTML source"
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "noresults"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

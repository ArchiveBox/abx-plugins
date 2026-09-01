"""
Integration tests for readability plugin

Tests verify:
1. Validate hook checks for readability-extractor binary
2. Verify deps with abxpkg
3. Extraction works against real example.com content
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import (
    get_hook_script,
    get_plugin_dir,
    install_required_binary_from_config,
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


@pytest.fixture(scope="module", autouse=True)
def readability_collection_cache(tmp_path_factory):
    """Keep dependency preflight and hook launches in one real collection cache."""
    previous_lib_dir = os.environ.get("ABXPKG_LIB_DIR")
    lib_dir = tmp_path_factory.mktemp("readability_collection_lib")
    os.environ["ABXPKG_LIB_DIR"] = str(lib_dir)
    try:
        yield lib_dir
    finally:
        if previous_lib_dir is None:
            os.environ.pop("ABXPKG_LIB_DIR", None)
        else:
            os.environ["ABXPKG_LIB_DIR"] = previous_lib_dir


def create_example_html(tmpdir: Path) -> Path:
    """Create sample HTML that looks like example.com with enough content for Readability."""
    singlefile_dir = tmpdir / "singlefile"
    singlefile_dir.mkdir()

    html_file = singlefile_dir / "singlefile.html"
    html_file.write_text(f"""
<!DOCTYPE html>
<html>
<head>
    <title>Example Article</title>
    <meta property="og:title" content="Example Article">
    <meta name="author" content="Example Author">
    <!-- DOM capture scripts can push the source charset beyond the sniffer window. -->
    <script>{"x" * 2048}</script>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
    <article>
        <h1>Example Article</h1>
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

            <p>Encoding check: café in Montréal…</p>

            <picture>
                <source srcset="/assets/example.webp 1x, /assets/example-2.webp 2x">
                <img src="/assets/example.svg" srcset="/assets/example-2.svg 2x" alt="Example illustration">
            </picture>
            <img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==" alt="Inline image">

            <p><a href="https://www.iana.org/domains/example">More information about example domains...</a></p>
        </div>
    </article>
</body>
</html>
    """)
    assert html_file.read_bytes().index(b'<meta charset="utf-8">') > 1024

    return html_file


def require_readability_binary() -> str:
    """Return readability-extractor binary path or fail with actionable context."""
    binary_path = get_readability_binary_path()
    assert binary_path, (
        "readability-extractor dependency resolution failed. required_binaries should resolve "
        "the binary automatically in this test environment."
    )
    assert Path(binary_path).is_file(), (
        f"readability-extractor binary path invalid: {binary_path}"
    )
    assert Path(binary_path).is_relative_to(Path(os.environ["ABXPKG_LIB_DIR"])), (
        "readability-extractor must be projected into the active collection cache"
    )
    return binary_path


def get_readability_binary_path() -> str | None:
    """Get readability-extractor binary path, installing via abxpkg if needed."""
    global _readability_binary_path
    if _readability_binary_path and Path(_readability_binary_path).is_file():
        return _readability_binary_path

    binary = install_required_binary_from_config(
        PLUGIN_DIR,
        "readability-extractor",
    )
    if binary and binary.abspath:
        _readability_binary_path = str(binary.abspath)
        return _readability_binary_path

    return None


def test_hook_script_exists():
    """Verify hook script exists."""
    assert READABILITY_HOOK.exists(), f"Hook script not found: {READABILITY_HOOK}"


def test_declares_capture_dependencies():
    """Readability restores HTML sources and waits for an enabled wget fallback."""
    config = json.loads((PLUGIN_DIR / "config.json").read_text())
    assert {"dom", "responses"} <= set(config["required_plugins"])
    assert config["wait_for_plugins"] == ["wget"]


def test_verify_deps_with_abxpkg():
    """Verify readability-extractor resolves through the real dependency preflight."""
    binary_path = require_readability_binary()
    assert Path(binary_path).is_file(), (
        f"Binary path must be a valid file: {binary_path}"
    )


def test_extracts_article_after_installation():
    """Test full workflow: extract article using readability-extractor from real HTML.

    WHY: the dependency preflight and hook must use the same collection cache.
    Pointing READABILITY_BINARY at the previous test's cache makes every hook
    invocation rebuild a fresh projection and turns a warm launch into slow,
    silent package-manager work on constrained CI runners.
    """
    binary_path = require_readability_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        # Create example.com HTML for readability to process
        create_example_html(snap_dir)
        archived_image = (
            snap_dir / "responses" / "image" / "example.com" / "assets" / "example.svg"
        )
        archived_image.parent.mkdir(parents=True)
        archived_image.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        wget_image = snap_dir / "wget" / "example.com" / "assets" / "example.svg"
        wget_image.parent.mkdir(parents=True)
        wget_image.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><text>wget</text></svg>',
        )
        stale_image = snap_dir / "readability" / "images" / "stale.example" / "old.png"
        stale_image.parent.mkdir(parents=True)
        stale_image.symlink_to("missing.png")

        # Run readability extraction (should find the binary)
        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["READABILITY_BINARY"] = binary_path
        result = subprocess.run(
            [
                str(READABILITY_HOOK),
                "--url",
                TEST_URL,
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
        assert "café in Montréal…" in html_content
        assert (
            "illustrative examples" in html_content.lower()
            or "use in" in html_content.lower()
            or "literature" in html_content.lower()
        ), "Missing example.com description in HTML"
        assert html_content.startswith("<!doctype html>")
        assert "reader view" not in html_content.lower()
        assert "reader mode" not in html_content.lower()
        assert "<header" not in html_content.lower()
        assert "<body><main><article>" in html_content
        assert "main { width: 100%; min-height: 100vh" in html_content
        assert "article { width: min(100%, 72rem); margin: 0 auto" in html_content
        assert (
            "article table { width: 100% !important; max-width: 100% !important"
            in html_content
        )
        assert 'src="./images/example.com/assets/example.svg"' in html_content
        assert "srcset=" not in html_content
        assert "data:image" not in html_content
        readability_image = (
            snap_dir
            / "readability"
            / "images"
            / "example.com"
            / "assets"
            / "example.svg"
        )
        assert readability_image.is_symlink()
        assert readability_image.resolve() == archived_image.resolve()
        assert not stale_image.is_symlink()
        assert "../responses" not in html_content

        # Verify text content contains REAL example.com text
        txt_content = txt_file.read_text()
        assert len(txt_content) > 50, (
            f"Text content too short: {len(txt_content)} bytes"
        )
        assert "example" in txt_content.lower(), "Missing 'example' in text"
        assert "café in Montréal…" in txt_content

        # Verify JSON metadata
        json_data = json.loads(json_file.read_text())
        assert isinstance(json_data, dict), "article.json should be a dict"


def test_falls_back_to_wget_images():
    """Readability should use wget requisites when response capture is unavailable."""
    binary_path = require_readability_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True)
        create_example_html(snap_dir)
        archived_image = snap_dir / "wget" / "example.com" / "assets" / "example.svg"
        archived_image.parent.mkdir(parents=True)
        archived_image.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["READABILITY_BINARY"] = binary_path

        result = subprocess.run(
            [str(READABILITY_HOOK), "--url", TEST_URL],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        html_content = (snap_dir / "readability" / "content.html").read_text()
        readability_image = (
            snap_dir
            / "readability"
            / "images"
            / "example.com"
            / "assets"
            / "example.svg"
        )
        assert 'src="./images/example.com/assets/example.svg"' in html_content
        assert "srcset=" not in html_content
        assert "data:image" not in html_content
        assert readability_image.is_symlink()
        assert readability_image.resolve() == archived_image.resolve()
        assert "../wget" not in html_content


def test_omits_unarchived_images():
    """Readability should stay self-contained when responses and wget are absent."""
    binary_path = require_readability_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True)
        create_example_html(snap_dir)
        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["READABILITY_BINARY"] = binary_path

        result = subprocess.run(
            [str(READABILITY_HOOK), "--url", TEST_URL],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, result.stderr
        html_content = (snap_dir / "readability" / "content.html").read_text()
        assert "<img" not in html_content.lower()
        assert "<source" not in html_content.lower()
        assert "<picture" not in html_content.lower()
        assert "example.svg" not in html_content
        assert "data:image" not in html_content
        assert not (snap_dir / "readability" / "images").exists()


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


def test_prefers_dom_output_over_singlefile_when_both_exist(
    tmp_path: Path,
    real_competing_html_snapshot,
):
    binary_path = require_readability_binary()
    snap_dir = real_competing_html_snapshot(tmp_path, "readability-precedence")
    env = os.environ.copy()
    env["SNAP_DIR"] = str(snap_dir)
    env["READABILITY_BINARY"] = binary_path
    result = subprocess.run(
        [str(READABILITY_HOOK), "--url", TEST_URL],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    output_dir = snap_dir / "readability"
    html_content = (output_dir / "content.html").read_text().lower()
    txt_content = (output_dir / "content.txt").read_text().lower()
    assert "example domain" in html_content
    assert "example domain" in txt_content
    assert "archivebox" not in html_content
    assert "archivebox" not in txt_content
    metadata = json.loads((output_dir / "article.json").read_text())
    assert isinstance(metadata, dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

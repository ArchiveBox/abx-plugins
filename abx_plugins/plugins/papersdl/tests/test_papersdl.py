"""
Integration tests for papersdl plugin

Tests verify:
1. Hook script exists
2. Dependencies installed via validation hooks
3. Verify deps with abxpkg
4. Paper extraction works on paper URLs
5. JSONL output is correct
6. Config options work
7. Handles non-paper URLs gracefully
"""

import os
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlopen
import pytest

from abx_plugins.plugins.base.testing import (
    install_required_binary_from_config,
    parse_jsonl_output,
)

PLUGIN_DIR = Path(__file__).parent.parent
_PAPERSDL_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_papersdl.*"), None)
if _PAPERSDL_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
PAPERSDL_HOOK = _PAPERSDL_HOOK
TEST_URL = "https://example.com"

# Module-level cache for binary path
_papersdl_binary_path = None
_papersdl_install_error = None
_papersdl_lib_root = None


def require_papersdl_binary() -> str:
    """Return papers-dl binary path or fail with actionable context."""
    binary_path = get_papersdl_binary_path()
    assert binary_path, (
        "papers-dl dependency resolution failed. required_binaries must resolve the real papers-dl package "
        f"from PyPI. {_papersdl_install_error or ''}".strip()
    )
    assert Path(binary_path).is_file(), f"papers-dl binary path invalid: {binary_path}"
    return binary_path


def get_papersdl_binary_path():
    """Get the installed papers-dl binary path from cache or by installing with abxpkg."""
    global _papersdl_binary_path, _papersdl_install_error, _papersdl_lib_root
    if _papersdl_binary_path and Path(_papersdl_binary_path).is_file():
        return _papersdl_binary_path

    if not _papersdl_lib_root:
        _papersdl_lib_root = tempfile.mkdtemp(prefix="papersdl-lib-")

    env = os.environ.copy()
    env["ABXPKG_LIB_DIR"] = str(Path(_papersdl_lib_root))

    try:
        loaded = install_required_binary_from_config(PLUGIN_DIR, "papers-dl", env=env)
    except Exception as err:
        _papersdl_install_error = f"abxpkg install failed: {err}"
        return None

    _papersdl_binary_path = str(loaded.loaded_abspath or "")
    return _papersdl_binary_path or None


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert PAPERSDL_HOOK.exists(), f"Hook not found: {PAPERSDL_HOOK}"


def test_verify_deps_with_abxpkg():
    """Verify papers-dl is installed by calling the REAL installation hooks."""
    binary_path = require_papersdl_binary()
    assert Path(binary_path).is_file(), (
        f"Binary path must be a valid file: {binary_path}"
    )
    help_result = subprocess.run(
        [binary_path, "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert help_result.returncode == 0, (
        f"Installed papers-dl CLI must be runnable: stdout={help_result.stdout[:400]} "
        f"stderr={help_result.stderr[:400]}"
    )


def test_handles_non_paper_url():
    """Test that papers-dl extractor handles non-paper URLs gracefully via hook."""
    binary_path = require_papersdl_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["PAPERSDL_BINARY"] = binary_path

        # Run papers-dl extraction hook on non-paper URL
        result = subprocess.run(
            [
                str(PAPERSDL_HOOK),
                "--url",
                "https://example.com",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        # Should exit 0 even for non-paper URL
        assert result.returncode == 0, (
            f"Should handle non-paper URL gracefully: {result.stderr}"
        )

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "noresults", (
            f"Non-paper URL should report noresults: {result_json}"
        )
        assert result_json["output_str"] == "No papers found", result_json


def test_config_save_papersdl_false_skips():
    """Test that PAPERSDL_ENABLED=False exits without emitting JSONL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["PAPERSDL_ENABLED"] = "False"

        result = subprocess.run(
            [
                str(PAPERSDL_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, (
            f"Should exit 0 when feature disabled: {result.stderr}"
        )

        # Feature disabled should emit skipped JSONL
        assert "Skipping" in result.stderr or "False" in result.stderr, (
            "Should log skip reason to stderr"
        )

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Expected skipped JSONL output"
        assert result_json["status"] == "skipped", result_json
        assert result_json["output_str"] == "PAPERSDL_ENABLED=False", result_json


def test_config_timeout():
    """Test that PAPERSDL_TIMEOUT config is respected."""
    binary_path = require_papersdl_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["PAPERSDL_BINARY"] = binary_path
        env["PAPERSDL_TIMEOUT"] = "30"

        result = subprocess.run(
            [
                str(PAPERSDL_HOOK),
                "--url",
                "https://example.com",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, "Should complete without hanging"
        result_json = parse_jsonl_output(result.stdout)
        assert result_json == {
            "type": "ArchiveResult",
            "status": "noresults",
            "output_str": "No papers found",
        }, result_json


def test_real_public_paper_download():
    """Test that papers-dl downloads a real public paper PDF via DOI or arXiv URL."""
    binary_path = require_papersdl_binary()

    paper_url = "https://arxiv.org/abs/1706.03762"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env["PAPERSDL_BINARY"] = binary_path
        env["PAPERSDL_TIMEOUT"] = "120"
        env["PAPERSDL_ARGS_EXTRA"] = '["--providers", "arxiv"]'
        env["SNAP_DIR"] = str(tmpdir)

        result = subprocess.run(
            [
                str(PAPERSDL_HOOK),
                "--url",
                paper_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )

        assert result.returncode == 0, (
            f"Paper download should succeed: stdout={result.stdout} stderr={result.stderr}"
        )

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, f"Should emit ArchiveResult JSONL. stdout: {result.stdout}"
        assert result_json["type"] == "ArchiveResult", result_json
        assert result_json["status"] == "succeeded", result_json

        papersdl_dir = tmpdir / "papersdl"
        downloaded_files = sorted(
            path for path in papersdl_dir.iterdir() if path.is_file()
        )
        pdf_files = [path for path in downloaded_files if path.suffix.lower() == ".pdf"]
        assert len(pdf_files) == 1, (
            f"Expected exactly one downloaded PDF: {downloaded_files}"
        )

        output_path = pdf_files[0]
        assert result_json["output_str"] == f"papersdl/{output_path.name}", result_json
        assert output_path.stat().st_size > 0, (
            f"Downloaded paper file is empty: {output_path}"
        )


def test_fetches_non_ascii_article_url(tmp_path, httpserver):
    """A page URL like /über must be fetched, not crash http.client's ASCII encode."""
    httpserver.expect_request("/über/artikel").respond_with_data(
        "<html><body><p>No citations here.</p></body></html>",
        content_type="text/html; charset=utf-8",
    )
    result = subprocess.run(
        [str(PAPERSDL_HOOK), "--url", httpserver.url_for("/über/artikel")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PAPERSDL_BINARY": require_papersdl_binary(),
            "SNAP_DIR": str(tmp_path),
        },
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert parse_jsonl_output(result.stdout) == {
        "type": "ArchiveResult",
        "status": "noresults",
        "output_str": "No papers found",
    }, result.stdout + result.stderr
    assert [request.environ["REQUEST_URI"] for request, _ in httpserver.log] == [
        "/%C3%BCber/artikel",
    ]


def test_reports_cited_papers_that_could_not_be_downloaded(tmp_path, httpserver):
    """A cited paper with no downloadable PDF is not 'No papers found'."""
    doi = "10.5555/abx-papersdl-undownloadable"
    httpserver.expect_request("/news/article").respond_with_data(
        f'<html><body><p>A study <a href="https://doi.org/{doi}">reported today</a>.</p></body></html>',
        content_type="text/html; charset=utf-8",
    )
    result = subprocess.run(
        [str(PAPERSDL_HOOK), "--url", httpserver.url_for("/news/article")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PAPERSDL_BINARY": require_papersdl_binary(),
            "PAPERSDL_TIMEOUT": "120",
            "SNAP_DIR": str(tmp_path),
        },
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Downloading paper: {doi}" in result.stderr, result.stderr
    assert parse_jsonl_output(result.stdout) == {
        "type": "ArchiveResult",
        "status": "noresults",
        "output_str": f"Found 1 paper, none downloadable: {doi}",
    }, result.stdout + result.stderr
    assert not list((tmp_path / "papersdl").glob("*.pdf"))


@pytest.mark.parametrize("saved_html", [False, True])
def test_download_paper_linked_from_article(tmp_path, saved_html):
    """Download the paper linked by a real page whose own URL has no identifier."""
    article_url = "https://huggingface.co/papers/1706.03762"
    if saved_html:
        with urlopen(article_url, timeout=30) as response:
            content = response.read()
        assert b"https://arxiv.org/abs/1706.03762" in content
        dom = tmp_path / "dom" / "output.html"
        dom.parent.mkdir()
        dom.write_bytes(content)

    result = subprocess.run(
        [str(PAPERSDL_HOOK), "--url", article_url],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PAPERSDL_BINARY": require_papersdl_binary(),
            "PAPERSDL_TIMEOUT": "120",
            "SNAP_DIR": str(tmp_path),
        },
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "succeeded", result.stdout + result.stderr
    pdfs = list((tmp_path / "papersdl").glob("*.pdf"))
    assert len(pdfs) == 1, pdfs
    assert pdfs[0].read_bytes().startswith(b"%PDF-"), pdfs[0]
    assert record["output_str"] == f"papersdl/{pdfs[0].name}", record


def test_download_linked_and_inline_citations(tmp_path):
    """Real PDFs for linked DOI aliases and an unlinked inline arXiv citation."""
    dom = tmp_path / "dom" / "output.html"
    dom.parent.mkdir()
    dom.write_text(
        """<!doctype html><html><head>
        <meta name="citation_doi" content="10.48550/arXiv.1706.03762">
        </head><body><article>
        <p>The Transformer paper is
        <a href="https://doi.org/10.48550%2FarXiv.1706.03762?source=citation">Attention Is All You Need</a>
        (DOI: 10.48550/arXiv.<em>1706.03762</em>).
        Its <a href="https://arxiv.org/pdf/1706.03762.pdf">PDF</a> is also available.</p>
        <p>BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding
        (arXiv: 1810.04805).</p>
        </article></body></html>""",
    )
    result = subprocess.run(
        [str(PAPERSDL_HOOK), "--url", "https://example.com/research-notes"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PAPERSDL_BINARY": require_papersdl_binary(),
            "PAPERSDL_TIMEOUT": "120",
            "SNAP_DIR": str(tmp_path),
        },
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert parse_jsonl_output(result.stdout) == {
        "type": "ArchiveResult",
        "status": "succeeded",
        "output_str": "2 PDFs downloaded",
    }, result.stdout + result.stderr
    pdfs = list((tmp_path / "papersdl").glob("*.pdf"))
    assert len(pdfs) == 2, pdfs
    assert all(path.read_bytes().startswith(b"%PDF-") for path in pdfs)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


@pytest.mark.parametrize(
    ("identifier", "provider"),
    [
        ("10.1126/sciadv.1602552", "pmc"),
        ("10.1016/j.cell.2024.11.027", "pmc"),
        ("10.21105/joss.01112", "unpaywall"),
        ("10.1101/833400v1.full.pdf", "biorxiv"),
    ],
)
def test_vendored_doi_provider_downloads_pdf(tmp_path, identifier, provider):
    """Exercise the installed PyPI CLI through the real hook and vendored providers."""
    import json

    url = (
        f"https://www.biorxiv.org/content/{identifier}"
        if provider == "biorxiv"
        else f"https://doi.org/{identifier}"
    )
    result = subprocess.run(
        [str(PAPERSDL_HOOK), "--url", url],
        cwd=tmp_path,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "PAPERSDL_BINARY": require_papersdl_binary(),
            "PAPERSDL_ARGS_EXTRA": json.dumps(["--providers", provider]),
            "PAPERSDL_UNPAYWALL_EMAIL": "unpaywall-papersdl@archivebox.io",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record is not None, result.stdout
    assert record["status"] == "succeeded", (record, result.stderr)
    pdfs = list((tmp_path / "papersdl").glob("*.pdf"))
    assert len(pdfs) == 1, result.stdout
    assert record["output_str"] == f"papersdl/{pdfs[0].name}"
    assert pdfs[0].read_bytes().startswith(b"%PDF-")
    assert pdfs[0].stat().st_size > 10_000


def test_science_brain_sleep_article_discovers_and_downloads_cited_paper(tmp_path):
    """Regression for the real Science news article's primary Cell citation."""
    import shutil

    (tmp_path / "dom").mkdir()
    shutil.copyfile(
        PLUGIN_DIR / "tests/fixtures/science-brain-sleep-citation.html",
        tmp_path / "dom/output.html",
    )
    result = subprocess.run(
        [
            str(PAPERSDL_HOOK),
            "--url",
            "https://www.science.org/content/article/scientists-uncover-how-brain-washes-itself-during-sleep",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "PAPERSDL_BINARY": require_papersdl_binary(),
            "PAPERSDL_UNPAYWALL_EMAIL": "unpaywall-papersdl@archivebox.io",
        },
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert "Downloading paper: 10.1016/j.cell.2024.11.027" in result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record is not None, result.stdout
    assert record["status"] == "succeeded", (record, result.stderr)
    pdfs = list((tmp_path / "papersdl").glob("*.pdf"))
    assert len(pdfs) == 1, result.stderr
    assert record["output_str"] == f"papersdl/{pdfs[0].name}"
    assert pdfs[0].read_bytes().startswith(b"%PDF-")
    assert pdfs[0].stat().st_size > 100_000

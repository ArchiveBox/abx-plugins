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

import hashlib
import json
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
TEST_URL = "https://archivebox.io/tests/"

# Module-level cache for binary path
_papersdl_binary_path = None
_papersdl_install_error = None
_papersdl_lib_root = None


def test_papersdl_does_not_refetch_failed_document(
    tmp_path,
    httpserver,
    ensure_chrome_test_prereqs,
):
    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
        CHROME_NAVIGATE_HOOK,
        chrome_session,
    )

    binary = require_papersdl_binary()
    url = httpserver.url_for("/article")
    with chrome_session(tmp_path, test_url=url, navigate=False, timeout=30) as (
        _,
        _,
        chrome,
        env,
    ):
        httpserver.stop()
        try:
            navigation = subprocess.run(
                [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
                cwd=chrome,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
        finally:
            httpserver.start()
        assert navigation.returncode != 0, navigation.stdout
        recorded = json.loads((chrome / "navigation.json").read_text())
        assert "ERR_CONNECTION_REFUSED" in recorded["error"], recorded
        assert recorded["status"] is None, recorded
        assert recorded["url"] == url
        assert httpserver.url_for("/article") == url
        body = "<html><body><article>No paper citations in this article.</article></body></html>"
        httpserver.expect_request("/article").respond_with_data(
            body,
            content_type="text/html",
        )
        httpserver.expect_request("/other").respond_with_data(
            body,
            content_type="text/html",
        )

        def run(source_url):
            result = subprocess.run(
                [str(PAPERSDL_HOOK), f"--url={source_url}"],
                cwd=chrome.parent,
                env={**env, "PAPERSDL_BINARY": binary},
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, result.stderr
            assert parse_jsonl_output(result.stdout) == {
                "type": "ArchiveResult",
                "status": "noresults",
                "output_str": "No papers found",
            }

        run(url)
        assert not httpserver.log, "Paper discovery retried the failed capture"
        # A failed capture of another URL does not suppress standalone discovery.
        run(httpserver.url_for("/other"))
        assert len(httpserver.log) == 1
        assert httpserver.log[0][0].path == "/other"
        # Saved source still wins over the failed browser navigation.
        source = chrome.parent / "dom/output.html"
        source.parent.mkdir(exist_ok=True)
        source.write_text(body)
        run(url)
        assert len(httpserver.log) == 1


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


def test_wayback_timestamp_is_not_a_paper_identifier(tmp_path):
    """A real archived citation from Wikipedia must not become a bogus DOI."""
    (tmp_path / "dom").mkdir()
    (tmp_path / "dom/output.html").write_text(
        '<a href="https://web.archive.org/web/20100105152318/http://www.networkcomputing.com/1119/1119f1products_5.html">'
        "Network Computing</a>",
    )
    result = subprocess.run(
        [str(PAPERSDL_HOOK), "--url=https://en.wikipedia.org/wiki/Network_switch"],
        cwd=tmp_path,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "PAPERSDL_BINARY": require_papersdl_binary(),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert parse_jsonl_output(result.stdout) == {
        "type": "ArchiveResult",
        "status": "noresults",
        "output_str": "No papers found",
    }, result.stderr
    assert "Downloading paper:" not in result.stderr
    assert not list((tmp_path / "papersdl").glob("*.pdf"))


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
                "https://archivebox.io/tests/",
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
                "https://archivebox.io/tests/",
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

        # A resumed capture overwrites its own files. The second successful
        # download must remain visible instead of being reported as noresults.
        original_bytes = output_path.read_bytes()
        repeated = subprocess.run(
            [str(PAPERSDL_HOOK), "--url", paper_url],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert parse_jsonl_output(repeated.stdout) == result_json
        assert list(papersdl_dir.glob("*.pdf")) == [output_path]
        assert output_path.read_bytes() == original_bytes


@pytest.mark.parametrize("requested_suffix", ["", "e"])
def test_download_checks_pdf_declared_doi(tmp_path, httpserver, requested_suffix):
    """A real corrigendum cites the original DOI but is a different document."""
    original_doi = "10.1051/0004-6361/201833910"
    declared_doi = original_doi + "e"
    with urlopen(
        "https://archimer.ifremer.fr/doc/00654/76633/88063.pdf",
        timeout=30,
    ) as response:
        pdf_bytes = response.read()
    assert pdf_bytes.startswith(b"%PDF-")
    requested_doi = original_doi + requested_suffix
    httpserver.expect_request("/" + requested_doi).respond_with_data(
        '<html><head><meta name="citation_pdf_url" content="/paper.pdf"></head></html>',
        content_type="text/html",
    )
    httpserver.expect_request("/paper.pdf").respond_with_data(
        pdf_bytes,
        content_type="application/pdf",
    )
    result = subprocess.run(
        [
            require_papersdl_binary(),
            "fetch",
            httpserver.url_for("/" + requested_doi),
            "--providers",
            "publisher",
            "--output",
            str(tmp_path),
        ],
        env={**os.environ, "PYTHONPATH": str(PLUGIN_DIR / "papers-dl-patches")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    pdfs = list(tmp_path.glob("*.pdf"))
    assert len(pdfs) == 1, result.stdout + result.stderr
    assert pdfs[0].read_bytes() == pdf_bytes
    if requested_suffix:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Successfully downloaded paper" in result.stdout
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "Successfully downloaded paper" not in result.stdout
        assert (
            f"PDF declares DOI {declared_doi}, requested {original_doi}"
            in result.stderr
        )
        assert pdfs[0].stem == hashlib.md5(pdf_bytes).hexdigest()


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
        ("10.1038/sj.bjp.0705942", "pmc"),
        ("10.1016/j.cell.2024.11.027", "pmc"),
        ("10.21105/joss.01112", "unpaywall"),
        ("10.1086/322460", "unpaywall"),
        ("10.1086/322460", "pmc,unpaywall"),
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
            "PAPERSDL_ARGS": json.dumps(["-v", "fetch"]),
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
    pdfs = list((tmp_path / "papersdl").glob("*.pdf"))
    # These exact external refusals are expected only when every selected
    # provider finished without a PDF. A successful fallback stays succeeded.
    pmc_captcha = provider == "pmc" and any(
        line.startswith(
            (
                "PMC PDF page is not a supported automatic proof ",
                "No publisher PDF metadata ",
            ),
        )
        and "title='Checking your browser - reCAPTCHA'" in line
        for line in result.stderr.splitlines()
    )
    biorxiv_rate_limit = provider == "biorxiv" and (
        f"error: 429, message='Too Many Requests', url='{url}'" in result.stderr
    )
    if record["status"] == "noresults" and (pmc_captcha or biorxiv_rate_limit):
        assert record == {
            "type": "ArchiveResult",
            "status": "noresults",
            "output_str": f"Found 1 paper, none downloadable: {url if provider == 'biorxiv' else identifier}",
        }
        assert not pdfs, pdfs
        assert "Traceback (most recent call last)" not in result.stderr
        print(
            f"Expected provider refusal with no PDF: {provider} {identifier}\n{result.stderr}",
        )
        return
    assert record["status"] == "succeeded", (record, result.stderr)
    if provider == "pmc,unpaywall":
        assert "matching providers: ['pmc']" in result.stderr
        assert "matching providers: ['unpaywall']" in result.stderr
    assert len(pdfs) == 1, result.stdout
    assert record["output_str"] == f"papersdl/{pdfs[0].name}"
    assert pdfs[0].read_bytes().startswith(b"%PDF-")
    assert pdfs[0].stat().st_size > 10_000
    if identifier == "10.1038/sj.bjp.0705942":
        assert pdfs[0].name == (
            "Intravenous anaesthetics inhibit nicotinic acetylcholine "
            "receptor‐mediated currents and Ca2+ transients in rat "
            "intracardiac ganglion neurons.pdf"
        )

    if identifier == "10.1086/322460":
        assert pdfs[0].name == (
            "Timescale Stretch Parameterization of Type Ia "
            "SupernovaB‐Band Light Curves.pdf"
        )


def test_science_brain_sleep_article_discovers_and_downloads_cited_paper(tmp_path):
    """Regression for the real Science news article's primary Cell citation."""
    import json
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
            "PAPERSDL_ARGS": json.dumps(["-v", "fetch"]),
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
    pdfs = list((tmp_path / "papersdl").glob("*.pdf"))
    pmc_captcha = any(
        line.startswith("PMC PDF page is not a supported automatic proof ")
        and "title='Checking your browser - reCAPTCHA'" in line
        for line in result.stderr.splitlines()
    )
    if record["status"] == "noresults" and pmc_captcha:
        assert record == {
            "type": "ArchiveResult",
            "status": "noresults",
            "output_str": "Found 1 paper, none downloadable: 10.1016/j.cell.2024.11.027",
        }
        assert not pdfs, pdfs
        assert "Traceback (most recent call last)" not in result.stderr
        print(
            f"Expected PMC CAPTCHA after all providers found no PDF:\n{result.stderr}",
        )
        return
    assert record["status"] == "succeeded", (record, result.stderr)
    assert len(pdfs) == 1, result.stderr
    assert record["output_str"] == f"papersdl/{pdfs[0].name}"
    assert pdfs[0].read_bytes().startswith(b"%PDF-")
    assert pdfs[0].stat().st_size > 100_000

"""
Integration tests for opendataloader plugin.

Tests verify:
1. Hook script exists
2. required_binaries can resolve the opendataloader-pdf binary
3. Extraction runs with real opendataloader-pdf binary on a real live PDF
4. Multiple PDFs are all processed (not just the first)
5. Config options work (enabled/disabled, FORCE_OCR)
6. Handles missing sources gracefully
"""

import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
import requests

from abx_plugins.plugins.base.testing import (
    get_hydrated_required_binary,
    install_required_binary_from_config,
    parse_jsonl_output,
)
from abx_plugins.plugins.base.utils import load_required_binary

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent
_OPENDATALOADER_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_opendataloader.*"), None)
if _OPENDATALOADER_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
OPENDATALOADER_HOOK = _OPENDATALOADER_HOOK
TEST_URL = "https://example.com"
HYBRID_BINARY_RECORD = {
    "name": "opendataloader-pdf-hybrid",
    "binproviders": "env,uv",
    "min_version": "2.0.0",
    "overrides": {
        "uv": {
            "install_root": "{ABXPKG_LIB_DIR}/uv/packages/opendataloader-hybrid",
            "install_args": ["--torch-backend=cpu", "opendataloader-pdf[hybrid]"],
            "postinstall_scripts": True,
        },
    },
}

# Module-level cache for binary path
_opendataloader_binary_path = None
_opendataloader_hybrid_binary_path = None
_java_binary_path = None


def get_opendataloader_binary_path() -> str | None:
    """Get opendataloader-pdf binary path, installing via abxpkg if needed."""
    global _opendataloader_binary_path
    if _opendataloader_binary_path and Path(_opendataloader_binary_path).is_file():
        return _opendataloader_binary_path

    binary = install_required_binary_from_config(PLUGIN_DIR, "opendataloader-pdf")
    if binary and binary.loaded_abspath:
        _opendataloader_binary_path = str(binary.loaded_abspath)
        return _opendataloader_binary_path

    return None


def require_opendataloader_binary() -> str:
    binary_path = get_opendataloader_binary_path()
    assert binary_path, (
        "opendataloader-pdf dependency resolution failed. required_binaries should resolve "
        "the binary automatically in this test environment."
    )
    assert Path(binary_path).is_file(), (
        f"opendataloader-pdf binary path invalid: {binary_path}"
    )
    return binary_path


def require_opendataloader_hybrid_binary() -> str:
    """Resolve the real hybrid server through the plugin's abxpkg contract."""
    global _opendataloader_hybrid_binary_path
    if (
        _opendataloader_hybrid_binary_path
        and Path(
            _opendataloader_hybrid_binary_path,
        ).is_file()
    ):
        return _opendataloader_hybrid_binary_path

    binary = load_required_binary(
        HYBRID_BINARY_RECORD,
        config=os.environ,
        environ=os.environ,
        install=True,
    )
    assert binary.loaded_abspath is not None, (
        "opendataloader-pdf-hybrid dependency resolution failed"
    )
    _opendataloader_hybrid_binary_path = str(binary.loaded_abspath)
    assert Path(_opendataloader_hybrid_binary_path).is_file()
    return _opendataloader_hybrid_binary_path


@contextmanager
def running_hybrid_server(binary: str):
    """Run the real OCR backend and gate on Uvicorn's startup event."""
    process = subprocess.Popen(
        [
            binary,
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--force-ocr",
            "--device",
            "cpu",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    output_stream = process.stdout
    assert output_stream is not None
    startup_result: queue.Queue[str | None] = queue.Queue(maxsize=1)
    output_lines: list[str] = []

    def consume_output() -> None:
        startup_complete = False
        server_url = None
        published = False
        for line in output_stream:
            output_lines.append(line)
            startup_complete = (
                startup_complete or "Application startup complete" in line
            )
            match = re.search(r"Uvicorn running on (http://127\.0\.0\.1:\d+)", line)
            if match:
                server_url = match.group(1)
            if startup_complete and server_url and not published:
                startup_result.put(server_url)
                published = True
        if not published:
            startup_result.put(None)

    output_thread = threading.Thread(target=consume_output, daemon=True)
    output_thread.start()

    try:
        try:
            server_url = startup_result.get(timeout=120)
        except queue.Empty as error:
            raise AssertionError(
                "Hybrid server did not publish its startup event:\n"
                + "".join(output_lines),
            ) from error
        assert server_url, (
            f"Hybrid server exited with {process.poll()} before startup:\n"
            + "".join(output_lines)
        )
        health = requests.get(f"{server_url}/health", timeout=10)
        assert health.status_code == 200, health.text
        assert health.json() == {"status": "ok"}, health.text
        yield server_url
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        output_thread.join(timeout=5)


def get_java_binary_path() -> str | None:
    """Get a Java 11+ binary path, installing via abxpkg if needed."""
    global _java_binary_path
    if _java_binary_path and Path(_java_binary_path).is_file():
        return _java_binary_path

    binary = install_required_binary_from_config(PLUGIN_DIR, "java")
    if binary and binary.loaded_abspath:
        _java_binary_path = str(binary.loaded_abspath)
        return _java_binary_path

    return None


def require_java_binary() -> str:
    binary_path = get_java_binary_path()
    assert binary_path, (
        "Java 11+ installation failed for opendataloader integration tests."
    )
    assert Path(binary_path).is_file(), f"Java binary path invalid: {binary_path}"
    return binary_path


def _download_test_pdf() -> bytes:
    """Download the canonical public PDF fixture."""
    url = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    assert response.content.startswith(b"%PDF-"), response.content[:8]
    return response.content


def test_hook_script_exists():
    assert OPENDATALOADER_HOOK.exists(), f"Hook script not found: {OPENDATALOADER_HOOK}"


def test_parser_runs_after_papersdl_finishes():
    papers_hook = next((PLUGINS_ROOT / "papersdl").glob("on_Snapshot__*.py"))
    assert papers_hook.name < OPENDATALOADER_HOOK.name


def test_verify_deps_with_install_hooks():
    binary_path = require_opendataloader_binary()
    assert Path(binary_path).is_file(), (
        f"Binary path must be a valid file: {binary_path}"
    )
    hybrid_binary_path = require_opendataloader_hybrid_binary()
    assert Path(hybrid_binary_path).is_file()


def test_install_hook_requests_java_dependency():
    java_record = get_hydrated_required_binary(PLUGIN_DIR, "java")
    assert java_record["min_version"] == "11.0.0"
    assert java_record["overrides"]["brew"]["install_args"] == ["openjdk"]
    assert java_record["binproviders"] == "env,apt,brew"


def test_opendataloader_env_executes_exact_abxpkg_selected_java():
    from abx_plugins.plugins.opendataloader.on_Snapshot__67_opendataloader import (
        _opendataloader_env,
    )

    loaded_java = install_required_binary_from_config(PLUGIN_DIR, "java")
    java_path = Path(str(loaded_java.loaded_abspath or ""))
    assert java_path.is_absolute()
    assert java_path.is_file()

    env = _opendataloader_env(str(java_path))

    assert env is not None
    selected_java = Path(env["PATH"].split(os.pathsep)[0]) / "java"
    assert selected_java.samefile(java_path)
    resolved_java = java_path.resolve()
    java_home = resolved_java.parent.parent
    if (java_home / "release").is_file():
        assert env["JAVA_HOME"] == str(java_home)
        assert (Path(env["JAVA_HOME"]) / "bin" / "java").samefile(resolved_java)
    else:
        assert env.get("JAVA_HOME") == os.environ.get("JAVA_HOME")

    version = subprocess.run(
        [str(java_path), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert version.returncode == 0, version.stderr


def test_config_disabled_skips():
    """Test that OPENDATALOADER_ENABLED=False skips extraction."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["OPENDATALOADER_ENABLED"] = "False"

        result = subprocess.run(
            [
                str(OPENDATALOADER_HOOK),
                "--url",
                TEST_URL,
            ],
            check=False,
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, f"Should exit 0 when disabled: {result.stderr}"
        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Expected skipped JSONL output"
        assert result_json["status"] == "skipped", result_json
        assert result_json["output_str"] == "OPENDATALOADER_ENABLED=False", result_json


def test_noresults_without_sources():
    """Test that hook reports noresults when no PDF sources exist."""
    binary_path = require_opendataloader_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = binary_path
        env["OPENDATALOADER_ENABLED"] = "True"

        result = subprocess.run(
            [
                str(OPENDATALOADER_HOOK),
                "--url",
                TEST_URL,
            ],
            check=False,
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, "Should exit 0 without sources"
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "noresults"


def test_no_sources_does_not_wait_for_another_pdf_extraction(tmp_path):
    """An HTML-only snapshot must not compete for the converter's memory slot."""
    binary = require_opendataloader_binary()
    java = require_java_binary()
    source = tmp_path / "busy" / "wget" / "document.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(_download_test_pdf())
    empty = tmp_path / "empty"
    empty.mkdir()
    env = os.environ | {
        "SNAP_DIR": str(source.parents[1]),
        "OPENDATALOADER_BINARY": binary,
        "JAVA_BINARY": java,
        "OPENDATALOADER_ENABLED": "True",
        "ABXBUS_MULTIPROCESS_SEMAPHORE_DIR": str(tmp_path / "semaphores"),
    }
    with subprocess.Popen(
        [str(OPENDATALOADER_HOOK), "--url", TEST_URL],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as busy:
        acquired: queue.Queue[bool] = queue.Queue(maxsize=1)

        def pause_after_acquisition():
            assert busy.stderr is not None
            for line in busy.stderr:
                if "acquired abxbus multiprocess semaphore" in line:
                    os.kill(busy.pid, signal.SIGSTOP)
                    acquired.put(True)
                    return
            acquired.put(False)

        reader = threading.Thread(target=pause_after_acquisition, daemon=True)
        reader.start()
        try:
            assert acquired.get(timeout=30), "Real PDF hook never acquired its slot"
            result = subprocess.run(
                [str(OPENDATALOADER_HOOK), "--url", TEST_URL],
                cwd=tmp_path,
                env=env | {"SNAP_DIR": str(empty)},
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        finally:
            os.kill(busy.pid, signal.SIGCONT)
            stdout, stderr = busy.communicate(timeout=60)
            reader.join(timeout=1)
        assert busy.returncode == 0, stderr
        busy_record = parse_jsonl_output(stdout)
        assert busy_record and busy_record["status"] == "succeeded", busy_record
        assert (source.parents[1] / "opendataloader" / "content.txt").stat().st_size
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "noresults", record
        assert record["output_str"] == "No PDF sources found"
        assert "acquired abxbus multiprocess semaphore" not in result.stderr


@pytest.mark.parametrize("source_dir", ["wget/example.com", "papersdl"])
@pytest.mark.parametrize("extension", ["pdf", "PDF", "PdF"])
@pytest.mark.parametrize(
    "html_response",
    [False, True],
    ids=["pdf-only", "with-html-response"],
)
def test_extract_single_pdf(source_dir, extension, html_response):
    """Test extraction on a single real PDF downloaded from the web."""
    binary_path = require_opendataloader_binary()
    java_binary = require_java_binary()
    pdf_content = _download_test_pdf()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"

        # Place a real PDF in an upstream downloader output directory.
        wget_dir = snap_dir / source_dir
        wget_dir.mkdir(parents=True, exist_ok=True)
        source_name = f"output.{extension}"
        (wget_dir / source_name).write_bytes(pdf_content)
        if html_response:
            # A response named .pdf can contain HTML (e.g. Chrome's PDF viewer),
            # while wget saved the actual PDF. Use real HTML bytes alongside the
            # live PDF, and require that only the PDF reaches the converter.
            html = requests.get("https://archivebox.io/tests/", timeout=30)
            html.raise_for_status()
            assert "text/html" in html.headers["Content-Type"]
            responses_dir = snap_dir / "responses" / "all"
            responses_dir.mkdir(parents=True)
            (responses_dir / f"document.{extension}").write_bytes(html.content)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = binary_path
        env["JAVA_BINARY"] = java_binary
        env["OPENDATALOADER_ENABLED"] = "True"

        result = subprocess.run(
            [
                str(OPENDATALOADER_HOOK),
                "--url",
                "https://example.com/test.pdf",
            ],
            check=False,
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record, "Should have ArchiveResult JSONL output"
        assert record["status"] == "succeeded", (
            f"Should succeed: {record}. stderr: {result.stderr}"
        )
        assert record["output_str"].startswith("opendataloader/"), record

        output_dir = snap_dir / "opendataloader"
        assert (output_dir / "content.md").exists(), "content.md not created"
        assert (output_dir / "content.txt").exists(), "content.txt not created"
        assert (output_dir / "metadata.json").exists(), "metadata.json not created"

        md_content = (output_dir / "content.md").read_text(errors="ignore")
        assert len(md_content) > 10, f"content.md too short: {md_content!r}"

        metadata = json.loads((output_dir / "metadata.json").read_text())
        assert metadata["sources_processed"] == 1
        assert metadata["files"][0]["source_file"] == source_name


def test_document_discovery_reads_siblings_not_parser_output(tmp_path):
    source = tmp_path / "01_responses" / "all" / "document.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(_download_test_pdf())
    ignored = tmp_path / "opendataloader" / "responses" / "document.pdf"
    ignored.parent.mkdir(parents=True)
    ignored.write_bytes(_download_test_pdf())
    try:
        result = subprocess.run(
            [str(OPENDATALOADER_HOOK), "--url", "https://archivebox.io/tests/"],
            cwd=tmp_path,
            env={
                **os.environ,
                "SNAP_DIR": str(tmp_path),
                "OPENDATALOADER_BINARY": require_opendataloader_binary(),
                "OPENDATALOADER_ENABLED": "true",
                "JAVA_BINARY": require_java_binary(),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", record
        metadata = json.loads((tmp_path / "opendataloader/metadata.json").read_text())
        assert metadata["sources_processed"] == 1, metadata
        assert Path(metadata["files"][0]["source_path"]) == source.resolve()
        assert source.read_bytes() == _download_test_pdf()
    finally:
        ignored.unlink()


def test_extract_multiple_pdfs():
    """Test that ALL PDFs are processed when multiple exist across plugins.

    Places PDFs in both responses/ and staticfile/ directories and verifies
    the hook processes every one, not just the first.
    """
    binary_path = require_opendataloader_binary()
    java_binary = require_java_binary()
    pdf_content = _download_test_pdf()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"

        # Place PDF in responses/ as if the server served a PDF
        responses_dir = snap_dir / "responses" / "application" / "example.com"
        responses_dir.mkdir(parents=True, exist_ok=True)
        (responses_dir / "document.pdf").write_bytes(pdf_content)

        # Place another PDF in staticfile/ as if a linked PDF was downloaded
        staticfile_dir = snap_dir / "staticfile" / "example.com"
        staticfile_dir.mkdir(parents=True, exist_ok=True)
        (staticfile_dir / "linked.pdf").write_bytes(pdf_content)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = binary_path
        env["JAVA_BINARY"] = java_binary
        env["OPENDATALOADER_ENABLED"] = "True"

        result = subprocess.run(
            [
                str(OPENDATALOADER_HOOK),
                "--url",
                "https://example.com/docs",
            ],
            check=False,
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record, "Should have ArchiveResult JSONL output"
        assert record["status"] == "succeeded", f"Should succeed: {record}"

        output_dir = snap_dir / "opendataloader"
        metadata = json.loads((output_dir / "metadata.json").read_text())
        assert metadata["sources_processed"] == 2, (
            f"Expected 2 PDFs processed, got {metadata['sources_processed']}. "
            f"Files: {metadata['files']}"
        )
        assert metadata["total_sources_found"] == 2

        # Verify combined output contains content from both files
        md_content = (output_dir / "content.md").read_text(errors="ignore")
        assert "---" in md_content or md_content.count("<!-- source:") >= 2, (
            "Combined markdown should contain content from both PDFs"
        )


LINE_ART_PDF_URL = (
    "https://raw.githubusercontent.com/Nakul22/website/"
    "2169e127854ab55ea4346a5be38e44ed9208e77f/"
    "assets/papers/litefoot_sensys2024_nakul.pdf"
)
# Author-hosted immutable copy, byte-identical to the original UMD regression PDF.
LINE_ART_PDF_SHA256 = "4d77dc41448f7092e9b45809e156cf49a4722b05e351b33fd731bfeb1f801ee3"
# The demo container's JVM ergonomics: 25% of its 10 GiB memory limit.
PRODUCTION_HEAP_ENV = {"JAVA_TOOL_OPTIONS": "-Xmx2560m"}


def _download_line_art_pdf() -> bytes:
    response = requests.get(LINE_ART_PDF_URL, timeout=60)
    response.raise_for_status()
    assert response.content.startswith(b"%PDF-"), response.content[:8]
    assert hashlib.sha256(response.content).hexdigest() == LINE_ART_PDF_SHA256
    return response.content


def _run_hook(
    snap_dir: Path,
    cwd: Path,
    url: str,
    extra_env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SNAP_DIR"] = str(snap_dir)
    env["OPENDATALOADER_BINARY"] = require_opendataloader_binary()
    env["JAVA_BINARY"] = require_java_binary()
    env["OPENDATALOADER_ENABLED"] = "True"
    env.update(extra_env)
    return subprocess.run(
        [str(OPENDATALOADER_HOOK), "--url", url],
        check=False,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def test_line_art_pdf_extracts_within_production_heap():
    """The pinned converter must handle line-art-heavy papers in a 2.5 GiB heap.

    WHY: opendataloader-pdf 2.5.9 through 2.5.12 grow veraPDF's line-art chunk
    list without bound on this real paper (OutOfMemoryError in
    ChunkParser.processLineArts even with an 8 GiB heap). 2.5.8 extracts it in
    seconds, so the plugin pins that release.

    The unpinned hybrid OCR extra installs opendataloader-pdf 2.5.12 into a
    sibling package venv whose site-packages abxpkg also exposes on the hook's
    PYTHONPATH. Install it first so the pinned CLI must not import that copy.
    """
    require_opendataloader_hybrid_binary()
    pdf_content = _download_line_art_pdf()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        source_dir = snap_dir / "responses" / "www.cs.umd.edu" / "papers"
        source_dir.mkdir(parents=True)
        (source_dir / "litefoot_sensys2024_nakul.pdf").write_bytes(pdf_content)

        result = _run_hook(
            snap_dir,
            tmpdir,
            LINE_ART_PDF_URL,
            PRODUCTION_HEAP_ENV,
            timeout=150,
        )

        assert "OutOfMemoryError" not in result.stderr, result.stderr[-2000:]
        assert result.returncode == 0, result.stderr[-2000:]
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", record
        md_content = (snap_dir / "opendataloader" / "content.md").read_text()
        assert "LiTEfoot: Ultra-low-power Localization" in md_content
        text_content = (snap_dir / "opendataloader" / "content.txt").read_text()
        assert "ABSTRACT" in text_content


def test_native_images_survive_duplicate_pdf_filenames(tmp_path):
    pdf = _download_line_art_pdf()
    snap = tmp_path / "snap"
    sources = [
        snap / producer / "document.pdf" for producer in ("responses", "papersdl")
    ]
    original = tmp_path / "original.pdf"
    original.write_bytes(pdf)
    for index, source in enumerate(sources):
        source.parent.mkdir(parents=True)
        if index == 0:
            source.symlink_to(original)
        else:
            source.write_bytes(pdf)

    baseline = tmp_path / "native"
    baseline.mkdir()
    java = Path(require_java_binary())
    env = (
        os.environ
        | PRODUCTION_HEAP_ENV
        | {
            "PATH": str(java.parent) + os.pathsep + os.environ.get("PATH", ""),
        }
    )
    env.pop("PYTHONPATH", None)
    native = subprocess.run(
        [
            require_opendataloader_binary(),
            "-f",
            "markdown,text",
            "-o",
            str(baseline),
            "-q",
            str(sources[0]),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert native.returncode == 0, native.stderr
    image_pattern = r"!\[[^\]]*\]\(<([^>]+)>\)"
    native_md = (baseline / "document.md").read_text()
    native_refs = re.findall(image_pattern, native_md)
    assert native_refs, native_md[:1000]
    native_images = {
        Path(ref).name: (baseline / ref).read_bytes() for ref in native_refs
    }

    result = _run_hook(
        snap,
        tmp_path,
        LINE_ART_PDF_URL,
        PRODUCTION_HEAP_ENV,
        timeout=150,
    )
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "succeeded", record
    output = snap / "opendataloader"
    markdown = (output / "content.md").read_text()
    refs = re.findall(image_pattern, markdown)
    assert len(refs) == 2 * len(native_refs), refs
    assert len({Path(ref).parent for ref in refs}) == 2, refs
    for ref in refs:
        image = output / ref
        assert image.is_file(), ref
        assert image.resolve().is_relative_to(output.resolve()), ref
        assert image.read_bytes() == native_images[image.name], ref
        assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), ref
    assert (output / "content.txt").read_text() == "\n\n---\n\n".join(
        [(baseline / "document.txt").read_text()] * 2,
    )
    # Native Markdown is unchanged apart from its relocated image directory.
    for part in markdown.split("<!-- source: document.pdf -->\n")[1:]:
        image_dir = Path(re.findall(image_pattern, part)[0]).parent.as_posix()
        assert (
            part.removesuffix("\n\n---\n\n").replace(
                image_dir + "/",
                "document_images/",
            )
            == native_md
        )
    assert all(source.read_bytes() == pdf for source in sources)


def test_native_image_directory_argument_keeps_precedence(tmp_path):
    snap = tmp_path / "snap"
    source = snap / "papersdl" / "document.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(_download_line_art_pdf())
    image_dir = snap / "opendataloader" / "configured_images"
    result = _run_hook(
        snap,
        tmp_path,
        LINE_ART_PDF_URL,
        PRODUCTION_HEAP_ENV
        | {"OPENDATALOADER_ARGS_EXTRA": json.dumps(["--image-dir", str(image_dir)])},
        timeout=150,
    )
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "succeeded", record
    refs = re.findall(
        r"!\[[^\]]*\]\(<([^>]+)>\)",
        (image_dir.parent / "content.md").read_text(),
    )
    assert refs
    assert all(Path(ref).parent.as_posix() == "configured_images" for ref in refs), refs
    assert all((image_dir.parent / ref).is_file() for ref in refs), refs


def test_failing_pdf_does_not_discard_other_pdfs():
    """A PDF the converter rejects must not abort extraction of the others."""
    pdf_content = _download_test_pdf()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        # responses/ is searched first, so the broken PDF is processed first.
        responses_dir = snap_dir / "responses" / "example.com"
        responses_dir.mkdir(parents=True)
        (responses_dir / "truncated.pdf").write_bytes(b"%PDF-1.4\ntruncated\n")
        wget_dir = snap_dir / "wget" / "example.com"
        wget_dir.mkdir(parents=True)
        (wget_dir / "valid.pdf").write_bytes(pdf_content)

        result = _run_hook(snap_dir, tmpdir, "https://example.com", {}, timeout=120)

        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", (record, result.stderr)
        assert "opendataloader-pdf failed for truncated.pdf" in result.stderr
        metadata = json.loads(
            (snap_dir / "opendataloader" / "metadata.json").read_text(),
        )
        assert metadata["total_sources_found"] == 2, metadata
        assert [file["source_file"] for file in metadata["files"]] == ["valid.pdf"]


def test_timeout_budget_writes_partial_results_before_runner_kill():
    """Retain extracted PDF bytes while reporting known incomplete extraction.

    WHY: a demo snapshot found 11 PDFs, gave every converter run the full
    120 s hook timeout, and was killed by the runner after three PDFs with
    nothing written.
    """
    pdf_content = _download_test_pdf()
    line_art_pdf = _download_line_art_pdf()
    hook_timeout = 10

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        responses_dir = snap_dir / "responses" / "example.com"
        responses_dir.mkdir(parents=True)
        (responses_dir / "quick.pdf").write_bytes(pdf_content)
        baseline = _run_hook(
            snap_dir,
            tmpdir,
            "https://example.com",
            PRODUCTION_HEAP_ENV,
            timeout=120,
        )
        assert baseline.returncode == 0, baseline.stderr
        baseline_record = parse_jsonl_output(baseline.stdout)
        assert baseline_record and baseline_record["status"] == "succeeded"
        output_dir = snap_dir / "opendataloader"
        baseline_md = (output_dir / "content.md").read_bytes()
        baseline_text = (output_dir / "content.txt").read_bytes()
        assert b"Dummy PDF file" in baseline_md
        assert b"Dummy PDF file" in baseline_text
        for output in output_dir.iterdir():
            output.unlink()

        wget_dir = snap_dir / "wget" / "example.com"
        wget_dir.mkdir(parents=True)
        # Each copy takes seconds to convert, more than the budget in total.
        for index in range(8):
            (wget_dir / f"paper-{index}.pdf").write_bytes(line_art_pdf)

        result = _run_hook(
            snap_dir,
            tmpdir,
            "https://example.com",
            PRODUCTION_HEAP_ENV | {"OPENDATALOADER_TIMEOUT": str(hook_timeout)},
            # The runner's kill: the hook must finish on its own before this.
            timeout=hook_timeout,
        )

        assert "timeout budget spent" in result.stderr, result.stderr[-2000:]
        metadata = json.loads(
            (snap_dir / "opendataloader" / "metadata.json").read_text(),
        )
        assert metadata["total_sources_found"] == 9, metadata
        processed = [file["source_file"] for file in metadata["files"]]
        assert processed[0] == "quick.pdf", processed
        assert len(processed) < 9, processed
        assert (output_dir / "content.md").read_bytes().split(b"\n\n---\n\n", 1)[
            0
        ] == baseline_md
        assert (output_dir / "content.txt").read_bytes().split(b"\n\n---\n\n", 1)[
            0
        ] == baseline_text
        assert result.returncode == 1, result.stderr[-2000:]
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "failed", record
        assert record["output_str"].startswith(
            f"Timed out after {hook_timeout}s extracting paper-",
        ), record


def test_force_ocr_adds_hybrid_flag():
    """Test FORCE_OCR through the real hybrid extraction lifecycle."""
    binary_path = require_opendataloader_binary()
    hybrid_binary = require_opendataloader_hybrid_binary()
    java_binary = require_java_binary()
    pdf_content = _download_test_pdf()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"

        responses_dir = snap_dir / "responses" / "application" / "example.com"
        responses_dir.mkdir(parents=True, exist_ok=True)
        (responses_dir / "output.pdf").write_bytes(pdf_content)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = binary_path
        env["JAVA_BINARY"] = java_binary
        env["OPENDATALOADER_ENABLED"] = "True"
        env["OPENDATALOADER_FORCE_OCR"] = "true"

        with running_hybrid_server(hybrid_binary) as hybrid_url:
            env["OPENDATALOADER_HYBRID_URL"] = hybrid_url
            result = subprocess.run(
                [
                    str(OPENDATALOADER_HOOK),
                    "--url",
                    "https://example.com/scanned.pdf",
                ],
                check=False,
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )

        assert result.returncode == 0, result.stderr

        record = parse_jsonl_output(result.stdout)
        assert record, "Should have ArchiveResult JSONL output"
        assert record["status"] == "succeeded", (
            f"FORCE_OCR must succeed through the hybrid backend, got: {record}. "
            f"stderr: {result.stderr}"
        )

        # Verify actual content was extracted (not a no-op pass)
        output_file = snap_dir / record["output_str"]
        assert output_file.exists(), f"Output file {record['output_str']} not created"
        content = output_file.read_text(errors="ignore")
        assert len(content) > 10, (
            f"Output too short, extraction may be broken: {content!r}"
        )

        assert "opendataloader" in record["output_str"].lower(), record


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

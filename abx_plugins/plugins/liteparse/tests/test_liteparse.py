"""
Integration tests for the liteparse plugin (lit v2+ CLI by LlamaIndex).

Tests verify:
1. Hook script exists
2. Crawl hook emits the correct BinaryRequest record for lit
3. required_binaries can resolve the lit binary via npm
4. Extraction runs end-to-end against a real PDF downloaded from the web
5. Multiple sources in pdf/ + responses/ are all processed
6. LITEPARSE_ENABLED=False short-circuits
7. Missing sources -> noresults
8. v2 OCR works end-to-end on a real scanned PDF
9. v2 OCR works end-to-end on a real PNG image
10. LITEPARSE_OCR_ENABLED=False suppresses the OCR pipeline
11. LITEPARSE_MIN_IMAGE_DIMENSION skips tiny images (favicons/thumbnails)
12. Documents under wget/<host>/... are auto-discovered

All network fixtures use real public URLs; nothing is mocked, faked, or stubbed.
"""

import json
import os
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

import pytest
import requests

from abx_plugins.plugins.base.test_utils import (
    get_hydrated_required_binary,
    install_required_binary_from_config,
    parse_jsonl_output,
)

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent

_LITEPARSE_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_liteparse.*"), None)
if _LITEPARSE_HOOK is None:
    raise FileNotFoundError(f"Snapshot hook not found in {PLUGIN_DIR}")
LITEPARSE_HOOK = _LITEPARSE_HOOK

TEST_URL = "https://example.com"

# Real-world public PDFs/images with known, deterministic text. The exact
# strings asserted below are intrinsic to each file, so assertions remain
# meaningful across lit versions.
#
# PDF_URL_A (w3.org dummy.pdf):
#   native text "Dummy PDF file"
# PDF_URL_B (pdfobject.com sample.pdf):
#   native text "This is a simple PDF file. Fun fun fun."
#   "Lorem ipsum dolor sit amet, consectetuer adipiscing elit."
# PDF_URL_OCR (fscrawler test-ocr.pdf):
#   2-page scanned PDF requiring OCR
#   page 1: "This file contains some words."
#   page 2: "This second part of the text is in Page 2"
# IMAGE_URL_OCR (tesseract eurotext.png):
#   classic Tesseract test image; contains "quick brown fox jumps" + spammer@website.com
PDF_URL_A = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"
PDF_URL_B = "https://pdfobject.com/pdf/sample.pdf"
PDF_URL_OCR = (
    "https://raw.githubusercontent.com/dadoonet/fscrawler/master/"
    "test-documents/src/main/resources/documents/test-ocr.pdf"
)
IMAGE_URL_OCR = "https://tesseract-ocr.github.io/tessdoc/images/eurotext.png"

# Module-level caches so we resolve each binary / download each fixture once
# per test session.
_liteparse_binary_path: str | None = None
_tesseract_binary_path: str | None = None
_imagemagick_binary_path: str | None = None
_tessdata_dir: str | None = None
_downloaded_cache: dict[str, bytes] = {}


def get_liteparse_binary_path() -> str | None:
    """Resolve lit v2 binary path from LiteParse required_binaries config."""
    global _liteparse_binary_path
    if _liteparse_binary_path and Path(_liteparse_binary_path).is_file():
        return _liteparse_binary_path

    binary = install_required_binary_from_config(PLUGIN_DIR, "lit")
    if binary and binary.abspath:
        _liteparse_binary_path = str(binary.abspath)
        return _liteparse_binary_path

    return None


def require_liteparse_binary() -> str:
    """Return lit binary path or fail with actionable context."""
    binary_path = get_liteparse_binary_path()
    assert binary_path, (
        "lit (LiteParse) dependency resolution failed. required_binaries should resolve "
        "the binary automatically in this test environment."
    )
    assert Path(binary_path).is_file(), f"lit binary path invalid: {binary_path}"
    return binary_path


def install_tesseract_binary() -> str:
    """Auto-install tesseract via abxpkg (brew on macOS, apt on Linux).

    Mirrors the ``required_binaries`` declaration in the plugin's config.json
    so the test exercises the exact resolution path that runs in production.
    Hard-fails if abxpkg can't install — tests are expected to work in CI on
    Linux and macOS without manual setup.
    """
    global _tesseract_binary_path
    if _tesseract_binary_path and Path(_tesseract_binary_path).is_file():
        return _tesseract_binary_path

    binary = install_required_binary_from_config(PLUGIN_DIR, "tesseract")
    assert binary and binary.abspath, (
        "abxpkg failed to install tesseract via env/brew/apt — required_binaries "
        "auto-install must work on Linux and macOS CI without manual setup."
    )
    _tesseract_binary_path = str(binary.abspath)
    return _tesseract_binary_path


def install_imagemagick_binary() -> str:
    """Auto-install ImageMagick via abxpkg (brew on macOS, apt on Linux).

    Mirrors the ``required_binaries`` declaration in the plugin's config.json
    so image OCR tests cover the same dependency install path used by normal
    plugin preflight.
    """
    global _imagemagick_binary_path
    if _imagemagick_binary_path and Path(_imagemagick_binary_path).is_file():
        return _imagemagick_binary_path

    binary = install_required_binary_from_config(PLUGIN_DIR, "convert")
    assert binary and binary.abspath, (
        "abxpkg failed to install ImageMagick via env/brew/apt — "
        "required_binaries auto-install must work on Linux and macOS CI "
        "without manual setup."
    )
    _imagemagick_binary_path = str(binary.abspath)
    return _imagemagick_binary_path


def discover_tessdata_dir(tesseract_binary: str) -> str:
    """Ask the installed tesseract for its compiled-in tessdata path.

    Verifies that an ``eng.traineddata`` file actually exists there.
    Hard-fails if not — a successful tesseract install must always come with
    the English language data.
    """
    global _tessdata_dir
    if _tessdata_dir and (Path(_tessdata_dir) / "eng.traineddata").is_file():
        return _tessdata_dir

    proc = subprocess.run(
        [tesseract_binary, "--list-langs"],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "TESSDATA_PREFIX": ""},
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    import re as _re

    match = _re.search(r'available languages in "([^"]+)"', output)
    assert match, (
        f"Could not parse tessdata path from `tesseract --list-langs`: {output!r}"
    )
    path = Path(match.group(1).rstrip("/"))
    assert (path / "eng.traineddata").is_file(), (
        f"tesseract reports tessdata at {path} but eng.traineddata is missing. "
        f"On Debian/Ubuntu run: apt install tesseract-ocr-eng"
    )
    _tessdata_dir = str(path)
    return _tessdata_dir


def require_tessdata_dir() -> str:
    """Install tesseract via abxpkg and return its tessdata directory."""
    return discover_tessdata_dir(install_tesseract_binary())


def _download(url: str, *, expect_prefix: bytes | None = None) -> bytes:
    """Download a fixture once per session and cache the bytes in memory."""
    if url in _downloaded_cache:
        return _downloaded_cache[url]
    resp = requests.get(url, timeout=60)
    assert resp.status_code == 200, f"Failed to download {url}: HTTP {resp.status_code}"
    if expect_prefix is not None:
        assert resp.content[: len(expect_prefix)] == expect_prefix, (
            f"Unexpected content prefix for {url}: {resp.content[:8]!r}"
        )
    _downloaded_cache[url] = resp.content
    return resp.content


def _download_pdf(url: str) -> bytes:
    return _download(url, expect_prefix=b"%PDF-")


def _download_png(url: str) -> bytes:
    return _download(url, expect_prefix=b"\x89PNG\r\n\x1a\n")


def _read_all_liteparse_text(snap_dir: Path) -> str:
    """Concatenate every per-source ``.txt`` under ``liteparse/`` (lowercased).

    The hook intentionally writes one ``<input-name>.txt`` per source and no
    cumulative file, so tests that need to assert "any per-source output
    contains X" walk the per-source files directly here.
    """
    lit_dir = snap_dir / "liteparse"
    if not lit_dir.is_dir():
        return ""
    return "\n".join(
        p.read_text(errors="ignore") for p in sorted(lit_dir.glob("*.txt"))
    ).lower()


def _liteparse_output_stems(snap_dir: Path) -> set[str]:
    """Return the set of per-source output stems in ``liteparse/`` (no ext).

    Useful for asserting "X was processed" / "Y was skipped" without needing
    a manifest file.
    """
    lit_dir = snap_dir / "liteparse"
    if not lit_dir.is_dir():
        return set()
    return {p.stem for p in lit_dir.iterdir() if p.suffix in (".txt", ".json")}


def _make_png(width: int, height: int) -> bytes:
    """Build a minimal valid grayscale PNG of given dimensions (stdlib only).

    Used to fabricate exact-size test fixtures for the dimension filter
    without pulling Pillow into the test deps.
    """

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00" * width for _ in range(height))
    idat = zlib.compress(raw)
    return (
        signature + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    )


def _run_hook(
    snap_dir: Path,
    url: str,
    extra_env: dict[str, str] | None = None,
    *,
    install_tesseract: bool = True,
) -> subprocess.CompletedProcess:
    """Invoke the snapshot hook with snap_dir set.

    By default, also ensures tesseract has been installed via abxpkg and
    pins ``LITEPARSE_TESSERACT_BINARY`` to its absolute path, so the hook's
    tessdata auto-discovery is exercised end-to-end. Pass
    ``install_tesseract=False`` for tests that intentionally explore the
    no-tesseract failure path.
    """
    env = os.environ.copy()
    env["SNAP_DIR"] = str(snap_dir)
    env["LITEPARSE_BINARY"] = require_liteparse_binary()
    if install_tesseract:
        env["LITEPARSE_TESSERACT_BINARY"] = install_tesseract_binary()
        imagemagick_binary = install_imagemagick_binary()
        env["LITEPARSE_IMAGEMAGICK_BINARY"] = imagemagick_binary
        env["PATH"] = f"{Path(imagemagick_binary).parent}:{env.get('PATH', '')}"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(LITEPARSE_HOOK), "--url", url],
        cwd=snap_dir.parent,
        capture_output=True,
        text=True,
        timeout=240,
        env=env,
    )


def test_hook_scripts_exist():
    assert LITEPARSE_HOOK.exists(), f"Snapshot hook not found: {LITEPARSE_HOOK}"


def test_crawl_hook_emits_lit_binary_request_record():
    binary = get_hydrated_required_binary(PLUGIN_DIR, "lit")
    assert binary.get("type", "BinaryRequest") == "BinaryRequest"
    assert binary.get("name") == "lit"
    assert binary.get("overrides", {}).get("npm", {}).get("install_args") == [
        "@llamaindex/liteparse",
    ]


def test_verify_deps_with_install_hooks():
    """lit v2 and OCR support binaries can be installed and resolved via abxpkg."""
    binary_path = require_liteparse_binary()
    assert Path(binary_path).is_file()
    assert Path(install_tesseract_binary()).is_file()
    assert Path(install_imagemagick_binary()).is_file()

    # Confirm we actually got v2+, not legacy v1.
    result = subprocess.run(
        [binary_path, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    version = result.stdout.strip()
    major = int(version.split(".")[0])
    assert major >= 2, f"Expected lit >=2.0.0, got {version}"


def test_config_disabled_skips():
    """LITEPARSE_ENABLED=False short-circuits to a skipped record."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["LITEPARSE_ENABLED"] = "False"

        result = subprocess.run(
            [str(LITEPARSE_HOOK), "--url", TEST_URL],
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
        assert result_json["output_str"] == "LITEPARSE_ENABLED=False", result_json


def test_noresults_without_sources():
    """No sources -> noresults."""
    require_liteparse_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        result = _run_hook(snap_dir, TEST_URL)
        assert result.returncode == 0, "Should exit 0 without sources"
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "noresults"


def test_extract_single_pdf():
    """End-to-end extraction on PDF_URL_B (pdfobject.com sample.pdf).

    Asserts the per-source flat layout (``<input-name>.txt`` directly in
    the plugin output dir, no merged content / manifest files). Opts into
    JSON output to also verify v2's spatial structure (pages, bounding
    boxes); JSON is non-default but supported.
    """
    pdf_content = _download_pdf(PDF_URL_B)

    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"
        pdf_dir = snap_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (pdf_dir / "output.pdf").write_bytes(pdf_content)

        result = _run_hook(
            snap_dir,
            PDF_URL_B,
            extra_env={"LITEPARSE_FORMATS": '["text","json"]'},
        )
        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record, "Should have ArchiveResult JSONL output"
        assert record["status"] == "succeeded", (
            f"Should succeed: {record}. stderr: {result.stderr}"
        )

        output_dir = snap_dir / "liteparse"
        # Per-source flat layout. Output filename = full input filename
        # (incl. extension) + ``.txt`` / ``.json``. No merged content or
        # manifest files exist — search backends index each per-source
        # ``.txt`` directly.
        assert (output_dir / "output.pdf.txt").exists(), list(output_dir.iterdir())
        assert (output_dir / "output.pdf.json").exists(), list(output_dir.iterdir())
        assert not (output_dir / "content.txt").exists(), (
            "merged content.txt should not exist"
        )
        assert not (output_dir / "content.json").exists(), (
            "merged content.json should not exist"
        )
        assert not (output_dir / "metadata.json").exists(), (
            "metadata.json should not exist"
        )

        text_content = (
            (output_dir / "output.pdf.txt").read_text(errors="ignore").lower()
        )
        assert "this is a simple pdf file" in text_content, text_content[:500]
        assert "consectetuer adipiscing elit" in text_content, text_content[:500]

        # v2 JSON output contains structured pages + textItems with bounding boxes.
        json_payload = json.loads((output_dir / "output.pdf.json").read_text())
        assert isinstance(json_payload, dict) and "pages" in json_payload, json_payload
        assert len(json_payload["pages"]) >= 1
        first_page = json_payload["pages"][0]
        assert "textItems" in first_page, first_page
        assert any("x" in item and "y" in item for item in first_page["textItems"])


def test_extract_multiple_pdfs():
    """All PDFs across pdf/ + responses/ produce their own per-source files.

    The two PDFs land at different paths; each must produce its own
    ``output.txt`` / ``document.txt`` and they must contain the source-
    specific text (which has no overlap between the two fixtures).
    """
    pdf_a = _download_pdf(PDF_URL_A)
    pdf_b = _download_pdf(PDF_URL_B)

    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"

        pdf_dir = snap_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (pdf_dir / "output.pdf").write_bytes(pdf_a)

        responses_dir = snap_dir / "responses" / "application" / "example.com"
        responses_dir.mkdir(parents=True, exist_ok=True)
        (responses_dir / "document.pdf").write_bytes(pdf_b)

        result = _run_hook(snap_dir, "https://example.com/docs")
        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", record

        output_dir = snap_dir / "liteparse"
        stems = _liteparse_output_stems(snap_dir)
        assert "output.pdf" in stems, stems
        assert "document.pdf" in stems, stems

        # Unique text from PDF_URL_A (w3.org dummy.pdf):
        output_text = (output_dir / "output.pdf.txt").read_text(errors="ignore").lower()
        assert "dummy pdf file" in output_text, output_text[:500]

        # Unique text from PDF_URL_B (pdfobject.com sample.pdf):
        document_text = (
            (output_dir / "document.pdf.txt").read_text(errors="ignore").lower()
        )
        assert "this is a simple pdf file" in document_text, document_text[:500]
        assert "consectetuer adipiscing elit" in document_text, document_text[:500]


def test_extract_scanned_pdf_via_ocr():
    """End-to-end OCR on a real scanned PDF (no native text layer).

    Uses the fscrawler test-ocr.pdf — a 2-page raster PDF. lit v2 must run
    Tesseract to recover the text. Asserts text from both pages appears.
    Relies on abxpkg auto-installing tesseract + the hook auto-discovering
    its tessdata path; no manual LITEPARSE_TESSDATA_DIR is passed.
    """
    pdf_content = _download_pdf(PDF_URL_OCR)
    require_tessdata_dir()

    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"
        pdf_dir = snap_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (pdf_dir / "scanned.pdf").write_bytes(pdf_content)

        result = _run_hook(snap_dir, PDF_URL_OCR)
        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", (
            f"OCR extraction should succeed: {record}. stderr: {result.stderr}"
        )

        text_content = (
            (snap_dir / "liteparse" / "scanned.pdf.txt")
            .read_text(errors="ignore")
            .lower()
        )
        assert "this file contains some words" in text_content, text_content[:500]
        assert "second part of the text is in page 2" in text_content, text_content[
            :500
        ]


def test_extract_image_via_ocr():
    """End-to-end OCR on a real PNG image (lit v2 supports raster image input).

    Uses the classic Tesseract eurotext.png test image and asserts a stable
    substring from its English line. Image is placed under responses/ to also
    cover that source-discovery path.
    """
    img_content = _download_png(IMAGE_URL_OCR)
    require_tessdata_dir()

    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"
        responses_dir = snap_dir / "responses" / "image" / "tesseract-ocr.github.io"
        responses_dir.mkdir(parents=True, exist_ok=True)
        (responses_dir / "eurotext.png").write_bytes(img_content)

        result = _run_hook(snap_dir, IMAGE_URL_OCR)
        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", (
            f"Image OCR should succeed: {record}. stderr: {result.stderr}"
        )

        text_content = (
            (snap_dir / "liteparse" / "eurotext.png.txt")
            .read_text(errors="ignore")
            .lower()
        )
        # eurotext.png English line: "The (quick) [brown] {fox} jumps!"
        assert (
            "quick" in text_content
            and "brown" in text_content
            and "fox" in text_content
        ), f"Expected 'quick brown fox' OCR output. Got: {text_content[:500]!r}"


def test_ocr_disabled_flag_passed():
    """LITEPARSE_OCR_ENABLED=False passes --no-ocr through to lit.

    Verifies the flag wiring by parsing the eurotext.png image — which has
    NO native text layer, so the only way to recover any English text is via
    OCR. With OCR disabled the recovered text must not contain the image's
    distinctive English phrase 'quick brown fox'.
    """
    img_content = _download_png(IMAGE_URL_OCR)
    # Ensure tesseract is installed so the hook reaches the --no-ocr path
    # instead of failing for missing tessdata; we want to prove --no-ocr is
    # what suppresses OCR, not lack of trained data.
    require_tessdata_dir()

    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"
        responses_dir = snap_dir / "responses" / "image" / "tesseract-ocr.github.io"
        responses_dir.mkdir(parents=True, exist_ok=True)
        (responses_dir / "eurotext.png").write_bytes(img_content)

        result = _run_hook(
            snap_dir,
            IMAGE_URL_OCR,
            extra_env={
                "LITEPARSE_OCR_ENABLED": "False",
            },
        )

        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record, result.stdout
        assert record["status"] in ("succeeded", "noresults"), record

        # With --no-ocr the image cannot be read, so any per-source text
        # output must not contain the image's distinctive English phrase.
        # Empty/missing per-source files are also acceptable.
        text_content = _read_all_liteparse_text(snap_dir)
        assert "quick" not in text_content, (
            f"OCR should be disabled but image was OCR'd: {text_content[:300]!r}"
        )
        assert "brown" not in text_content, (
            f"OCR should be disabled but image was OCR'd: {text_content[:300]!r}"
        )


def test_min_image_dimension_skips_thumbnails():
    """LITEPARSE_MIN_IMAGE_DIMENSION filters out tiny images.

    Drop a hand-crafted 16x16 PNG next to the real eurotext.png and assert:
      - no per-source ``favicon.png.txt`` is written (it was filtered)
      - ``eurotext.png.txt`` still exists with the OCR'd text
    """
    img_content = _download_png(IMAGE_URL_OCR)
    require_tessdata_dir()
    tiny_png = _make_png(16, 16)

    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"
        responses_dir = snap_dir / "responses" / "image" / "example.com"
        responses_dir.mkdir(parents=True, exist_ok=True)
        (responses_dir / "favicon.png").write_bytes(tiny_png)
        (responses_dir / "eurotext.png").write_bytes(img_content)

        result = _run_hook(
            snap_dir,
            IMAGE_URL_OCR,
            extra_env={
                "LITEPARSE_MIN_IMAGE_DIMENSION": "128",
            },
        )
        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", record

        lit_dir = snap_dir / "liteparse"
        assert not (lit_dir / "favicon.png.txt").exists(), (
            f"Tiny favicon.png should be filtered by min-dim=128. Files: {list(lit_dir.iterdir())}"
        )
        eurotext_text = (
            (lit_dir / "eurotext.png.txt").read_text(errors="ignore").lower()
        )
        assert "quick" in eurotext_text and "brown" in eurotext_text, (
            f"Expected OCR text from large image, got: {eurotext_text[:300]!r}"
        )


def test_min_image_dimension_zero_disables_filter():
    """LITEPARSE_MIN_IMAGE_DIMENSION=0 keeps tiny images in the source set.

    With filtering disabled the tiny PNG is fed to lit. lit may produce an
    empty extraction (which means no per-source file is written), but the
    stderr log must show it was *processed* — that's how we know the
    dimension filter was bypassed, not the file silently skipped.
    """
    require_tessdata_dir()
    tiny_png = _make_png(16, 16)

    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"
        responses_dir = snap_dir / "responses" / "image" / "example.com"
        responses_dir.mkdir(parents=True, exist_ok=True)
        (responses_dir / "favicon.png").write_bytes(tiny_png)

        result = _run_hook(
            snap_dir,
            "https://example.com/favicon.png",
            extra_env={
                "LITEPARSE_MIN_IMAGE_DIMENSION": "0",
            },
        )
        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record, result.stdout
        # Stderr says "Processing 1 document(s)" — proves favicon made it
        # past the dim filter (with min-dim=128 it would say "0 documents").
        assert "Processing 1 document" in result.stderr, result.stderr


def test_extract_pdf_from_wget_output():
    """Documents under wget/<host>/... are auto-discovered.

    Place a real PDF in the wget plugin's output layout and verify liteparse
    picks it up and writes ``sample.pdf.txt`` with the deterministic text.
    """
    pdf_content = _download_pdf(PDF_URL_B)

    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"
        wget_dir = snap_dir / "wget" / "pdfobject.com" / "pdf"
        wget_dir.mkdir(parents=True, exist_ok=True)
        (wget_dir / "sample.pdf").write_bytes(pdf_content)

        result = _run_hook(snap_dir, PDF_URL_B)
        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", record

        text_content = (
            (snap_dir / "liteparse" / "sample.pdf.txt")
            .read_text(errors="ignore")
            .lower()
        )
        assert "this is a simple pdf file" in text_content, text_content[:500]
        assert "consectetuer adipiscing elit" in text_content, text_content[:500]


def test_tesseract_auto_installs_via_abxpkg():
    """abxpkg installs the tesseract system package end-to-end.

    Drives the same Binary().install() chain that the crawl preflight uses
    when the plugin's ``required_binaries`` runs. Asserts:
      - we got an absolute path to a real ``tesseract`` binary
      - it reports >=5.0.0
      - it ships eng.traineddata in a discoverable system path
      - that path matches what the hook's resolve_tessdata_dir() returns

    Hard-fails if any step doesn't work — the plugin must self-bootstrap
    on Linux (apt) and macOS (brew) without manual setup.
    """
    binary_path = install_tesseract_binary()
    assert Path(binary_path).is_file(), (
        f"tesseract binary path must exist after install(): {binary_path}"
    )

    version_proc = subprocess.run(
        [binary_path, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert version_proc.returncode == 0, version_proc.stderr
    version_line = (version_proc.stdout + version_proc.stderr).splitlines()[0]
    major = int(version_line.split()[1].split(".")[0])
    assert major >= 5, f"Expected tesseract >=5.0.0, got: {version_line!r}"

    tessdata = discover_tessdata_dir(binary_path)
    assert (Path(tessdata) / "eng.traineddata").is_file(), tessdata
    assert Path(tessdata, "eng.traineddata").stat().st_size > 1_000_000, (
        "eng.traineddata is suspiciously small — install probably incomplete"
    )

    # Hook's resolver, invoked via the same code path the script uses at
    # runtime, should land on the same directory.
    import importlib.util as _import_util

    _spec = _import_util.spec_from_file_location("_lh", LITEPARSE_HOOK)
    assert _spec and _spec.loader
    _module = _import_util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    hook_tessdata = _module.resolve_tessdata_dir("", binary_path, "eng")
    assert hook_tessdata == tessdata, (
        f"Hook resolved a different tessdata path than the test fixture. "
        f"hook={hook_tessdata!r} fixture={tessdata!r}"
    )


def test_extract_image_embedded_in_responses_directory():
    """Real images saved by the responses/ plugin tree are auto-OCR'd.

    Mimics the production layout where the responses extractor mirrors
    downloaded media under ``responses/<mime-bucket>/<host>/...``. Drops
    eurotext.png at that depth and asserts the hook discovers + OCRs it.
    """
    img_content = _download_png(IMAGE_URL_OCR)
    require_tessdata_dir()

    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"
        nested = (
            snap_dir
            / "responses"
            / "image"
            / "tesseract-ocr.github.io"
            / "tessdoc"
            / "images"
        )
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "eurotext.png").write_bytes(img_content)

        result = _run_hook(snap_dir, IMAGE_URL_OCR)
        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", record

        text_content = (
            (snap_dir / "liteparse" / "eurotext.png.txt")
            .read_text(errors="ignore")
            .lower()
        )
        assert (
            "quick" in text_content
            and "brown" in text_content
            and "fox" in text_content
        ), f"Expected OCR text from nested responses image: {text_content[:300]!r}"


def test_warns_but_succeeds_when_ocr_misconfigured_with_native_text_available():
    """Missing tessdata for the requested language warns but doesn't kill the snapshot.

    With ``LITEPARSE_OCR_LANGUAGE=xzz`` (no host has xzz.traineddata) and a
    native-text PDF in the sources set, the hook must:
      - emit a stderr WARN mentioning tessdata + the requested language
      - still extract the PDF's native text via PDFium
      - return status=succeeded — degraded extraction is more useful than
        a failed snapshot for users on minimal envs.

    Tests stay strict; the hook degrades gracefully.
    """
    pdf_content = _download_pdf(PDF_URL_B)
    require_tessdata_dir()

    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"
        pdf_dir = snap_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        (pdf_dir / "output.pdf").write_bytes(pdf_content)

        result = _run_hook(
            snap_dir,
            PDF_URL_B,
            extra_env={"LITEPARSE_OCR_LANGUAGE": "xzz"},
        )

        assert result.returncode == 0, (
            f"Hook should still succeed when native-text PDFs are available "
            f"even if OCR is misconfigured. stderr={result.stderr!r}"
        )
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", record

        # Stderr must surface the misconfiguration so users notice.
        assert "tessdata" in result.stderr.lower(), result.stderr
        assert "xzz" in result.stderr, result.stderr
        assert "warn" in result.stderr.lower(), result.stderr

        # Native-text extraction must still have produced real PDF body text.
        text_content = (
            (snap_dir / "liteparse" / "output.pdf.txt")
            .read_text(errors="ignore")
            .lower()
        )
        assert "consectetuer adipiscing elit" in text_content, text_content[:500]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

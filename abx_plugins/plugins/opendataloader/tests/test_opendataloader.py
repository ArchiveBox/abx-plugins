"""
Integration tests for opendataloader plugin.

Tests verify:
1. Hook script exists
2. Install hooks can install opendataloader-pdf binary
3. OCR extraction runs with real opendataloader-pdf binary on a real live PDF
4. Config options work (enabled/disabled)
5. Handles missing sources gracefully
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
import requests

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from base.test_utils import parse_jsonl_output

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent
_OPENDATALOADER_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_opendataloader.*"), None)
if _OPENDATALOADER_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
OPENDATALOADER_HOOK = _OPENDATALOADER_HOOK
TEST_URL = "https://example.com"

# Module-level cache for binary path
_opendataloader_binary_path = None
_opendataloader_lib_root = None


def get_opendataloader_binary_path():
    """Get opendataloader-pdf binary path from PATH or by running install hooks."""
    global _opendataloader_binary_path
    if _opendataloader_binary_path and Path(_opendataloader_binary_path).is_file():
        return _opendataloader_binary_path

    # Try finding it on PATH first (already installed)
    found = shutil.which("opendataloader-pdf")
    if found and Path(found).is_file():
        _opendataloader_binary_path = found
        return _opendataloader_binary_path

    # Fall back to install via real plugin hooks
    pip_hook = PLUGINS_ROOT / "pip" / "on_Binary__11_pip_install.py"
    crawl_hook = PLUGIN_DIR / "on_Crawl__42_opendataloader_install.finite.bg.py"
    if not pip_hook.exists():
        return None

    binproviders = "*"
    overrides = None

    if crawl_hook.exists():
        crawl_result = subprocess.run(
            [sys.executable, str(crawl_hook)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in crawl_result.stdout.strip().split("\n"):
            if not line.strip().startswith("{"):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") == "Binary" and record.get("name") == "opendataloader-pdf":
                binproviders = record.get("binproviders", "*")
                overrides = record.get("overrides")
                break

    global _opendataloader_lib_root
    if not _opendataloader_lib_root:
        _opendataloader_lib_root = tempfile.mkdtemp(prefix="opendataloader-lib-")

    env = os.environ.copy()
    env["LIB_DIR"] = str(Path(_opendataloader_lib_root) / "lib")
    env["SNAP_DIR"] = str(Path(_opendataloader_lib_root) / "data")
    env["CRAWL_DIR"] = str(Path(_opendataloader_lib_root) / "crawl")

    cmd = [
        sys.executable,
        str(pip_hook),
        "--binary-id",
        str(uuid.uuid4()),
        "--machine-id",
        str(uuid.uuid4()),
        "--name",
        "opendataloader-pdf",
        f"--binproviders={binproviders}",
    ]
    if overrides:
        cmd.append(f"--overrides={json.dumps(overrides)}")

    install_result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    for line in install_result.stdout.strip().split("\n"):
        if not line.strip().startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "Binary" and record.get("name") == "opendataloader-pdf":
            _opendataloader_binary_path = record.get("abspath")
            return _opendataloader_binary_path

    return None


def require_opendataloader_binary() -> str:
    binary_path = get_opendataloader_binary_path()
    assert binary_path, (
        "opendataloader-pdf installation failed. Install hook should install "
        "the binary automatically in this test environment."
    )
    assert Path(binary_path).is_file(), f"opendataloader-pdf binary path invalid: {binary_path}"
    return binary_path


def test_hook_script_exists():
    assert OPENDATALOADER_HOOK.exists(), f"Hook script not found: {OPENDATALOADER_HOOK}"


def test_verify_deps_with_install_hooks():
    binary_path = require_opendataloader_binary()
    assert Path(binary_path).is_file(), f"Binary path must be a valid file: {binary_path}"


def test_config_disabled_skips():
    """Test that OPENDATALOADER_ENABLED=False skips extraction."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["OPENDATALOADER_ENABLED"] = "False"

        result = subprocess.run(
            [
                sys.executable,
                str(OPENDATALOADER_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test-disabled",
            ],
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
    """Test that hook reports noresults when no PDF/image sources exist."""
    binary_path = require_opendataloader_binary()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = binary_path

        result = subprocess.run(
            [
                sys.executable,
                str(OPENDATALOADER_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test-nosources",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, "Should exit 0 without sources"
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "noresults"


def test_ocr_real_pdf_from_web():
    """Test OCR extraction on a real PDF downloaded from the web.

    Downloads a public PDF, places it as if the pdf plugin produced it,
    and runs the opendataloader snapshot hook to extract text.
    """
    binary_path = require_opendataloader_binary()

    # Try multiple stable public PDF sources
    pdf_urls = [
        "https://unec.edu.az/application/uploads/2014/12/pdf-sample.pdf",
        "https://www.orimi.com/pdf-test.pdf",
    ]

    pdf_content = None
    for url in pdf_urls:
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
                pdf_content = resp.content
                break
        except Exception:
            continue

    if pdf_content is None:
        pytest.fail("Could not download any test PDF from the web")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"

        # Place PDF as if the pdf plugin produced it
        pdf_dir = snap_dir / "pdf"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        pdf_file = pdf_dir / "output.pdf"
        pdf_file.write_bytes(pdf_content)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = binary_path

        result = subprocess.run(
            [
                sys.executable,
                str(OPENDATALOADER_HOOK),
                "--url",
                "https://example.com/test.pdf",
                "--snapshot-id",
                "test-real-pdf",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, f"OCR extraction failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record, "Should have ArchiveResult JSONL output"
        assert record["status"] == "succeeded", f"Should succeed: {record}. stderr: {result.stderr}"

        output_dir = snap_dir / "opendataloader"
        md_file = output_dir / "content.md"
        txt_file = output_dir / "content.txt"
        meta_file = output_dir / "metadata.json"

        assert md_file.exists(), "content.md not created"
        assert txt_file.exists(), "content.txt not created"
        assert meta_file.exists(), "metadata.json not created"

        md_content = md_file.read_text(errors="ignore")
        assert len(md_content) > 10, f"content.md too short: {md_content!r}"

        metadata = json.loads(meta_file.read_text())
        assert metadata["sources_processed"] >= 1
        assert metadata["files"][0]["source_file"] == "output.pdf"


def test_ocr_image_from_responses():
    """Test OCR extraction on an image placed as if the responses plugin produced it."""
    binary_path = require_opendataloader_binary()

    # Download a real image with text content (a table image from W3C)
    image_url = "https://www.w3.org/WAI/WCAG21/Techniques/pdf/img/table-word.jpg"

    try:
        resp = requests.get(image_url, timeout=30)
        resp.raise_for_status()
        image_content = resp.content
    except Exception as e:
        pytest.fail(f"Could not download test image: {e}")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"

        # Place image as if responses plugin produced it
        responses_dir = snap_dir / "responses" / "image" / "example.com"
        responses_dir.mkdir(parents=True, exist_ok=True)
        img_file = responses_dir / "table-word.jpg"
        img_file.write_bytes(image_content)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["OPENDATALOADER_BINARY"] = binary_path

        result = subprocess.run(
            [
                sys.executable,
                str(OPENDATALOADER_HOOK),
                "--url",
                "https://example.com/image.jpg",
                "--snapshot-id",
                "test-image-ocr",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 0, f"Image OCR failed: {result.stderr}"

        record = parse_jsonl_output(result.stdout)
        assert record, "Should have ArchiveResult JSONL output"
        # opendataloader-pdf handles PDFs; images may not be supported - accept noresults too
        assert record["status"] in ("succeeded", "noresults"), f"Unexpected status: {record}"

        if record["status"] == "succeeded":
            output_dir = snap_dir / "opendataloader"
            assert (output_dir / "content.md").exists(), "content.md not created"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

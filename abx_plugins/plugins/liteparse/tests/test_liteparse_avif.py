"""Real OCR of AVIF downloads, including misleading URL extensions."""

import hashlib
from pathlib import Path

import pytest

from abx_plugins.plugins.liteparse.tests.test_liteparse import (
    IMAGE_URL_OCR,
    _download_png,
    _run_hook,
    parse_jsonl_output,
)

AVIF_FIXTURE = Path(__file__).with_name("fixtures") / "ocr.avif"


@pytest.mark.parametrize("suffix", [".jpg", ".avif"])
def test_avif_image_ocr_preserves_original_bytes(tmp_path, suffix):
    # ImageMagick 6 can silently retain PNG encoding for an unknown output
    # extension. Use actual AVIF bytes, regardless of the host's encoder list.
    avif = AVIF_FIXTURE.read_bytes()
    assert b"ftypavif" in avif[:32]
    snapshot = tmp_path / "snapshot"
    source = snapshot / "responses" / f"eurotext{suffix}"
    source.parent.mkdir(parents=True)
    source.write_bytes(avif)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    result = _run_hook(snapshot, IMAGE_URL_OCR)
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "succeeded", (record, result.stderr)
    text = (snapshot / "liteparse" / f"eurotext{suffix}.txt").read_text().lower()
    assert all(word in text for word in ("quick", "brown", "fox")), text
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest


def test_invalid_avif_reports_failure_and_retains_other_ocr(tmp_path):
    original = tmp_path / "eurotext.png"
    original.write_bytes(_download_png(IMAGE_URL_OCR))
    snapshot = tmp_path / "snapshot"
    sources = snapshot / "responses"
    sources.mkdir(parents=True)
    (sources / "valid.png").write_bytes(original.read_bytes())
    # A genuinely truncated download must fail, while the other document's
    # completed OCR survives the batch error.
    truncated = AVIF_FIXTURE.read_bytes()[:32]
    assert b"ftypavif" in truncated
    (sources / "truncated.avif").write_bytes(truncated)
    result = _run_hook(snapshot, IMAGE_URL_OCR)
    assert result.returncode == 1, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "failed", result.stdout
    assert "AVIF conversion failed" in result.stderr
    text = (snapshot / "liteparse" / "valid.png.txt").read_text().lower()
    assert all(word in text for word in ("quick", "brown", "fox")), text
    assert (sources / "truncated.avif").read_bytes() == truncated

"""Real OCR of AVIF downloads, including misleading URL extensions."""

import hashlib
import subprocess

import pytest

from abx_plugins.plugins.liteparse.tests.test_liteparse import (
    IMAGE_URL_OCR,
    _download_png,
    _run_hook,
    install_imagemagick_binary,
    parse_jsonl_output,
)


@pytest.mark.parametrize("suffix", [".jpg", ".avif"])
def test_avif_image_ocr_preserves_original_bytes(tmp_path, suffix):
    original = tmp_path / "eurotext.png"
    original.write_bytes(_download_png(IMAGE_URL_OCR))
    avif = tmp_path / "eurotext.avif"
    converted = subprocess.run(
        [install_imagemagick_binary(), str(original), "-quality", "95", str(avif)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert converted.returncode == 0, converted.stderr
    assert b"ftypavif" in avif.read_bytes()[:32]
    snapshot = tmp_path / "snapshot"
    source = snapshot / "responses" / f"eurotext{suffix}"
    source.parent.mkdir(parents=True)
    source.write_bytes(avif.read_bytes())
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
    avif = tmp_path / "eurotext.avif"
    converted = subprocess.run(
        [install_imagemagick_binary(), str(original), str(avif)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert converted.returncode == 0, converted.stderr
    snapshot = tmp_path / "snapshot"
    sources = snapshot / "responses"
    sources.mkdir(parents=True)
    (sources / "valid.png").write_bytes(original.read_bytes())
    # A genuinely truncated download must fail, while the other document's
    # completed OCR survives the batch error.
    truncated = avif.read_bytes()[:32]
    (sources / "truncated.avif").write_bytes(truncated)
    result = _run_hook(snapshot, IMAGE_URL_OCR)
    assert result.returncode == 1, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "failed", result.stdout
    assert "AVIF conversion failed" in result.stderr
    text = (snapshot / "liteparse" / "valid.png.txt").read_text().lower()
    assert all(word in text for word in ("quick", "brown", "fox")), text
    assert (sources / "truncated.avif").read_bytes() == truncated

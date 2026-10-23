"""Real JPEG XL decoding, including misleading source extensions."""

from pathlib import Path

import pytest

from abx_plugins.plugins.liteparse.tests.test_liteparse import (
    IMAGE_URL_OCR,
    PLUGIN_DIR,
    _download_png,
    _run_hook,
    parse_jsonl_output,
    install_required_binary_from_config,
)

FIXTURES = Path(__file__).with_name("fixtures")


@pytest.fixture(scope="module")
def djxl_binary():
    binary = install_required_binary_from_config(PLUGIN_DIR, "djxl")
    assert binary and binary.abspath, "JPEG XL decoder dependency resolution failed"
    return str(binary.abspath)


@pytest.mark.parametrize("suffix", [".jpg", ".jxl"])
@pytest.mark.parametrize("fixture", ["ocr.jxl", "ocr-container.jxl"])
def test_jxl_image_ocr_preserves_original_bytes(tmp_path, suffix, fixture, djxl_binary):
    original = (FIXTURES / fixture).read_bytes()
    assert original.startswith((b"\xff\x0a", b"\x00\x00\x00\x0cJXL \r\n\x87\n"))
    snapshot = tmp_path / "snapshot"
    source = snapshot / "responses" / f"eurotext{suffix}"
    source.parent.mkdir(parents=True)
    source.write_bytes(original)
    (source.parent / "reference.png").write_bytes((FIXTURES / "ocr.png").read_bytes())
    result = _run_hook(snapshot, IMAGE_URL_OCR, {"LITEPARSE_DJXL_BINARY": djxl_binary})
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "succeeded", (record, result.stderr)
    text = (snapshot / "liteparse" / f"eurotext{suffix}.txt").read_text()
    assert all(word in text.lower() for word in ("quick", "brown", "fox")), text
    reference = (snapshot / "liteparse" / "reference.png.txt").read_text()
    assert text.partition("\n")[2] == reference.partition("\n")[2]
    assert source.read_bytes() == original


@pytest.mark.parametrize("kind", ["truncated", "animated"])
def test_unreadable_or_animated_jxl_retains_other_ocr(tmp_path, kind, djxl_binary):
    snapshot = tmp_path / "snapshot"
    sources = snapshot / "responses"
    sources.mkdir(parents=True)
    (sources / "valid.png").write_bytes(_download_png(IMAGE_URL_OCR))
    original = (
        FIXTURES / ("animated.jxl" if kind == "animated" else "ocr.jxl")
    ).read_bytes()
    if kind == "truncated":
        original = original[:32]
    source = sources / f"{kind}.jxl"
    source.write_bytes(original)
    result = _run_hook(snapshot, IMAGE_URL_OCR, {"LITEPARSE_DJXL_BINARY": djxl_binary})
    assert result.returncode == 1, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "failed", result.stdout
    assert "JPEG XL conversion failed" in result.stderr
    text = (snapshot / "liteparse" / "valid.png.txt").read_text().lower()
    assert all(word in text for word in ("quick", "brown", "fox")), text
    assert not (snapshot / "liteparse" / f"{kind}.jxl.txt").exists()
    assert source.read_bytes() == original

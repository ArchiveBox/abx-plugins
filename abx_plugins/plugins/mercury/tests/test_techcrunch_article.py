"""Preserve the requested TechCrunch story when the saved DOM has more stories."""

import gzip
import json
import os
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import install_required_binary_from_config

PLUGIN = Path(__file__).resolve().parents[1]
URL = (
    "https://techcrunch.com/2026/06/30/the-father-of-the-internet-is-finally-retiring/"
)
PATH = "/2026/06/30/the-father-of-the-internet-is-finally-retiring/"


@pytest.fixture(scope="module")
def mercury_binary():
    binary = install_required_binary_from_config(PLUGIN, "mercury-parser")
    assert binary and binary.abspath
    return str(binary.abspath)


def extract(snapshot, source, binary):
    (snapshot / "dom").mkdir(parents=True)
    saved = snapshot / "dom" / "output.html"
    saved.write_bytes(source)
    result = subprocess.run(
        [str(PLUGIN / "on_Snapshot__66_mercury.py"), "--url", URL],
        cwd=snapshot,
        env={**os.environ, "SNAP_DIR": str(snapshot), "MERCURY_BINARY": binary},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert '"status": "succeeded"' in result.stdout, result.stdout
    assert saved.read_bytes() == source
    return (
        (snapshot / "mercury" / "content.txt").read_text(),
        json.loads((snapshot / "mercury" / "article.json").read_text()),
    )


def fixture(name):
    return gzip.decompress(
        (
            Path(__file__).parent
            / "fixtures"
            / f"techcrunch-infinite-scroll-{name}.html.gz"
        ).read_bytes(),
    )


def test_infinite_scroll_retains_complete_original_article(tmp_path, mercury_binary):
    before, before_metadata = extract(
        tmp_path / "before",
        fixture("before"),
        mercury_binary,
    )
    assert "Vinton Cerf will step down" in before
    assert "This story has been updated with comment from Google." in before
    after, after_metadata = extract(
        tmp_path / "after",
        fixture("after"),
        mercury_binary,
    )
    assert after == before
    for key in ("title", "author", "date_published", "lead_image_url"):
        assert after_metadata[key] == before_metadata[key]
    assert after_metadata["author"] == "Tim Fernholz"
    assert "Father of the Internet" in after_metadata["title"]
    assert "Companies are giving employees AI tools" not in after
    assert "Walmart" not in after
    assert "1Password" not in after
    assert "Spotify" not in after


@pytest.mark.parametrize(
    "case",
    ["missing", "empty", "mismatched", "ambiguous", "invalid"],
)
def test_unproven_article_identity_keeps_native_selection(
    tmp_path,
    mercury_binary,
    case,
):
    original = fixture("after")
    marker = f'data-url-observer="{PATH}"'.encode()
    assert original.count(marker) == 1
    if case == "missing":
        source = original.replace(marker, b"")
    elif case == "empty":
        source = original.replace(marker, b'data-url-observer=""')
    elif case == "mismatched":
        source = original.replace(marker, b'data-url-observer="/different-article/"')
    elif case == "invalid":
        source = original.replace(marker, b'data-url-observer="http://["')
    else:
        second_main = original.index(b"<main ", original.index(b"<main ") + 1)
        source = original[:second_main] + original[second_main:].replace(
            b"<main ",
            b'<main data-url-observer="/different-article/" ',
            1,
        )
    native_source = source.replace(
        b"data-url-observer=",
        b"data-original-url-observer=",
    )
    native, native_metadata = extract(
        tmp_path / "native",
        native_source,
        mercury_binary,
    )
    actual, actual_metadata = extract(tmp_path / "actual", source, mercury_binary)
    assert actual == native
    assert actual_metadata == native_metadata

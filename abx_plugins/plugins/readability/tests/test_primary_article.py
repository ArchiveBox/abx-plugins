"""Real Readability extraction must retain the captured primary article."""

import os
import gzip
import json
import re
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import install_required_binary_from_config


PLUGIN = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "qz-infinite-articles.html"
URL = "https://qz.com/1307380/yoga-and-meditation-boost-your-ego-say-psychology-researchers/"


@pytest.mark.parametrize("primary_in_article", [False, True])
def test_primary_article_survives_longer_infinite_scroll_story(
    tmp_path,
    primary_in_article,
):
    installed = install_required_binary_from_config(PLUGIN, "readability-extractor")
    assert installed and installed.abspath
    original = FIXTURE.read_text().replace("<main>", "<main>\r\n\r")
    if primary_in_article:
        original = original.replace("<article>", "</article><article>", 1).replace(
            "<main>",
            "<main><article>",
            1,
        )
    source = tmp_path / "dom" / "output.html"
    source.parent.mkdir()
    source.write_text(original)
    result = subprocess.run(
        [str(PLUGIN / "on_Snapshot__66_readability.py"), "--url", URL],
        cwd=tmp_path,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "READABILITY_BINARY": str(installed.abspath),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "readability" / "content.txt").read_text()
    assert "According to Buddhist teaching" in text
    assert (
        "does not invalidate thousands of years of Buddhist teaching and practice"
        in text
    )
    assert "data center" not in text.lower()
    assert source.read_bytes() == original.encode("utf-8")


@pytest.mark.parametrize(
    "case",
    [
        "missing_metadata",
        "conflicting_metadata",
        "multiple_h1",
        "unheaded_article",
        "nested_article",
        "null_type",
        "noniterable_type",
        "nested_type",
        "invalid_jsonld",
        "truncated_article",
    ],
)
def test_ambiguous_or_continuing_articles_keep_native_selection(tmp_path, case):
    """Do not discard content without an authoritative primary article boundary."""
    import json
    import re

    installed = install_required_binary_from_config(PLUGIN, "readability-extractor")
    assert installed and installed.abspath
    original = FIXTURE.read_text()
    if case == "missing_metadata":
        original = re.sub(
            r'<script type="application/ld\+json">.*?</script>',
            "",
            original,
        )
    elif case == "conflicting_metadata":
        original = original.replace(
            '"headline":"People’s egos',
            '"headline":"Different people’s egos',
            1,
        )
    elif case == "multiple_h1":
        original = original.replace("</main>", "<h1>A different article</h1></main>")
    elif case == "unheaded_article":
        original = re.sub(r"<h2\b[^>]*>.*?</h2>", "", original)
    elif case == "nested_article":
        original = original.replace("<main>", "<main><article>").replace(
            "</main>",
            "</article></main>",
        )
    elif case in {"null_type", "noniterable_type", "nested_type"}:
        value = {"null_type": "null", "noniterable_type": "7", "nested_type": "[{}]"}[
            case
        ]
        original = original.replace('"@type":"NewsArticle"', '"@type":' + value, 1)
    elif case == "invalid_jsonld":
        original = original.replace('{"@context":', '{invalid JSON "@context":', 1)
    elif case == "truncated_article":
        original = original.replace("</article>", "", 1)
    source = tmp_path / "dom" / "output.html"
    source.parent.mkdir()
    source.write_text(original)
    native = subprocess.run(
        [str(installed.abspath), str(source), URL, "utf-8"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert native.returncode == 0, native.stderr
    native_text = json.loads(native.stdout)["textContent"]
    result = subprocess.run(
        [str(PLUGIN / "on_Snapshot__66_readability.py"), "--url", URL],
        cwd=tmp_path,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "READABILITY_BINARY": str(installed.abspath),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "readability" / "content.txt").read_text() == native_text
    assert source.read_bytes() == original.encode("utf-8")


@pytest.mark.parametrize("metadata", ["unique", "unmatched", "ambiguous"])
def test_metadata_selects_one_of_multiple_captured_article_headings(tmp_path, metadata):
    installed = install_required_binary_from_config(PLUGIN, "readability-extractor")
    assert installed and installed.abspath
    original = gzip.decompress(
        (FIXTURE.parent / "multiple-article-headings.html.gz").read_bytes(),
    ).decode("utf-8")
    if metadata != "unique":
        match = re.search(
            r'(<script type="application/ld\+json"[^>]*>)(.*?)(</script>)',
            original,
            re.S,
        )
        assert match
        data = json.loads(match[2])
        if metadata == "unmatched":
            data["@graph"][0]["headline"] = "Unmatched article metadata"
        else:
            data["@graph"].append(
                {
                    "@type": "NewsArticle",
                    "headline": "Ancient shipwreck discovered off Turkey could be world’s oldest",
                },
            )
        original = (
            original[: match.start(2)] + json.dumps(data) + original[match.end(2) :]
        )
    source = tmp_path / "dom" / "output.html"
    source.parent.mkdir()
    source.write_text(original)
    result = subprocess.run(
        [
            str(PLUGIN / "on_Snapshot__66_readability.py"),
            "--url",
            "https://divemagazine.com/scuba-diving-news/"
            "ss-united-states-to-be-sunk-as-worlds-largest-artificial-reef-in-november",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "SNAP_DIR": str(tmp_path),
            "READABILITY_BINARY": str(installed.abspath),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "readability" / "content.txt").read_text()
    if metadata == "unique":
        assert "once the fastest passenger liner in the world" in text
        assert "including arrangements for a public livestream" in text
        assert "4,700 years" not in text
        assert "ancient shipwreck" not in text
    else:
        native = subprocess.run(
            [
                str(installed.abspath),
                str(source),
                "https://divemagazine.com/scuba-diving-news/"
                "ss-united-states-to-be-sunk-as-worlds-largest-artificial-reef-in-november",
                "utf-8",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert native.returncode == 0, native.stderr
        assert text == json.loads(native.stdout)["textContent"]
    assert source.read_bytes() == original.encode("utf-8")

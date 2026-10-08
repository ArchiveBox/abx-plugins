"""Save a genuine public Notion page through the shared Chrome lifecycle."""

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://notion.notion.site/Notion-Cookie-Tables-c38abeb47f8e420a94ade9ac053d90fd"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_notion.js"


@pytest.mark.parametrize(
    "pnpm_converter",
    [False, True],
    ids=["default", "configured-pnpm"],
)
def test_public_page_html_and_markdown(
    tmp_path,
    ensure_chrome_test_prereqs,
    pnpm_converter,
):
    assert HOOK.is_file(), "Notion capture hook is missing"
    with chrome_session(tmp_path, test_url=URL, timeout=60) as (_, _, chrome, env):
        if pnpm_converter:
            # ArchiveBox supplies the actual installed .bin launcher as an
            # explicit binary override, rather than just its command name.
            converter = subprocess.run(
                [
                    "abxpkg",
                    "env",
                    "--install",
                    "--json",
                    "--binproviders=pnpm",
                    f"--deps-from={HOOK.parent.parent / 'defuddle/config.json'}:required_binaries",
                ],
                env={**env, "DEFUDDLE_BINARY": "defuddle"},
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert converter.returncode == 0, converter.stderr
            installed = json.loads(converter.stdout)
            launcher = Path(installed["PNPM_HOME"]) / "defuddle"
            assert launcher.is_file()
            assert ".pnpm/defuddle@0.14.0" in launcher.read_text()
            env = {**env, "DEFUDDLE_BINARY": str(launcher)}
        result = subprocess.run(
            [str(HOOK), f"--url={URL}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "succeeded", result.stdout
        output = chrome.parent / "notion"
        manifest = json.loads((output / "downloads.json").read_text())
        assert manifest["title"] == "Notion Cookie Tables"
        files = {item["format"]: output / item["path"] for item in manifest["files"]}
        assert set(files) == {"html", "md"}
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            assert len(data) == item["size"] > 10000
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
        html = files["html"].read_text()
        markdown = files["md"].read_text()
        for text in (html, markdown):
            assert "Notion Cookie Tables" in text
            assert "Strictly Necessary Cookies" in text
            assert "Marketing" in text
            assert "Authenticates your login." in text
            assert "Cloudflare" in text
            assert "Podscribe" in text
            assert "MNTN Connected TV" in text
            assert (
                "https://docs.aws.amazon.com/elasticloadbalancing/latest/application/sticky-sessions.html"
                in text
            )
        assert "<table" in html
        assert "| Cookie Name |" in markdown
        html_tables = re.findall(r"<table\b.*?</table>", html, re.DOTALL)
        markdown_tables = re.findall(
            r"^\| Cookie Name \|[^\n]+\n\| --- \|[^\n]+\n(?:\|[^\n]+\n)+",
            markdown,
            re.MULTILINE,
        )
        assert len(html_tables) == len(markdown_tables) == 4
        for html_table, markdown_table in zip(
            html_tables,
            markdown_tables,
            strict=True,
        ):
            rows = len(re.findall(r"<tr\b", html_table))
            # Header, separator, and every body row must remain contiguous.
            assert len(markdown_table.splitlines()) == rows + 1
        assert "|\n\n|" not in markdown
        assert "### Strictly Necessary Cookies" in markdown
        assert (
            "](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/sticky-sessions.html)"
            in markdown
        )


def test_unrelated_url_with_browser(tmp_path, ensure_chrome_test_prereqs):
    with chrome_session(tmp_path, test_url="https://example.com", timeout=60) as (
        _,
        _,
        chrome,
        env,
    ):
        result = subprocess.run(
            [str(HOOK), "--url=https://example.com"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record is not None, result.stdout
        assert record["status"] == "noresults", result.stdout
        assert not (chrome.parent / "notion").exists()


def test_product_page_returns_noresults(tmp_path, ensure_chrome_test_prereqs):
    url = "https://www.notion.com/product"
    with chrome_session(tmp_path, test_url=url, timeout=60) as (_, _, chrome, env):
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record is not None, result.stdout
        assert record["status"] == "noresults", result.stdout
        assert not (chrome.parent / "notion").exists()

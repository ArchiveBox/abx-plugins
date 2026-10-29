"""
Tests for the parse_dom_outlinks plugin.

Tests the real DOM outlinks hook with an actual URL to verify
link extraction and categorization.
"""

import json
import lzma
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import get_hook_script, get_plugin_dir
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


# Get the path to the parse_dom_outlinks hook
PLUGIN_DIR = get_plugin_dir(__file__)
OUTLINKS_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_parse_dom_outlinks.*")


def test_expanded_dashboard_preserves_crawlable_links(tmp_path, httpserver):
    # This reduced real-page fixture has 492k DOM elements. The old full-DOM
    # style scan timed out after 30s; anchor extraction finishes without needing
    # the layout of unrelated table cells. Keep the ordinary protocol deadline.
    fixture = Path(__file__).parent / "fixtures" / "expanded-dashboard.html.xz"
    httpserver.expect_request("/table").respond_with_data(
        lzma.decompress(fixture.read_bytes()),
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data("", status=404)
    url = httpserver.url_for("/table")
    with chrome_session(tmp_path, test_url=url, timeout=30) as (_, _, chrome, env):
        start = time.monotonic()
        result = subprocess.run(
            [str(OUTLINKS_HOOK), f"--url={url}", "--depth=2"],
            cwd=chrome,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        print(f"Expanded-dashboard outlinks hook: {time.monotonic() - start:.3f}s")
        assert result.returncode == 0, result.stderr
        records = [
            json.loads(line)
            for line in (Path(env["SNAP_DIR"]) / "parse_dom_outlinks" / "urls.jsonl")
            .read_text()
            .splitlines()
        ]
        assert records == [
            {
                "type": "Snapshot",
                "url": href,
                "plugin": "parse_dom_outlinks",
                "depth": 3,
            }
            for href in (
                "https://lungfish-science.github.io/wastewater-dashboard/wastewater-variant-explorer.html",
                "https://lungfish-science.github.io/wastewater-dashboard/acknowledgements.html",
                url + "#",
                "https://github.com/Lungfish-science/wastewater-dashboard/issues",
                "https://github.com/Lungfish-science/wastewater-dashboard/discussions",
                "https://github.com/Lungfish-science/wastewater-dashboard",
                url + "#scatter-plot-visualization",
                url + "#variant-data-browser",
                url + "#project-metadata-browser",
                "https://www.ncbi.nlm.nih.gov/sra",
                "https://www.ncbi.nlm.nih.gov/nuccore/1798174254",
                "https://cov-spectrum.org/explore/World/AllSamples/Past6M/variants?nextcladePangoLineage=JN.1*&",
                "https://www.ncbi.nlm.nih.gov/",
                "https://lungfish-science.github.io/data/found_meta.tsv.gz",
                "https://github.com/Lungfish-science/wastewater-dashboard/blob/main/LICENSE",
            )
        ]
        result_record = json.loads(result.stdout.strip().splitlines()[-1])
        assert result_record["status"] == "succeeded"
        assert result_record["output_str"] == "15 URLs parsed"


class TestParseDomOutlinksPlugin:
    """Test the parse_dom_outlinks plugin."""

    def test_outlinks_hook_exists(self):
        """DOM outlinks hook script should exist."""
        assert OUTLINKS_HOOK is not None, (
            "DOM outlinks hook not found in plugin directory"
        )
        assert OUTLINKS_HOOK.exists(), f"Hook not found: {OUTLINKS_HOOK}"


class TestParseDomOutlinksWithChrome:
    """Integration tests for parse_dom_outlinks plugin with Chrome."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_outlinks_extracts_links_from_page(self, chrome_test_urls):
        """DOM outlinks hook should extract and categorize links from page."""
        test_url = chrome_test_urls["base_url"]
        snapshot_id = "test-outlinks-snapshot"

        try:
            with chrome_session(
                self.temp_dir,
                crawl_id="test-outlinks-crawl",
                snapshot_id=snapshot_id,
                test_url=test_url,
                navigate=True,
                timeout=30,
            ) as (chrome_process, chrome_pid, snapshot_chrome_dir, env):
                # Use the environment from chrome_session (already has CHROME_HEADLESS=true)

                # Run outlinks hook with the active Chrome session
                result = subprocess.run(
                    [
                        str(OUTLINKS_HOOK),
                        f"--url={test_url}",
                    ],
                    cwd=str(snapshot_chrome_dir),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                )

                # Check for output file
                snap_dir = Path(env["SNAP_DIR"])
                urls_output = snap_dir / "parse_dom_outlinks" / "urls.jsonl"

                # Verify hook ran successfully
                assert result.returncode == 0, f"Hook failed: {result.stderr}"
                assert "Traceback" not in result.stderr

                archive_result = json.loads(result.stdout.strip().splitlines()[-1])
                assert archive_result["type"] == "ArchiveResult"
                assert archive_result["status"] == "succeeded"

                assert urls_output.exists(), "urls.jsonl not created"
                urls_data = [
                    json.loads(line)
                    for line in urls_output.read_text().splitlines()
                    if line.strip()
                ]
                assert urls_data, "urls.jsonl should contain at least one URL"
                assert all(entry["type"] == "Snapshot" for entry in urls_data)
                urls = {entry["url"] for entry in urls_data}
                assert chrome_test_urls["linked_url"] in urls
                assert chrome_test_urls["redirect_url"] in urls
                assert archive_result["output_str"] == f"{len(urls_data)} URLs parsed"

        except RuntimeError:
            raise

    def test_redirected_page_uses_final_url_for_same_page_links(self, httpserver):
        """DOM outlinks must ignore HedgeDoc's stale <base> for same-page anchors."""
        httpserver.expect_request("/").respond_with_data(
            "",
            status=302,
            headers={"Location": "/s/blog"},
        )
        httpserver.expect_request("/s/blog").respond_with_data(
            """
            <!doctype html>
            <html>
              <head>
                <base href="https://docs.monadical.com/">
                <title>Redirected Blog</title>
              </head>
              <body>
                <h1 id="About">About</h1>
                <a href="#About">About</a>
              </body>
            </html>
            """,
            content_type="text/html",
        )
        test_url = httpserver.url_for("/")
        final_url = httpserver.url_for("/s/blog")
        snapshot_id = "test-outlinks-blog-sweeting"

        try:
            with chrome_session(
                self.temp_dir,
                crawl_id="test-outlinks-blog-sweeting-crawl",
                snapshot_id=snapshot_id,
                test_url=test_url,
                navigate=True,
                timeout=60,
            ) as (_chrome_process, _chrome_pid, snapshot_chrome_dir, env):
                result = subprocess.run(
                    [
                        str(OUTLINKS_HOOK),
                        f"--url={test_url}",
                    ],
                    cwd=str(snapshot_chrome_dir),
                    capture_output=True,
                    text=True,
                    timeout=90,
                    env=env,
                )

                assert result.returncode == 0, result.stderr
                snap_dir = Path(env["SNAP_DIR"])
                urls_output = snap_dir / "parse_dom_outlinks" / "urls.jsonl"
                assert urls_output.exists(), "urls.jsonl not created"
                urls = {
                    json.loads(line)["url"]
                    for line in urls_output.read_text().splitlines()
                    if line.strip()
                }

                assert f"{final_url}#About" in urls
                assert "https://docs.monadical.com/#About" not in urls
                assert f"{test_url}#About" not in urls

        except RuntimeError:
            raise

    def test_outlinks_removes_outputs_when_no_crawlable_urls(self, httpserver):
        """Hook should not leave output files behind when no crawlable URLs are found."""
        httpserver.expect_request("/no-links").respond_with_data(
            """<!doctype html>
<html>
<head><title>No Links</title></head>
<body><p>No crawlable links on this page.</p></body>
</html>
""",
            content_type="text/html",
        )
        test_url = httpserver.url_for("/no-links")
        snapshot_id = "test-outlinks-empty"

        try:
            with chrome_session(
                self.temp_dir,
                crawl_id="test-outlinks-empty-crawl",
                snapshot_id=snapshot_id,
                test_url=test_url,
                navigate=True,
                timeout=30,
            ) as (_chrome_process, _chrome_pid, snapshot_chrome_dir, env):
                result = subprocess.run(
                    [
                        str(OUTLINKS_HOOK),
                        f"--url={test_url}",
                    ],
                    cwd=str(snapshot_chrome_dir),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                )

                assert result.returncode == 0, f"Hook failed: {result.stderr}"

                archive_result = json.loads(result.stdout.strip().splitlines()[-1])
                assert archive_result["type"] == "ArchiveResult"
                assert archive_result["status"] == "noresults"
                assert archive_result["output_str"] == "0 URLs parsed"

                snap_dir = Path(env["SNAP_DIR"])
                outlinks_dir = snap_dir / "parse_dom_outlinks"
                assert not (outlinks_dir / "urls.jsonl").exists()

        except RuntimeError:
            raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
Tests for the SEO plugin.

Tests deterministic SEO extraction via local pytest-httpserver fixtures.
"""

import json
import subprocess
import tempfile
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    CHROME_NAVIGATE_HOOK,
    get_plugin_dir,
    get_hook_script,
)


# Get the path to the SEO hook
PLUGIN_DIR = get_plugin_dir(__file__)
SEO_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_seo.*")
CHROME_STARTUP_TIMEOUT_SECONDS = 45


@pytest.fixture
def seo_test_url(httpserver):
    """Serve a deterministic page with known SEO tags."""
    httpserver.expect_request("/seo").respond_with_data(
        """
        <!doctype html>
        <html lang="en">
          <head>
            <meta charset="utf-8" />
            <title>Deterministic SEO Title</title>
            <meta name="description" content="SEO fixture description" />
            <meta name="keywords" content="archivebox,seo,fixture" />
            <meta property="og:title" content="Deterministic OG Title" />
            <meta property="og:description" content="Deterministic OG Description" />
            <meta name="twitter:title" content="Deterministic Twitter Title" />
            <link rel="canonical" href="/canonical-target" />
          </head>
          <body>
            <h1>SEO Fixture</h1>
          </body>
        </html>
        """.strip(),
        content_type="text/html; charset=utf-8",
    )
    return httpserver.url_for("/seo")


class TestSEOPlugin:
    """Test the SEO plugin."""

    def test_seo_hook_exists(self):
        """SEO hook script should exist."""
        assert SEO_HOOK is not None, "SEO hook not found in plugin directory"
        assert SEO_HOOK.exists(), f"Hook not found: {SEO_HOOK}"


class TestSEOWithChrome:
    """Integration tests for SEO plugin with Chrome."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_seo_extracts_meta_tags(self, seo_test_url):
        """SEO hook should extract known meta tags from deterministic fixture."""
        test_url = seo_test_url
        snapshot_id = "test-seo-snapshot"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-seo-crawl",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (chrome_process, chrome_pid, snapshot_chrome_dir, env):
            seo_dir = snapshot_chrome_dir.parent / "seo"
            seo_dir.mkdir(exist_ok=True)

            nav_result = subprocess.run(
                [str(CHROME_NAVIGATE_HOOK),
                    f"--url={test_url}",
                    f"--snapshot-id={snapshot_id}",
                ],
                cwd=str(snapshot_chrome_dir),
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"

            # Run SEO hook with the active Chrome session
            result = subprocess.run(
                [str(SEO_HOOK),
                    f"--url={test_url}",
                    f"--snapshot-id={snapshot_id}",
                ],
                cwd=str(seo_dir),
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )

            # Check for output file
            seo_output = seo_dir / "seo.json"

            # Verify hook ran successfully
            assert result.returncode == 0, f"Hook failed: {result.stderr}"
            assert "Traceback" not in result.stderr
            assert "Error:" not in result.stderr

            assert seo_output.exists(), "No seo.json produced"
            seo_data = json.loads(seo_output.read_text())
            assert seo_data["title"] == "Deterministic SEO Title"
            assert seo_data["description"] == "SEO fixture description"
            assert seo_data["keywords"] == "archivebox,seo,fixture"
            assert seo_data["og:title"] == "Deterministic OG Title"
            assert seo_data["og:description"] == "Deterministic OG Description"
            assert seo_data["twitter:title"] == "Deterministic Twitter Title"
            assert seo_data["canonical"] == "/canonical-target"
            assert seo_data["language"] == "en"
            assert seo_data["url"] == test_url


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

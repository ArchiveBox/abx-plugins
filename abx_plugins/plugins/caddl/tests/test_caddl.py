"""Integration tests for the CAD/3D asset downloader plugin."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

PLUGIN_DIR = Path(__file__).parent.parent
CADDL_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_caddl.*"), None)
CHROME_STARTUP_TIMEOUT_SECONDS = 45


@pytest.fixture
def caddl_test_url(httpserver):
    """Serve direct and embedded 3D assets with deterministic download names."""
    httpserver.expect_request("/").respond_with_data(
        """<!doctype html>
        <html><body>
          <a href="/models/direct.stl">Download STL</a>
          <model-viewer src="/models/viewer.glb"></model-viewer>
        </body></html>""",
        content_type="text/html; charset=utf-8",
    )
    httpserver.expect_request("/models/direct.stl").respond_with_data(
        b"solid direct\nendsolid direct\n",
        content_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="direct.stl"'},
    )
    httpserver.expect_request("/models/viewer.glb").respond_with_data(
        b"glTF\x02\x00\x00\x00",
        content_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="viewer.glb"'},
    )
    return httpserver.url_for("/").replace("localhost", "127.0.0.1", 1)


def test_hook_and_templates_exist():
    """The plugin exposes its hook and host-rendered output templates."""
    assert CADDL_HOOK is not None, "CADDL hook not found"
    assert CADDL_HOOK.exists()
    for template in ("card.html", "full.html", "icon.html"):
        assert (PLUGIN_DIR / "templates" / template).is_file()


def test_disabled_plugin_skips():
    """CADDL_ENABLED=False must not require a Chrome session."""
    with tempfile.TemporaryDirectory() as tmpdir:
        snap_dir = Path(tmpdir) / "snap"
        snap_dir.mkdir()
        result = subprocess.run(
            [str(CADDL_HOOK), "--url=https://example.com"],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=os.environ | {"SNAP_DIR": str(snap_dir), "CADDL_ENABLED": "False"},
            timeout=30,
        )

    assert result.returncode == 0, result.stderr
    record = json.loads(next(line for line in result.stdout.splitlines() if line.startswith("{")))
    assert record["status"] == "skipped"
    assert record["output_str"] == "CADDL_ENABLED=False"


def test_downloads_direct_and_embedded_assets(caddl_test_url):
    """The real Chrome hook downloads extension-matched page assets and manifests them."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with chrome_session(
            Path(tmpdir),
            crawl_id="test-caddl",
            snapshot_id="snap-caddl",
            test_url=caddl_test_url,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (_, _, snapshot_chrome_dir, env):
            output_dir = snapshot_chrome_dir.parent / "caddl"
            output_dir.mkdir()
            env["CADDL_TIMEOUT"] = "15"
            result = subprocess.run(
                [str(CADDL_HOOK), f"--url={caddl_test_url}", "--snapshot-id=snap-caddl"],
                cwd=output_dir,
                capture_output=True,
                text=True,
                env=env,
                timeout=45,
            )

            assert result.returncode == 0, result.stderr
            record = json.loads(
                next(line for line in result.stdout.splitlines() if line.startswith("{"))
            )
            assert record["status"] == "succeeded"

            manifest = json.loads((output_dir / "index.json").read_text())
            assert {asset["filename"] for asset in manifest["assets"]} == {
                "direct.stl",
                "viewer.glb",
            }
            assert (output_dir / "assets" / "direct.stl").read_bytes().startswith(b"solid")
            assert (output_dir / "assets" / "viewer.glb").read_bytes().startswith(b"glTF")

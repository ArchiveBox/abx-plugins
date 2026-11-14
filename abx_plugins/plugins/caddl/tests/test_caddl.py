"""Integration tests for the CAD/3D asset downloader plugin."""

import hashlib
import json
import os
import subprocess
import tempfile
import threading

from pathlib import Path

import pytest
from werkzeug.wrappers import Response

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
          <a href="/models/direct.glb" download="Model α.glb">Download GLB</a>
          <a href="/models/collision.glb" download="Model α.glb">Another GLB</a>
          <model-viewer src="/models/viewer.glb"></model-viewer>
          <button aria-label="Download button.glb" onclick="location.href='/models/button.glb'">Download</button>
        </body></html>""",
        content_type="text/html; charset=utf-8",
    )
    model = (PLUGIN_DIR / "tests/fixtures/Box.glb").read_bytes()
    httpserver.expect_request("/models/collision.glb").respond_with_data(
        model, content_type="application/octet-stream",
    )
    httpserver.expect_request("/models/direct.glb").respond_with_data(
        model,
        content_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="direct.glb"'},
    )
    stall = threading.Event()
    release = threading.Event()

    def serve_model(request):
        if stall.is_set():
            release.wait(30)
        return Response(model, content_type="application/octet-stream")

    httpserver.expect_request("/models/viewer.glb").respond_with_handler(serve_model)
    httpserver.expect_request("/models/button.glb").respond_with_data(
        model,
        content_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="button.glb"'},
    )
    try:
        yield (
            httpserver.url_for("/").replace("localhost", "127.0.0.1", 1),
            stall,
            release,
        )
    finally:
        release.set()


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
    record = json.loads(
        next(line for line in result.stdout.splitlines() if line.startswith("{")),
    )
    assert record["status"] == "skipped"
    assert record["output_str"] == "CADDL_ENABLED=False"


def test_downloads_direct_and_embedded_assets(caddl_test_url):
    """The real Chrome hook downloads extension-matched page assets and manifests them."""
    url, stall, release = caddl_test_url
    with tempfile.TemporaryDirectory() as tmpdir:
        with chrome_session(
            Path(tmpdir),
            crawl_id="test-caddl",
            snapshot_id="snap-caddl",
            test_url=url,
            timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
        ) as (_, _, snapshot_chrome_dir, env):
            output_dir = snapshot_chrome_dir.parent / "caddl"
            output_dir.mkdir()
            env["CADDL_TIMEOUT"] = "15"
            result = subprocess.run(
                [
                    str(CADDL_HOOK),
                    f"--url={url}",
                    "--snapshot-id=snap-caddl",
                ],
                cwd=output_dir,
                capture_output=True,
                text=True,
                env=env,
                timeout=45,
            )

            assert result.returncode == 0, result.stderr
            record = json.loads(
                next(
                    line for line in result.stdout.splitlines() if line.startswith("{")
                ),
            )
            assert record["status"] == "succeeded"

            manifest = json.loads((output_dir / "downloads.json").read_text())
            assert {asset["filename"] for asset in manifest["files"]} == {
                "Model α.glb",
                "Model α (2).glb",
                "viewer.glb",
                "button.glb",
            }
            for asset in manifest["files"]:
                content = (output_dir / asset["path"]).read_bytes()
                assert content == (PLUGIN_DIR / "tests/fixtures/Box.glb").read_bytes()
                assert asset["size"] == len(content)
                assert asset["sha256"] == hashlib.sha256(content).hexdigest()

            previous = (output_dir / "downloads.json").read_bytes()
            result = subprocess.run(
                [str(CADDL_HOOK), f"--url={url}"],
                cwd=output_dir,
                capture_output=True,
                text=True,
                env=env | {"CADDL_MAX_SIZE": "1"},
                timeout=45,
            )
            assert result.returncode != 0
            assert "exceeds 1 bytes" in result.stderr
            assert (output_dir / "downloads.json").read_bytes() == previous
            assert not list(output_dir.glob(".downloads-*"))

            stall.set()
            try:
                result = subprocess.run(
                    [str(CADDL_HOOK), f"--url={url}"],
                    cwd=output_dir,
                    capture_output=True,
                    text=True,
                    env=env | {"CADDL_TIMEOUT": "5"},
                    timeout=30,
                )
            finally:
                release.set()
            assert result.returncode != 0
            assert "CADDL download deadline exceeded" in result.stderr
            assert (output_dir / "downloads.json").read_bytes() == previous
            for asset in manifest["files"]:
                assert (output_dir / asset["path"]).read_bytes() == (
                    PLUGIN_DIR / "tests/fixtures/Box.glb"
                ).read_bytes()
            assert not list(output_dir.glob(".downloads-*"))

"""Capture a genuine media kit and reject an expired transfer after accepting terms."""

import base64
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

import imagesize
from playwright.sync_api import sync_playwright

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://we.tl/t-JU6cMxNNFJMrW72K"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_wetransfer.js"
MEDIA_KIT_URL = "https://we.tl/t-vRtsYmlUVM"
ZIP_FILENAME = "katherinehunt_headshots-dr-katherine-hunt_2025-10-06_0144.zip"
HEADSHOTS = {
    "KH 2024 1.jpg": (2666, 2962),
    "KH FAAA photo.jpg": (6275, 4846),
    "KH on stage FAAA square.jpg": (3542, 2837),
    "ANDY4332.JPG": (4252, 2836),
    "ANDY2060.JPG": (4252, 2836),
    "ANDY2167.JPG": (4252, 2836),
}


def test_public_media_kit_zip(tmp_path, ensure_chrome_test_prereqs):
    # Published as high-resolution headshots on drkatherinehunt.com/media-kit.
    with chrome_session(tmp_path, test_url=MEDIA_KIT_URL, timeout=60) as (
        _,
        _,
        chrome,
        env,
    ):
        result = subprocess.run(
            [str(HOOK), f"--url={MEDIA_KIT_URL}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record is not None, result.stdout
        assert record["status"] == "succeeded", result.stdout
        output = chrome.parent / "wetransfer"
        manifest = json.loads((output / "downloads.json").read_text())
        assert len(manifest["files"]) == 1
        item = manifest["files"][0]
        assert item["filename"] == ZIP_FILENAME
        assert item["format"] == "zip"
        archive_path = output / item["path"]
        data = archive_path.read_bytes()
        assert len(data) == item["size"] == 12086255
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        assert sorted(path.name for path in (output / "files").iterdir()) == [
            ZIP_FILENAME,
        ], "The delivery ZIP must remain unchanged, without extracted copies"
        with zipfile.ZipFile(archive_path) as archive, sync_playwright() as playwright:
            assert archive.testzip() is None, "All member CRCs must match"
            assert set(archive.namelist()) == set(HEADSHOTS)
            browser = playwright.chromium.connect_over_cdp(
                (chrome / "cdp_url.txt").read_text().strip(),
            )
            page = browser.contexts[0].pages[0]
            for filename, expected_size in HEADSHOTS.items():
                jpeg = archive.read(filename)
                assert jpeg.startswith(b"\xff\xd8\xff") and jpeg.endswith(b"\xff\xd9")
                assert imagesize.get(io.BytesIO(jpeg)) == expected_size
                decoded_size = page.evaluate(
                    """async encoded => {
                        const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
                        const bitmap = await createImageBitmap(new Blob([bytes], {type: 'image/jpeg'}));
                        const size = [bitmap.width, bitmap.height];
                        bitmap.close();
                        return size;
                    }""",
                    base64.b64encode(jpeg).decode(),
                )
                assert decoded_size == list(expected_size), filename


def test_expired_transfer_after_terms_acceptance(tmp_path, ensure_chrome_test_prereqs):
    with chrome_session(tmp_path, test_url=URL, timeout=60) as (_, _, chrome, env):
        result = subprocess.run(
            [str(HOOK), f"--url={URL}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        record = parse_jsonl_output(result.stdout)
        assert result.returncode == 1, result.stderr
        assert record and record["status"] == "failed", result.stdout
        assert "expired or was deleted" in record["output_str"], result.stdout
        assert not (chrome.parent / "wetransfer/downloads.json").exists()

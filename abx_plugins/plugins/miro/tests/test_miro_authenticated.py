# ci-environment: provider-capture
"""Explicit live acceptance: requires an authorized AUTH_STORAGE_FILE persona."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from abx_plugins.plugins.base.testing import (
    install_required_binary_from_config,
    parse_jsonl_output,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://miro.com/app/board/uXjVK4c-_uU=/"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_miro.js"


def test_authenticated_public_board_pdf(tmp_path, ensure_chrome_test_prereqs):
    auth = os.environ.get("AUTH_STORAGE_FILE")
    assert auth and Path(auth).is_file(), (
        "Set AUTH_STORAGE_FILE to an authorized Miro persona before running this explicit acceptance test"
    )
    with chrome_session(
        tmp_path,
        test_url=URL,
        timeout=90,
        env_overrides={"AUTH_STORAGE_FILE": auth, "CHROME_HEADLESS": "false"},
    ) as (_, _, chrome, env):
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
        assert record is not None, result.stdout
        assert record["status"] == "succeeded", result.stdout
        output = chrome.parent / "miro"
        manifest = json.loads((output / "downloads.json").read_text())
        assert "Master template for student activity frame" in manifest["title"]
        pdfs = [item for item in manifest["files"] if item["format"] == "pdf"]
        assert len(pdfs) == 1
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            assert len(data) == item["size"] > 0
            assert hashlib.sha256(data).hexdigest() == item["sha256"]
        pdf = (output / pdfs[0]["path"]).read_bytes()
        assert pdf.startswith(b"%PDF-") and b"%%EOF" in pdf[-100:]
        assert b"/Subtype /Image" in pdf or b"/Subtype/Image" in pdf
        assert b"/Type /Page" in pdf or b"/Type/Page" in pdf
        pdf_path = output / pdfs[0]["path"]
        info = subprocess.run(
            ["pdfinfo", str(pdf_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert re.search(r"^Pages:\s+1$", info.stdout, re.MULTILINE), info.stdout
        image_prefix = tmp_path / "exported-frame"
        subprocess.run(
            ["pdfimages", str(pdf_path), str(image_prefix)],
            check=True,
            timeout=15,
        )
        images = sorted(tmp_path.glob("exported-frame-*.ppm"))
        assert len(images) == 1, images
        magic, dimensions, maximum, pixels = images[0].read_bytes().split(b"\n", 3)
        assert magic == b"P6" and maximum == b"255"
        width, height = map(int, dimensions.split())
        assert width >= 600 and height >= 300
        assert abs(width / height - 16 / 9) < 0.01
        assert len(pixels) == width * height * 3
        # The same native frame uses Retina/standard DPI and platform font
        # rasterization. Verify its actual Hello sticky instead of JPEG bytes.
        colors = zip(pixels[::3], pixels[1::3], pixels[2::3], strict=True)
        yellow = []
        white = 0
        for index, (red, green, blue) in enumerate(colors):
            white += min(red, green, blue) > 240
            if red > 230 and green > 230 and blue < 200:
                yellow.append((index % width, index // width))
        assert 0.95 < white / (width * height) < 0.975
        assert 0.03 < len(yellow) / (width * height) < 0.04
        left = min(x for x, _ in yellow)
        right = max(x for x, _ in yellow) + 1
        top = min(y for _, y in yellow)
        bottom = max(y for _, y in yellow) + 1
        assert 0.425 < left / width < 0.435
        assert 0.565 < right / width < 0.575
        assert 0.345 < top / height < 0.365
        assert 0.605 < bottom / height < 0.625
        sticky = tmp_path / "exported-sticky.ppm"
        sticky.write_bytes(
            f"P6\n{right - left} {bottom - top}\n255\n".encode()
            + b"".join(
                pixels[(y * width + left) * 3 : (y * width + right) * 3]
                for y in range(top, bottom)
            ),
        )
        tesseract = install_required_binary_from_config(
            HOOK.parent.parent / "liteparse",
            "tesseract",
            env=env,
        )
        assert tesseract.loaded_abspath is not None
        ocr = subprocess.run(
            [str(tesseract.loaded_abspath), str(sticky), "stdout", "--psm", "6"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        (tmp_path / "exported-sticky-ocr.txt").write_text(ocr.stdout)
        assert ocr.stdout.strip() == "Hello", ocr.stdout

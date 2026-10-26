# ci-environment: provider-capture
"""Full Substack media acceptance using the authorized persona cookie file."""

import gzip
import hashlib
import json
import os
import subprocess
from http.cookiejar import MozillaCookieJar
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output

# Explicit re-exports register the same installed-runtime fixtures with pytest.
from abx_plugins.plugins.ytdlp.tests.test_ytdlp import (
    YTDLP_HOOK,
    installed_ytdlp_runtime_env as installed_ytdlp_runtime_env,
    ytdlp_runtime_env as ytdlp_runtime_env,
)


def test_substack_json_script_downloads_real_video(
    tmp_path,
    ytdlp_runtime_env,
):  # - imported pytest fixture
    """Parse actual new-format HTML and decode its complete public media bytes."""
    # Hosted media requests need the same authorized persona cookie path as
    # captures. AUTH_STORAGE_FILE alone only supplies browser-based hooks.
    cookies = MozillaCookieJar(os.environ["COOKIES_FILE"])
    cookies.load(ignore_discard=True)
    assert any(
        cookie.domain.lstrip(".") == "substack.com" and not cookie.is_expired()
        for cookie in cookies
    ), "The provider persona must contain current Substack cookies"
    page = gzip.decompress(
        (
            Path(__file__).parent / "fixtures/substack-json-script-video.html.gz"
        ).read_bytes(),
    )
    assert hashlib.sha256(page).hexdigest() == (
        "85b64ba067f2defc06ec7d5b71aeb1c34fb5424e6eb2242878cefd95e77f205c"
    )
    slug = "mussels-with-black-bean-sauce-recipe"
    output = tmp_path / "ytdlp"
    output.mkdir()
    saved_page = output / f"{slug}_https_-_andrewzimmern.substack.com_p_{slug}.dump"
    saved_page.write_bytes(page)
    result = subprocess.run(
        [str(YTDLP_HOOK), f"--url=https://andrewzimmern.substack.com/p/{slug}"],
        cwd=tmp_path,
        env={
            **os.environ,
            **ytdlp_runtime_env,
            "SNAP_DIR": str(tmp_path),
            "YTDLP_ARGS_EXTRA": '["--load-pages"]',
        },
        capture_output=True,
        text=True,
        timeout=140,
    )
    assert "Loading request from" in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "succeeded", result.stdout
    assert saved_page.read_bytes() == page
    videos = list(output.glob("*.mp4"))
    assert len(videos) == 1, list(output.iterdir())
    # yt-dlp metadata can include scoped cookies; never print the whole record
    # in CI assertion diagnostics or publish it as a failure artifact.
    info = json.loads(videos[0].with_suffix(".info.json").read_text())
    assert info["id"] == "47368578", info.get("id")
    assert info["title"] == "Mussels with Black Bean Sauce: Recipe of the Week #7"
    assert info["uploader_id"] == "577659", info.get("uploader_id")
    ffmpeg = Path(ytdlp_runtime_env["FFMPEG_BINARY"]).resolve()
    probe = subprocess.run(
        [
            str(ffmpeg.with_name("ffprobe")),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(videos[0]),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
    media = json.loads(probe.stdout)
    assert 59 < float(media["format"]["duration"]) < 62, media
    assert {stream["codec_type"] for stream in media["streams"]} >= {
        "video",
        "audio",
    }, media
    decoded = subprocess.run(
        [str(ffmpeg), "-v", "error", "-i", str(videos[0]), "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert decoded.returncode == 0, decoded.stderr
    assert not decoded.stderr, decoded.stderr

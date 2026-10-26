# ci-environment: provider-capture
"""Full Substack media acceptance using the authorized persona cookie file."""

import gzip
import hashlib
import json
import os
import shlex
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


def diagnose_substack_media_response(cookie_file, ytdlp_binary, impersonate=False):
    """Describe a separate request through the actual installed yt-dlp runtime."""
    diagnostic = {
        "client": "secondary yt-dlp request (not the original download)",
        "impersonate": "any" if impersonate else None,
    }
    try:
        # Pytest's interpreter need not contain the hook's yt-dlp dependencies.
        # Its installed console script identifies the exact interpreter; a stdlib
        # request would exercise a different TLS/header/cookie implementation.
        with Path(ytdlp_binary).resolve().open("rb") as launcher:
            shebang = shlex.split(launcher.readline(4096).decode().removeprefix("#!"))
        if (
            len(shebang) != 1
            or not Path(shebang[0]).is_absolute()
            or not Path(shebang[0]).is_file()
            or not Path(shebang[0]).name.startswith("python")
        ):
            raise ValueError("yt-dlp does not identify a Python interpreter")
        result = subprocess.run(
            [
                "uv",
                "run",
                "--no-project",
                "--no-sync",
                "--python",
                shebang[0],
                "python",
                "-",
                str(cookie_file),
                "1" if impersonate else "0",
            ],
            input=r"""
import json
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.networking.impersonate import ImpersonateTarget

impersonate = sys.argv[2] == "1"
diagnostic = {
    "client": "secondary yt-dlp request (not the original download)",
    "impersonate": "any" if impersonate else None,
}
try:
    # YoutubeDL saves its jar on close. Diagnose from a private copy so this
    # extra request cannot change the persona used by the failed capture.
    with tempfile.TemporaryDirectory(prefix="ytdlp-diagnostic-") as private:
        cookies = Path(private) / "cookies.txt"
        shutil.copyfile(sys.argv[1], cookies)
        cookies.chmod(0o600)
        options = {"cookiefile": str(cookies), "quiet": True,
                   "no_warnings": True, "socket_timeout": 10}
        if impersonate:
            options["impersonate"] = ImpersonateTarget()
        with YoutubeDL(options) as ydl:
            try:
                response = ydl.urlopen(
                    "https://andrewzimmern.substack.com/api/v1/video/upload/"
                    "52057d2b-a609-471e-9237-e6eeaac08ddb/src?type=hls"
                )
            except HTTPError as error:
                response = error.response
            with response:
                diagnostic.update(
                    impersonate=str(response.extensions.get("impersonate") or "") or None,
                    status=response.status,
                    final_hostname=urlsplit(response.url).hostname,
                    headers={name: response.headers.get(name, "")[:256]
                             for name in ("server", "content-type", "cf-mitigated")},
                )
                body = response.read(8192).lower()
                diagnostic["body_has_hls_marker"] = body.startswith(b"#extm3u")
                diagnostic["body_has_challenge_marker"] = any(
                    marker in body for marker in (
                        b"cf-chl-", b"/cdn-cgi/challenge-platform/", b"verify you are human"
                    )
                )
                diagnostic["body_has_access_denied_marker"] = any(
                    marker in body for marker in (b"access denied", b"<code>accessdenied</code>")
                )
except Exception as error:
    diagnostic["error_type"] = type(error).__name__
print(json.dumps(diagnostic))
""",
            capture_output=True,
            text=True,
            timeout=15,
        )
        result.check_returncode()
        return json.loads(result.stdout)
    except Exception as error:
        # URLs and response text can contain session material, including inside
        # exception messages. This optional evidence must preserve the real failure.
        diagnostic["error_type"] = type(error).__name__
        return diagnostic


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
    if result.returncode:
        # Compare supported transports on the same failing host and persona.
        # Upstream selects its browser profile/headers; neither result retries
        # the capture or replaces the original strict download assertion below.
        for impersonate in (False, True):
            print(
                json.dumps(
                    diagnose_substack_media_response(
                        os.environ["COOKIES_FILE"],
                        ytdlp_runtime_env["YTDLP_BINARY"],
                        impersonate=impersonate,
                    ),
                ),
            )
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

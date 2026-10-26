# ci-environment: provider-capture
# ci-runner: ugnas
# Like Figma, the synced session needs residential egress for native downloads;
# hosted yt-dlp receives a challenge even when that runner's Chrome succeeds.
"""Full Substack media acceptance using the authorized persona cookie file."""

import gzip
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output

# Explicit re-exports register the same installed-runtime fixtures with pytest.
from abx_plugins.plugins.ytdlp.tests.test_ytdlp import (
    YTDLP_HOOK,
    installed_ytdlp_runtime_env as installed_ytdlp_runtime_env,
    ytdlp_runtime_env as ytdlp_runtime_env,
)


def diagnose_substack_media_response(
    cookie_file,
    ytdlp_binary,
    with_referer=False,
    strict_domain=False,
):
    """Describe a separate request through the actual installed yt-dlp runtime."""
    diagnostic = {
        "client": "secondary yt-dlp request (not the original download)",
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
                "1" if with_referer else "0",
                "1" if strict_domain else "0",
            ],
            input=r"""
import json
import shutil
import sys
import tempfile
from http.cookiejar import DefaultCookiePolicy
from pathlib import Path
from urllib.parse import urlsplit
from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import HTTPError

diagnostic = {
    "client": "secondary yt-dlp request (not the original download)",
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
        if sys.argv[2] == "1":
            # Compare upstream-supported embedding-page context, not a made-up
            # browser fingerprint. Keep yt-dlp's own UA and transport untouched.
            options["http_headers"] = {
                "Referer": "https://andrewzimmern.substack.com/p/mussels-with-black-bean-sauce-recipe",
                "Accept": "*/*",
            }
            diagnostic["request_headers"] = options["http_headers"]
        with YoutubeDL(options) as ydl:
            media_url = (
                "https://andrewzimmern.substack.com/api/v1/video/upload/"
                "52057d2b-a609-471e-9237-e6eeaac08ddb/src?type=hls"
            )
            if sys.argv[3] == "1":
                # Chrome excludes host-only parent cookies that the default
                # Netscape policy returns to subdomains. Requests copies jars
                # without their policy, so filter this private diagnostic jar
                # using the supported policy before the single known request.
                jar = ydl.cookiejar
                before = len(jar.get_cookies_for_url(media_url))
                jar.set_policy(DefaultCookiePolicy(
                    strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain,
                ))
                selected = jar.get_cookies_for_url(media_url)
                jar.clear()
                for cookie in selected:
                    jar.set_cookie(cookie)
                diagnostic["strict_cookie_selection"] = {
                    "default_selected_count": before,
                    "strict_selected_count": len(selected),
                }
            try:
                response = ydl.urlopen(media_url)
            except HTTPError as error:
                response = error.response
            with response:
                diagnostic.update(
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


def diagnose_substack_browser_response(cookie_file, ytdlp_binary):
    """Compare the real persona browser and its live jar without retrying capture."""
    from playwright.sync_api import sync_playwright

    from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
        CHROME_NAVIGATE_HOOK,
        chrome_session,
        require_chrome_runtime_impl,
    )

    diagnostic: dict[str, object] = {
        "client": "secondary Chrome persona request (not the original download)",
    }
    try:
        cookies = MozillaCookieJar(cookie_file)
        cookies.load(ignore_discard=True)
        expected = {
            (cookie.domain, cookie.path, cookie.name): cookie.value
            for cookie in cookies
            if not cookie.is_expired()
        }
        require_chrome_runtime_impl()
        # The browser profile and temporary export contain credentials. Keep both
        # outside pytest's uploaded artifact tree; never replace the input persona.
        with tempfile.TemporaryDirectory(
            prefix="substack-browser-diagnostic-",
        ) as private:
            root = Path(private)
            url = "https://andrewzimmern.substack.com/p/mussels-with-black-bean-sauce-recipe"
            with chrome_session(
                root,
                test_url=url,
                navigate=False,
                env_overrides={
                    "COOKIES_FILE": cookie_file,
                    "AUTH_STORAGE_FILE": "",
                    "CHROME_HEADLESS": "false",
                },
            ) as (_, _, chrome, env):
                with sync_playwright() as playwright:
                    browser = playwright.chromium.connect_over_cdp(
                        (chrome / "cdp_url.txt").read_text().strip(),
                    )
                    context = browser.contexts[0]
                    browser_session = browser.new_browser_cdp_session()
                    imported = {
                        (cookie["domain"], cookie["path"], cookie["name"]): cookie[
                            "value"
                        ]
                        for cookie in browser_session.send("Storage.getCookies")[
                            "cookies"
                        ]
                    }
                    diagnostic.update(
                        expected_cookie_count=len(expected),
                        matching_cookie_count=sum(
                            imported.get(key) == value
                            for key, value in expected.items()
                        ),
                    )
                    target = (chrome / "target_id.txt").read_text().strip()
                    page = next(
                        page
                        for page in context.pages
                        if context.new_cdp_session(page).send("Target.getTargetInfo")[
                            "targetInfo"
                        ]["targetId"]
                        == target
                    )
                    navigation = subprocess.run(
                        [
                            str(CHROME_NAVIGATE_HOOK),
                            f"--url={url}",
                            "--snapshot-id=test-snapshot",
                        ],
                        cwd=chrome,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    diagnostic["navigation_exit_code"] = navigation.returncode
                    # Native same-origin fetch uses Chrome's actual cookies and
                    # transport. Do not manufacture a browser UA for yt-dlp.
                    diagnostic["media"] = page.evaluate("""async () => {
                        const response = await fetch('/api/v1/video/upload/52057d2b-a609-471e-9237-e6eeaac08ddb/src?type=hls',
                            {credentials: 'include', signal: AbortSignal.timeout(10000)});
                        const reader = response.body.getReader();
                        let body = new Uint8Array(8192), size = 0;
                        try {
                            while (size < body.length) {
                                const {value, done} = await reader.read();
                                if (done) break;
                                const count = Math.min(value.length, body.length - size);
                                body.set(value.subarray(0, count), size); size += count;
                            }
                        } finally { await reader.cancel(); }
                        const text = new TextDecoder().decode(body.subarray(0, size)).toLowerCase();
                        return {status: response.status, final_hostname: new URL(response.url).hostname,
                            headers: Object.fromEntries(['server', 'content-type', 'cf-mitigated']
                                .map(name => [name, (response.headers.get(name) || '').slice(0, 256)])),
                            body_has_hls_marker: text.startsWith('#extm3u'),
                            body_has_challenge_marker: ['cf-chl-', '/cdn-cgi/challenge-platform/', 'verify you are human']
                                .some(marker => text.includes(marker))};
                    }""")
                    live = browser_session.send("Storage.getCookies")["cookies"]
                    jar_path = root / "cookies.txt"
                    jar = MozillaCookieJar(str(jar_path))
                    for cookie in live:
                        expires = (
                            int(cookie["expires"]) if cookie["expires"] > 0 else None
                        )
                        jar.set_cookie(
                            Cookie(
                                0,
                                cookie["name"],
                                cookie["value"],
                                None,
                                False,
                                cookie["domain"],
                                cookie["domain"].startswith("."),
                                cookie["domain"].startswith("."),
                                cookie["path"],
                                True,
                                cookie["secure"],
                                expires,
                                expires is None,
                                None,
                                None,
                                {"HttpOnly": ""} if cookie["httpOnly"] else {},
                            ),
                        )
                    jar.save(ignore_discard=True)
                    jar_path.chmod(0o600)
                    # Diagnostic only: distinguish an updated browser session from
                    # transport rejection, without a production cookie persistence pass.
                    diagnostic["live_browser_jar_ytdlp"] = (
                        diagnose_substack_media_response(
                            str(jar_path),
                            ytdlp_binary,
                        )
                    )
    except Exception as error:
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
        print(
            json.dumps(
                diagnose_substack_media_response(
                    os.environ["COOKIES_FILE"],
                    ytdlp_runtime_env["YTDLP_BINARY"],
                ),
            ),
        )
        # A real Chrome GET carries its embedding page as Referer. This bounded
        # comparison isolates that supported context before any extractor change;
        # it cannot turn the original failed capture into a passing test.
        print(
            json.dumps(
                diagnose_substack_media_response(
                    os.environ["COOKIES_FILE"],
                    ytdlp_runtime_env["YTDLP_BINARY"],
                    with_referer=True,
                ),
            ),
        )
        # Isolate a proven request-cookie difference without changing the
        # original download, its UA/transport, or its strict media assertions.
        print(
            json.dumps(
                diagnose_substack_media_response(
                    os.environ["COOKIES_FILE"],
                    ytdlp_runtime_env["YTDLP_BINARY"],
                    strict_domain=True,
                ),
            ),
        )
        print(
            json.dumps(
                diagnose_substack_browser_response(
                    os.environ["COOKIES_FILE"],
                    ytdlp_runtime_env["YTDLP_BINARY"],
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

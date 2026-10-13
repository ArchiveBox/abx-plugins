"""
Integration tests for ytdlp plugin

Tests verify:
1. Hook script exists
2. Verify deps with abxpkg
3. YT-DLP extraction works on video URLs
4. JSONL output is correct
5. Config options work (YTDLP_ENABLED, YTDLP_TIMEOUT)
6. Handles non-video URLs gracefully
"""

import gzip
import hashlib
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import pytest

from abx_plugins.plugins.base.testing import (
    install_required_binary_from_config,
    parse_jsonl_output,
)

PLUGIN_DIR = Path(__file__).parent.parent
PLUGINS_ROOT = PLUGIN_DIR.parent
_YTDLP_HOOK = next(PLUGIN_DIR.glob("on_Snapshot__*_ytdlp.*"), None)
if _YTDLP_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
YTDLP_HOOK = _YTDLP_HOOK
TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def _build_test_wav_bytes() -> bytes:
    """Build a short deterministic WAV payload for local-media extractor tests."""
    sample_rate = 8000
    duration_seconds = 1
    num_frames = sample_rate * duration_seconds

    wav_io = io.BytesIO()
    with wave.open(wav_io, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * num_frames)

    return wav_io.getvalue()


@pytest.fixture
def non_video_test_url(httpserver):
    """Serve deterministic non-media content for failure-path ytdlp tests."""
    httpserver.expect_request("/").respond_with_data(
        """
        <!doctype html>
        <html>
          <head><title>Not a media URL</title></head>
          <body><h1>No downloadable media here</h1></body>
        </html>
        """.strip(),
        content_type="text/html; charset=utf-8",
    )
    return httpserver.url_for("/")


@pytest.fixture
def media_test_url(httpserver):
    """Serve deterministic media bytes for end-to-end ytdlp extraction tests."""
    httpserver.expect_request("/sample.wav").respond_with_data(
        _build_test_wav_bytes(),
        content_type="audio/wav",
    )
    return httpserver.url_for("/sample.wav")


@pytest.fixture(scope="session")
def installed_ytdlp_runtime_env(tmp_path_factory) -> dict[str, str]:
    """Install every declared yt-dlp dependency once through abxpkg."""
    lib_dir = Path(
        os.environ.get(
            "ABXPKG_LIB_DIR",
            str(tmp_path_factory.mktemp("ytdlp_test_lib")),
        ),
    )
    expected_bin_dir = lib_dir / "env" / "bin"
    install_env = os.environ.copy()
    install_env["ABXPKG_LIB_DIR"] = str(lib_dir)

    resolved = {
        "ABXPKG_LIB_DIR": str(lib_dir),
        "PATH": os.pathsep.join(
            (str(expected_bin_dir), install_env.get("PATH", "")),
        ).rstrip(os.pathsep),
    }
    for config_name, env_name in (
        ("yt-dlp", "YTDLP_BINARY"),
        ("node", "NODE_BINARY"),
        ("ffmpeg", "FFMPEG_BINARY"),
    ):
        binary = install_required_binary_from_config(
            PLUGIN_DIR,
            config_name,
            env=install_env,
        )
        assert binary and binary.loaded_abspath, (
            f"{config_name} dependency resolution failed via abxpkg"
        )
        binary_path = Path(binary.loaded_abspath)
        assert binary_path.is_file(), (
            f"{config_name} binary path invalid: {binary_path}"
        )
        assert binary.loaded_binprovider is not None, (
            f"{config_name} must identify the abxpkg provider that resolved it"
        )
        if binary.loaded_binprovider.name == "env":
            assert binary_path.parent == expected_bin_dir, (
                f"host {config_name} must be projected through {expected_bin_dir}: "
                f"{binary_path}"
            )
        resolved[env_name] = str(binary_path)

    return resolved


@pytest.fixture
def ytdlp_runtime_env(installed_ytdlp_runtime_env) -> dict[str, str]:
    """Return the resolved runtime for process-scoped hook environments."""
    return installed_ytdlp_runtime_env.copy()


def test_hook_script_exists():
    """Verify on_Snapshot hook exists."""
    assert YTDLP_HOOK.exists(), f"Hook not found: {YTDLP_HOOK}"


def test_card_template_lists_files_without_links_or_eager_players():
    """The card lists filenames without individual links or eager media downloads."""
    template = (PLUGIN_DIR / "templates" / "card.html").read_text()
    assert 'class="thumbnail-wrapper"' not in template
    assert 'class="ytdlp-file-badge"' in template
    assert "<a " not in template
    assert '<span class="ytdlp-file-name">{{ file.name }}</span>' in template
    assert "text-overflow: ellipsis" in template
    assert "white-space: nowrap" in template
    assert "<video" not in template
    assert "<audio" not in template


def test_verify_deps_with_abxpkg(ytdlp_runtime_env):
    """Verify the complete yt-dlp runtime resolves through the real preflight."""
    assert {
        "YTDLP_BINARY",
        "NODE_BINARY",
        "FFMPEG_BINARY",
    } < ytdlp_runtime_env.keys()


def test_handles_non_video_url(non_video_test_url, ytdlp_runtime_env):
    """Test that ytdlp extractor handles non-video URLs gracefully via hook."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        env = os.environ.copy()
        env.update(ytdlp_runtime_env)
        env["SNAP_DIR"] = str(tmpdir)

        # Run ytdlp extraction hook on non-video URL
        result = subprocess.run(
            [
                str(YTDLP_HOOK),
                "--url",
                non_video_test_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        # Should exit 0 even for non-media URL
        assert result.returncode == 0, (
            f"Should handle non-media URL gracefully: {result.stderr}"
        )

        # Parse clean JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, "Should have ArchiveResult JSONL output"
        assert result_json["status"] == "noresults", (
            f"Non-media URL should report noresults: {result_json}"
        )
        assert result_json["output_str"] == "No media found", result_json


def test_twitter_post_without_video_reports_noresults(tmp_path, ytdlp_runtime_env):
    """A real image-only post is a successful media check with no video to save."""
    result = subprocess.run(
        [str(YTDLP_HOOK), "--url=https://x.com/dysmemic/status/2108398189862957386"],
        cwd=tmp_path,
        env={**os.environ, **ytdlp_runtime_env, "SNAP_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "No video could be found in this tweet" in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "noresults", result.stdout
    assert record["output_str"] == "No media found", record
    assert not list((tmp_path / "ytdlp").iterdir())


def test_reddit_text_post_reports_noresults(tmp_path, ytdlp_runtime_env):
    """The real Reddit parser recognizes a saved text post with no media."""
    page = gzip.decompress(
        (Path(__file__).parent / "fixtures" / "reddit-text-post.json.gz").read_bytes(),
    )
    assert hashlib.sha256(page).hexdigest() == (
        "7a1ce8abd0549a72be7c469a1e6333e366eff6ba1b71ff04c082ea3eef118db2"
    )
    output_dir = tmp_path / "ytdlp"
    output_dir.mkdir()
    saved_page = (
        output_dir / "1k11yw5_https_-_www.reddit.com_comments_1k11yw5_.json.dump"
    )
    saved_page.write_bytes(page)
    # A real refused proxy connection makes session setup warn, while native
    # saved-page loading still reaches the real Reddit metadata parser.
    with socket.socket() as unavailable_proxy:
        unavailable_proxy.bind(("127.0.0.1", 0))
        proxy = f"http://127.0.0.1:{unavailable_proxy.getsockname()[1]}"
        result = subprocess.run(
            [str(YTDLP_HOOK), "--url=https://www.reddit.com/comments/1k11yw5/"],
            cwd=tmp_path,
            env={
                **os.environ,
                **ytdlp_runtime_env,
                "SNAP_DIR": str(tmp_path),
                "YTDLP_ARGS_EXTRA": f'["--load-pages", "--proxy", "{proxy}"]',
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert "Session request failed" in result.stderr, result.stderr
    assert "Loading request from" in result.stderr, result.stderr
    assert "Unable to load request" not in result.stderr, result.stderr
    assert "ERROR: [Reddit+archivebox] 1k11yw5: No media found" in result.stderr, (
        result.stderr
    )
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "noresults", result.stdout
    assert record["output_str"] == "No media found", record
    assert saved_page.read_bytes() == page
    saved_page.unlink()  # Remove only the unchanged test input before checking outputs.
    assert not list(output_dir.iterdir())


class _RecordedHTTPSProxy(ThreadingHTTPServer):
    """Local HTTP proxy that answers HTTPS requests from recorded responses.

    Real yt-dlp tunnels https:// requests through CONNECT; the tunnel is
    terminated with a throwaway self-signed certificate (the hook runs with
    certificate checks disabled), so the real extractors run unmodified against
    their real https://www.reddit.com/... URLs. Unrecorded URLs get HTTP 404.
    """

    daemon_threads = True

    def __init__(self, tls_dir: Path, routes: dict[str, tuple[int, dict, bytes]]):
        key_path, cert_path = tls_dir / "proxy.key", tls_dir / "proxy.crt"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key_path),
                "-out",
                str(cert_path),
                "-days",
                "1",
                "-subj",
                "/CN=www.reddit.com",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ssl_context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        self.routes = routes
        self.requests: list[str] = []
        super().__init__(("127.0.0.1", 0), _RecordedHTTPSProxyHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def handle_error(self, request, client_address):
        # yt-dlp may drop idle keep-alive tunnels when it exits.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


class _RecordedHTTPSProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    tunnel_host = ""

    @property
    def proxy(self) -> _RecordedHTTPSProxy:
        assert isinstance(self.server, _RecordedHTTPSProxy)
        return self.server

    def log_message(self, format, *args):
        pass

    def do_CONNECT(self):
        self.send_response(200, "Connection Established")
        self.end_headers()
        try:
            tls = self.proxy.ssl_context.wrap_socket(self.connection, server_side=True)
        except (ssl.SSLError, OSError):
            self.close_connection = True
            return
        # Keep serving requests on this connection, now inside the TLS tunnel.
        self.connection = self.request = tls
        self.rfile, self.wfile = tls.makefile("rb"), tls.makefile("wb")
        self.tunnel_host = self.path.rsplit(":", 1)[0]

    def do_GET(self):
        url = urlsplit(
            self.path
            if not self.tunnel_host
            else f"https://{self.tunnel_host}{self.path}",
        )
        key = f"{url.scheme}://{url.hostname}{url.path}"
        self.proxy.requests.append(key)
        status, headers, body = self.proxy.routes.get(key, (404, {}, b"not recorded"))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def recorded_https_proxy(tmp_path_factory):
    servers: list[_RecordedHTTPSProxy] = []

    def start(routes: dict[str, tuple[int, dict, bytes]]) -> _RecordedHTTPSProxy:
        server = _RecordedHTTPSProxy(tmp_path_factory.mktemp("proxy_tls"), routes)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _recorded_reddit_json(name: str, sha256: str) -> bytes:
    page = gzip.decompress((Path(__file__).parent / "fixtures" / name).read_bytes())
    assert hashlib.sha256(page).hexdigest() == sha256
    return page


def _run_hook_through_proxy(tmp_path, env, proxy, url, timeout=120):
    start = time.monotonic()
    result = subprocess.run(
        [str(YTDLP_HOOK), f"--url={url}"],
        cwd=tmp_path,
        env={
            **os.environ,
            **env,
            "SNAP_DIR": str(tmp_path),
            "YTDLP_CHECK_SSL_VALIDITY": "False",
            "YTDLP_ARGS_EXTRA": json.dumps(["--proxy", proxy.url]),
        },
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result, time.monotonic() - start


GALLERY_POST_URL = "https://www.reddit.com/r/interestingasfuck/comments/1hkxvx5/san_francisco_bay_bridge_workers_in_a_white/"


def test_reddit_gallery_post_does_not_loop(
    tmp_path,
    ytdlp_runtime_env,
    recorded_https_proxy,
):
    """A gallery post must stop at "No media found" instead of re-extracting itself.

    reddit-gallery-post.json.gz is the byte-exact anonymous public API response
    for this production post, recorded on 2026-10-12 with yt-dlp 2026.06.09:
    ``yt-dlp --ignore-config --write-pages --skip-download https://www.reddit.com/comments/1hkxvx5/``
    (224246 bytes; post ``url`` is https://www.reddit.com/gallery/1hkxvx5, two
    ``Image`` media_metadata entries, no video). Reddit answers the gallery URL
    with a redirect back to the post, which is what makes unpatched yt-dlp loop
    (Reddit -> generic /gallery/<id> -> redirect -> Reddit ...) until the hook
    timeout. Live Reddit only sustains that loop for logged-in sessions, so the
    responses are replayed locally to keep the reproduction deterministic.
    """
    gallery = "https://www.reddit.com/gallery/1hkxvx5"
    post_json = "https://www.reddit.com/r/interestingasfuck/comments/1hkxvx5/.json"
    proxy = recorded_https_proxy(
        {
            post_json: (
                200,
                {"Content-Type": "application/json; charset=UTF-8"},
                _recorded_reddit_json(
                    "reddit-gallery-post.json.gz",
                    "5421818e285d37e34cce8f6470522fbd02f78e4bf6b85ec20a85f4651b9d188c",
                ),
            ),
            gallery: (301, {"Location": GALLERY_POST_URL}, b""),
            GALLERY_POST_URL: (
                200,
                {"Content-Type": "text/html; charset=utf-8"},
                b"<!doctype html><title>reddit</title>",
            ),
        },
    )
    result, elapsed = _run_hook_through_proxy(
        tmp_path,
        {**ytdlp_runtime_env, "YTDLP_TIMEOUT": "30"},
        proxy,
        GALLERY_POST_URL,
    )
    assert "ERROR: [Reddit+archivebox] 1hkxvx5: No media found" in result.stderr, (
        result.stderr
    )
    assert "[generic]" not in result.stderr, result.stderr
    assert proxy.requests.count(post_json) == 1, proxy.requests
    assert gallery not in proxy.requests, proxy.requests
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record == {
        "type": "ArchiveResult",
        "status": "noresults",
        "output_str": "No media found",
    }, result.stdout
    assert elapsed < 25, f"gallery extraction took {elapsed:.1f}s"
    assert not list((tmp_path / "ytdlp").iterdir())


def test_reddit_hosted_video_still_downloads(
    tmp_path,
    ytdlp_runtime_env,
    recorded_https_proxy,
):
    """Reddit-hosted video extraction is unchanged by the gallery override.

    reddit-video-post.json.gz is the byte-exact anonymous public API response
    recorded on 2026-10-12 with yt-dlp 2026.06.09:
    ``yt-dlp --ignore-config --write-pages --skip-download https://www.reddit.com/comments/6rrwyj/``
    (391823 bytes; ``secure_media.reddit_video`` for v.redd.it/zv89llsvexdz).
    The HLS/DASH manifests are not recorded, so RedditIE's real fallback
    format is downloaded; a locally encoded MP4 stands in for its bytes.
    """
    video = tmp_path / "source.mp4"
    subprocess.run(
        [
            ytdlp_runtime_env["FFMPEG_BINARY"],
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=64x64:rate=5",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "mpeg4",
            str(video),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    post_json = "https://www.reddit.com/r/videos/comments/6rrwyj/.json"
    fallback = "https://v.redd.it/zv89llsvexdz/DASH_2_4_M"
    proxy = recorded_https_proxy(
        {
            post_json: (
                200,
                {"Content-Type": "application/json; charset=UTF-8"},
                _recorded_reddit_json(
                    "reddit-video-post.json.gz",
                    "3144a7da79dae71f24bfbfd5b5305b1f8c59110214d4f0f7a5e5ad1ffe6226ac",
                ),
            ),
            fallback: (200, {"Content-Type": "video/mp4"}, video.read_bytes()),
        },
    )
    video.unlink()
    result, _elapsed = _run_hook_through_proxy(
        tmp_path,
        ytdlp_runtime_env,
        proxy,
        "https://www.reddit.com/r/videos/comments/6rrwyj/that_small_heart_attack/",
    )
    assert "[Reddit+archivebox] Extracting URL" in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "succeeded", result.stdout
    assert proxy.requests.count(fallback) == 1, proxy.requests
    saved = tmp_path / record["output_str"]
    assert saved.suffix == ".mp4" and saved.parent == tmp_path / "ytdlp", record
    with saved.open("rb") as media:
        assert media.read(12)[4:8] == b"ftyp"
    info = json.loads(next((tmp_path / "ytdlp").glob("*.info.json")).read_text())
    assert (info["id"], info["display_id"], info["title"], info["format_id"]) == (
        "zv89llsvexdz",
        "6rrwyj",
        "That small heart attack.",
        "fallback",
    ), info


def test_substack_newsletter_reports_noresults(tmp_path, ytdlp_runtime_env):
    """Replay real public HTML through yt-dlp's native saved-page input."""
    page = gzip.decompress(
        (
            Path(__file__).parent / "fixtures" / "substack-newsletter.html.gz"
        ).read_bytes(),
    )
    assert hashlib.sha256(page).hexdigest() == (
        "e2fe1f2435ddfca8713cf651a7b64f05d5e66039a5f35ba81df34c24aa3c326c"
    )
    # Hosted Substack may return403 before extraction. Native replay exercises
    # the real newsletter parser without converting those HTTP errors to absence.
    output_dir = tmp_path / "ytdlp"
    output_dir.mkdir()
    saved_page = output_dir / (
        "how-apple-built-icloud-to-store-billions_https_-_"
        "engineercodex.substack.com_p_how-apple-built-icloud-to-store-billions.dump"
    )
    saved_page.write_bytes(page)
    result = subprocess.run(
        [
            str(YTDLP_HOOK),
            "--url=https://engineercodex.substack.com/p/how-apple-built-icloud-to-store-billions",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            **ytdlp_runtime_env,
            "SNAP_DIR": str(tmp_path),
            "YTDLP_ARGS_EXTRA": '["--load-pages"]',
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "Loading request from" in result.stderr, result.stderr
    assert "Unable to load request" not in result.stderr, result.stderr
    assert "Downloading webpage" not in result.stderr, result.stderr
    assert "[Substack]" in result.stderr, result.stderr
    assert 'Page type "newsletter" is not supported' in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "noresults", result.stdout
    assert record["output_str"] == "No media found", record
    assert saved_page.read_bytes() == page
    saved_page.unlink()  # Remove only the unchanged test input before checking outputs.
    assert not list((tmp_path / "ytdlp").iterdir())


@pytest.mark.parametrize("status", [403, 404])
def test_http_failure_remains_failed(tmp_path, httpserver, ytdlp_runtime_env, status):
    """Actual HTTP failures must not become successful media-absence checks."""
    httpserver.expect_request("/unavailable").respond_with_data(
        "Unavailable",
        status=status,
        content_type="text/plain",
    )
    result = subprocess.run(
        [str(YTDLP_HOOK), "--url", httpserver.url_for("/unavailable")],
        cwd=tmp_path,
        env={**os.environ, **ytdlp_runtime_env, "SNAP_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "failed", result.stdout
    assert record["output_str"] == {403: "403 Forbidden", 404: "404 Not Found"}[status]
    assert not list((tmp_path / "ytdlp").iterdir())


def test_config_ytdlp_enabled_false_skips(ytdlp_runtime_env):
    """Test that YTDLP_ENABLED=False exits without emitting JSONL."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env.update(ytdlp_runtime_env)
        env["YTDLP_ENABLED"] = "False"

        result = subprocess.run(
            [
                str(YTDLP_HOOK),
                "--url",
                TEST_URL,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

        assert result.returncode == 0, (
            f"Should exit 0 when feature disabled: {result.stderr}"
        )

        # Feature disabled should emit skipped JSONL
        assert "Skipping" in result.stderr or "False" in result.stderr, (
            "Should log skip reason to stderr"
        )

        result_json = parse_jsonl_output(result.stdout)
        assert result_json, "Expected skipped JSONL output"
        assert result_json["status"] == "skipped", result_json
        assert result_json["output_str"] == "YTDLP_ENABLED=False", result_json


def test_config_timeout(non_video_test_url, ytdlp_runtime_env):
    """Test that YTDLP_TIMEOUT config is respected (also via MEDIA_TIMEOUT alias)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env.update(ytdlp_runtime_env)
        env["MEDIA_TIMEOUT"] = "30"
        env["SNAP_DIR"] = str(tmpdir)

        start_time = time.time()
        result = subprocess.run(
            [
                str(YTDLP_HOOK),
                "--url",
                non_video_test_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=35,
        )
        elapsed_time = time.time() - start_time

        assert result.returncode == 0, (
            f"Should complete without hanging: {result.stderr}"
        )
        assert elapsed_time <= 10.0, (
            f"Non-media URL should still fail quickly even with a valid configured timeout, took {elapsed_time:.2f}s"
        )
        result_json = parse_jsonl_output(result.stdout)
        assert result_json == {
            "type": "ArchiveResult",
            "status": "noresults",
            "output_str": "No media found",
        }, result_json


@pytest.mark.parametrize(
    "config_key",
    ["YOUTUBEDL_ARGS", "YTDLP_EXTRA_ARGS", "YTDLP_ARGS_EXTRA"],
)
def test_extra_args_aliases_reach_real_download(
    tmp_path: Path,
    media_test_url: str,
    ytdlp_runtime_env: dict[str, str],
    config_key: str,
):
    # A schema/CLI read alone does not prove the hook uses the extra arguments.
    # A custom output template gives a visible side effect in real yt-dlp output.
    env = os.environ.copy()
    env.update(ytdlp_runtime_env)
    env["SNAP_DIR"] = str(tmp_path)
    env["YTDLP_TIMEOUT"] = "60"
    for key in ("YOUTUBEDL_ARGS", "YTDLP_EXTRA_ARGS", "YTDLP_ARGS_EXTRA"):
        env.pop(key, None)
    env[config_key] = json.dumps(["--output", "alias-output.%(ext)s"])
    result = subprocess.run(
        [str(YTDLP_HOOK), "--url", media_test_url],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "succeeded", result.stdout
    saved = list((tmp_path / "ytdlp").rglob("alias-output.wav"))
    assert len(saved) == 1
    # The default --add-metadata step can rewrite WAV headers. Verify the
    # decoded audio and format, while allowing the intended embedded metadata.
    with (
        wave.open(str(saved[0])) as captured,
        wave.open(io.BytesIO(_build_test_wav_bytes())) as original,
    ):
        assert captured.getparams() == original.getparams()
        assert captured.readframes(captured.getnframes()) == original.readframes(
            original.getnframes(),
        )


def test_extracts_local_media_url(media_test_url, ytdlp_runtime_env):
    """Test yt-dlp extraction against deterministic local media served by httpserver."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        env = os.environ.copy()
        env.update(ytdlp_runtime_env)
        env["YTDLP_TIMEOUT"] = "60"
        env["SNAP_DIR"] = str(tmpdir)

        start_time = time.time()
        result = subprocess.run(
            [
                str(YTDLP_HOOK),
                "--url",
                media_test_url,
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )
        elapsed_time = time.time() - start_time

        assert result.returncode == 0, (
            f"Should extract local media successfully: {result.stderr}"
        )

        # Parse JSONL output
        result_json = parse_jsonl_output(result.stdout)

        assert result_json, (
            f"Should have ArchiveResult JSONL output. stdout: {result.stdout}"
        )
        assert result_json["type"] == "ArchiveResult", result_json
        assert result_json["status"] == "succeeded", result_json

        # Check that some video/audio files were downloaded
        output_files = sorted(tmpdir.glob("**/*"))
        media_files = [
            f
            for f in output_files
            if f.is_file()
            and f.suffix.lower()
            in (
                ".mp4",
                ".webm",
                ".mkv",
                ".m4a",
                ".mp3",
                ".wav",
                ".json",
                ".jpg",
                ".webp",
            )
        ]

        assert len(media_files) > 0, (
            f"Should have downloaded at least one video/audio file. Files: {output_files}"
        )
        output_path = tmpdir / result_json["output_str"]
        assert output_path.is_file(), f"ArchiveResult output missing: {output_path}"
        assert output_path in media_files, (
            f"ArchiveResult should point at a downloaded media artifact: {result_json}"
        )
        assert output_path.stat().st_size > 0, (
            f"Downloaded media is empty: {output_path}"
        )

        print(
            f"Successfully extracted {len(media_files)} file(s) in {elapsed_time:.2f}s",
        )


def test_uses_real_ffmpeg_binary_from_env_when_not_on_path(
    media_test_url,
    ytdlp_runtime_env,
):
    """Hook should use FFMPEG_BINARY explicitly instead of relying on PATH probing."""
    ffmpeg_binary = ytdlp_runtime_env["FFMPEG_BINARY"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        env = os.environ.copy()
        env.update(ytdlp_runtime_env)
        env["SNAP_DIR"] = str(tmpdir_path)
        env["YTDLP_ARGS_EXTRA"] = '["--downloader","ffmpeg"]'
        uv_dir = next(
            (
                path
                for path in env.get("PATH", "").split(os.pathsep)
                if path and (Path(path) / "uv").exists()
            ),
            "",
        )
        ffmpeg_dirs = {
            str(Path(ffmpeg_binary).parent.resolve()),
            str(Path(ffmpeg_binary).resolve().parent),
        }
        filtered_paths = [
            path
            for path in env.get("PATH", "").split(os.pathsep)
            if path and str(Path(path).resolve()) not in ffmpeg_dirs
        ]
        if uv_dir and uv_dir not in filtered_paths:
            filtered_paths.insert(0, uv_dir)
        env["PATH"] = os.pathsep.join(filtered_paths)

        result = subprocess.run(
            [
                str(YTDLP_HOOK),
                "--url",
                media_test_url,
            ],
            cwd=tmpdir_path,
            capture_output=True,
            text=True,
            env=env,
            timeout=90,
        )

        assert result.returncode == 0, result.stderr
        result_json = parse_jsonl_output(result.stdout)
        assert result_json, (
            f"Should have ArchiveResult JSONL output. stdout: {result.stdout}"
        )
        assert result_json["type"] == "ArchiveResult", result_json
        assert result_json["status"] == "succeeded", result_json
        output_path = tmpdir_path / result_json["output_str"]
        assert output_path.is_file(), f"ArchiveResult output missing: {output_path}"
        assert output_path.stat().st_size > 0, (
            f"Downloaded media is empty: {output_path}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


@pytest.mark.parametrize(
    ("url", "provider"),
    [
        ("https://DRIVE.GOOGLE.COM:443/drive/folders/public-folder", "googledrive"),
        ("https://WWW.DROPBOX.COM:443/sh/public/folder", "dropbox"),
        (
            "https://drive.google.com/drive/folders/1KpLl_1tcK0eeehzN980zbG-3M2nhbVks",
            "googledrive",
        ),
        (
            "https://www.dropbox.com/scl/fo/kf9a29cwaebpkbtug6a7k/AFiq9xq2XcvTcmHl_z-tsIc/Lockups?rlkey=4mrp0lpvxmwrlwdy349nspygn&dl=0",
            "dropbox",
        ),
    ],
)
def test_folder_downloads_belong_to_provider_plugins(
    tmp_path,
    ytdlp_runtime_env,
    url,
    provider,
):
    result = subprocess.run(
        [str(YTDLP_HOOK), "--url", url],
        cwd=tmp_path,
        env={**os.environ, **ytdlp_runtime_env, "SNAP_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "noresults", result.stdout
    assert (
        record["output_str"]
        == f"Folder URL is not media; enable {provider} to download its files"
    ), record
    assert "[ytdlp] Starting download" not in result.stderr
    assert not list((tmp_path / "ytdlp").iterdir())


def test_individual_public_video_still_downloads(tmp_path, ytdlp_runtime_env):
    url = "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"
    result = subprocess.run(
        [str(YTDLP_HOOK), "--url", url],
        cwd=tmp_path,
        env={**os.environ, **ytdlp_runtime_env, "SNAP_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "succeeded", result.stdout
    videos = list((tmp_path / "ytdlp").glob("*.mp4"))
    assert len(videos) == 1
    with videos[0].open("rb") as video:
        assert video.read(12)[4:8] == b"ftyp"
    assert videos[0].stat().st_size > 1_000_000

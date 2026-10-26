from __future__ import annotations

import json
import base64
import os
from html import escape as html_escape
import struct
import sys
import zipfile
from pathlib import Path

import pytest

from abx_plugins.plugins.archivewebpage import presentation


@pytest.mark.parametrize(
    "supported, missing_record",
    [(True, False), (False, False), (True, True)],
)
def test_replay_reports_real_document_or_unsupported_platform(
    tmp_path,
    httpserver,
    require_chrome_runtime,
    supported,
    missing_record,
):
    """Use an actual insecure origin to exercise absent SW, without API overrides."""
    from playwright.sync_api import sync_playwright
    from werkzeug.wrappers import Response
    from abx_plugins.plugins.base.testing import install_required_binary_from_config
    from abx_plugins.plugins.base.utils import load_config

    plugin_dir = Path(presentation.__file__).parent
    install_required_binary_from_config(
        plugin_dir,
        "archivewebpage",
        env=dict(os.environ),
    )
    config = load_config(plugin_dir / "config.json", hydrate_binaries=False)
    fixture = Path(__file__).parent / "fixtures/replay/iframetest.wacz"
    source = "/archivewebpage/capture.wacz"
    for asset in ("ui.js", "sw-index.js", "sw.js", "replay.html"):
        response = presentation.serve_replay_asset(f"replay/{asset}", config)
        assert response is not None
        body, content_type, headers = response
        httpserver.expect_request(f"/replay/{asset}").respond_with_data(
            body,
            content_type=content_type,
            headers=headers,
        )
    archive = fixture.read_bytes()
    viewer_html = presentation.render_replay_html(
        "capture.wacz",
        source,
        None if missing_record else fixture,
        fallback_url="https://example.invalid/not-in-archive" if missing_record else "",
    )

    def serve_capture(request):
        # Match the real preview/raw URL contract: the worker only accepts an
        # inline index from preview=1 at the same path as the actual archive.
        if request.args.get("preview") == "1":
            return Response(viewer_html, content_type="text/html")
        if byte_range := request.headers.get("Range"):
            start, end = byte_range.removeprefix("bytes=").split("-")
            first = int(start) if start else max(0, len(archive) - int(end))
            last = (
                min(int(end), len(archive) - 1) if start and end else len(archive) - 1
            )
            return Response(
                archive[first : last + 1],
                status=206,
                headers={
                    "Content-Range": f"bytes {first}-{last}/{len(archive)}",
                    "Accept-Ranges": "bytes",
                },
                content_type="application/wacz",
            )
        return Response(
            archive,
            content_type="application/wacz",
            headers={"Accept-Ranges": "bytes"},
        )

    httpserver.expect_request(source).respond_with_handler(serve_capture)
    httpserver.expect_request("/").respond_with_data(
        f'<!doctype html><iframe id="replay" src="{source}?preview=1" style="width:100%;height:90vh"></iframe>'
        '<script>window.messages=[];addEventListener("message",event=>{'
        'if(event.source===document.getElementById("replay").contentWindow) messages.push(event.data);});</script>',
        content_type="text/html",
    )
    url = httpserver.url_for("/")
    if not supported:
        url = url.replace("localhost", "replay.invalid")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ["CHROME_BINARY"],
            args=[
                "--no-sandbox",
                "--host-resolver-rules=MAP replay.invalid 127.0.0.1",
                "--no-proxy-server",
            ],
        )
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded")
        if supported and not missing_record:
            page.wait_for_function(
                "messages.some(message => message.type === 'archivebox-replay-painted')",
            )
            replay = next(frame for frame in page.frames if "/replay/w/" in frame.url)
            assert "Outside iframe" in replay.locator("body").inner_text()
            assert not page.evaluate(
                "messages.some(message => message.type === 'archivebox-replay-error')",
            )
        else:
            page.wait_for_function(
                "messages.some(message => message.type === 'archivebox-replay-error')",
            )
            viewer = next(frame for frame in page.frames if "preview=1" in frame.url)
            if missing_record:
                assert viewer.evaluate("'serviceWorker' in navigator") is True
                assert viewer.evaluate("window.archiveboxReplayError") == (
                    "Requested page was not found in the saved WACZ"
                )
            else:
                assert viewer.evaluate("'serviceWorker' in navigator") is False
                assert viewer.get_by_role("link", name="Open in browser").is_visible()
            assert not page.evaluate(
                "messages.some(message => message.type === 'archivebox-replay-painted')",
            )
        page.screenshot(path=str(tmp_path / "replay.png"))
        browser.close()


def test_replay_storage_fix_preserves_installed_bundle_and_rejects_changes(tmp_path):
    from abx_plugins.plugins.base.testing import install_required_binary_from_config
    from abx_plugins.plugins.base.utils import load_config

    plugin_dir = Path(presentation.__file__).parent
    env = {**os.environ, "ABXPKG_LIB_DIR": str(tmp_path / "lib")}
    install_required_binary_from_config(plugin_dir, "archivewebpage", env=env)
    config = load_config(
        plugin_dir / "config.json",
        environ=env,
        hydrate_binaries=False,
    )
    extension_dir = presentation.find_extension_dir(config)
    assert extension_dir is not None
    source = extension_dir / "sw.js"
    original = source.read_bytes()
    result = presentation.serve_replay_asset("replay/sw.js", config)
    assert result is not None
    transformed, content_type, headers = result
    assert transformed != original
    assert source.read_bytes() == original
    assert content_type == "application/javascript; charset=utf-8"
    assert headers["Service-Worker-Allowed"] == "/"
    indexed_result = presentation.serve_replay_asset("replay/sw-index.js", config)
    assert indexed_result is not None
    assert indexed_result[0] == transformed
    with pytest.raises(RuntimeError, match="requires the pinned 0.17.0 worker bundle"):
        presentation._replay_worker_body(original + b"\n")


def test_replay_index_stops_file_reads_after_metadata_budget(tmp_path):
    """Profile real ZIP reads: a full inline budget must stop the header scan."""
    path = tmp_path / "capture.wacz"
    name = "pages/pages.jsonl"
    budget = 512 * 1024
    page = b'{"url":"https://example.com/"}\n'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, page.ljust(budget - 30 - len(name), b"\n"))
        for number in range(10):
            archive.writestr(f"indexes/index-{number}.cdxj", b"index\n")

    reads = []

    def profile(frame, event, function):
        if (
            event == "c_call"
            and frame.f_code is presentation._replay_index.__code__
            and getattr(function, "__name__", "") == "read"
        ):
            reads.append(function.__self__.tell())

    previous = sys.getprofile()
    try:
        sys.setprofile(profile)
        index = presentation._replay_index(path)
    finally:
        sys.setprofile(previous)

    assert index["size"] == path.stat().st_size
    assert len(index["ranges"]) == 1
    assert len(base64.b64decode(index["ranges"][0][1])) == budget
    assert reads == [0, 0], reads  # Read its header, then its exact inline bytes.


def test_replay_prefers_requested_page_over_unrelated_first_page(
    tmp_path: Path,
) -> None:
    wacz_path = tmp_path / "capture.wacz"
    with zipfile.ZipFile(wacz_path, "w") as zf:
        zf.writestr(
            "pages/pages.jsonl",
            "\n".join(
                json.dumps(page)
                for page in [
                    {"format": "json-pages-1.0"},
                    {"url": "https://x.com/explore/tabs/for-you"},
                    {"url": "https://x.com/theSquashSH"},
                ]
            ),
        )
    html = presentation.render_replay_html(
        "archivewebpage.wacz",
        "/archivewebpage/archivewebpage.wacz",
        wacz_path=wacz_path,
        fallback_url="https://x.com/theSquashSH",
    )
    assert 'data-url="https://x.com/theSquashSH"' in html


def test_replay_prefers_latest_capture_of_requested_page_including_post(
    tmp_path: Path,
) -> None:
    # A bot-check interstitial (here Cloudflare's) is recorded first as a GET of
    # the URL; the cleared page is a later form POST back to it, which the
    # recorder lists with a __wb_method=POST suffix. Replay the final document:
    # the recorded 307 of that GET to itself would loop forever.
    url = "https://www.science.org/content/article/brain?id=1"
    cleared = f"{url}&__wb_method=POST&7f74c57f=YVIudVq5-1791806206-1.2.1.1"
    wacz_path = tmp_path / "capture.wacz"
    with zipfile.ZipFile(wacz_path, "w") as zf:
        zf.writestr(
            "pages/pages.jsonl",
            "\n".join(
                json.dumps(page, ensure_ascii=False)
                for page in [
                    {"format": "json-pages-1.0"},
                    {
                        "url": url,
                        "ts": "2026-10-12T11:56:46.548Z",
                        "title": "Just a moment...",
                    },
                    {
                        "url": "https://www.science.org/other",
                        "ts": "2026-10-12T11:58:00.000Z",
                    },
                    {
                        "url": cleared,
                        "ts": "2026-10-12T11:57:05.819Z",
                        "title": "Brain | Science",
                        "text": "Article paragraph\u2028Next paragraph\u2029More text\u0085End",
                    },
                ]
            ),
        )
    html = presentation.render_replay_html(
        "archivewebpage.wacz",
        "/archivewebpage/archivewebpage.wacz",
        wacz_path=wacz_path,
        fallback_url=url,
    )
    assert f'data-url="{html_escape(cleared, quote=True)}"' in html


def test_replay_prefers_recorded_final_navigation_over_initial_document(tmp_path):
    original = "https://example.com/article"
    final = original + "?referrer=https%3A%2F%2Fexample.com%2Farticle"
    capture = tmp_path / "archivewebpage" / "archivewebpage.wacz"
    capture.parent.mkdir()
    with zipfile.ZipFile(capture, "w") as archive:
        archive.writestr(
            "pages/pages.jsonl",
            "\n".join(
                json.dumps(page)
                for page in [
                    {"format": "json-pages-1.0"},
                    {
                        "url": "https://example.com/unrelated",
                        "ts": "2026-10-23T17:22:00Z",
                    },
                    {
                        "url": original,
                        "ts": "2026-10-23T17:21:15Z",
                        "title": "Verification",
                    },
                    {
                        "url": final,
                        "ts": "2026-10-23T17:21:24Z",
                        "title": "Original article",
                    },
                ]
            ),
        )
    navigation = tmp_path / "chrome" / "navigation.json"
    navigation.parent.mkdir()
    navigation.write_text(
        json.dumps({"url": original, "finalUrl": final, "status": 200}),
    )
    html = presentation.render_replay_html(
        "archivewebpage.wacz",
        "/archivewebpage/archivewebpage.wacz",
        wacz_path=capture,
        fallback_url=original,
    )
    assert f'data-url="{html_escape(final, quote=True)}"' in html

    # Unrelated, absent, or malformed metadata must preserve ordinary selection.
    for metadata in [
        {"url": "https://example.com/other", "finalUrl": final, "status": 200},
        {
            "url": original,
            "finalUrl": "https://example.com/not-recorded",
            "status": 200,
        },
        {"url": original, "finalUrl": None},
        [],
    ]:
        navigation.write_text(json.dumps(metadata))
        html = presentation.render_replay_html(
            "archivewebpage.wacz",
            "/archivewebpage/archivewebpage.wacz",
            wacz_path=capture,
            fallback_url=original,
        )
        assert f'data-url="{original}"' in html
    navigation.write_text("{")
    html = presentation.render_replay_html(
        "archivewebpage.wacz",
        "/archivewebpage/archivewebpage.wacz",
        wacz_path=capture,
        fallback_url=original,
    )
    assert f'data-url="{original}"' in html


def test_replay_bootstrap_gates_ui_on_worker_and_exposes_readiness(
    tmp_path: Path,
) -> None:
    wacz_path = tmp_path / "capture.wacz"
    pages = [
        {"format": "json-pages-1.0", "id": "pages", "title": "All Pages"},
        {"url": "https://example.com/", "title": "Example Domain"},
    ]
    with zipfile.ZipFile(wacz_path, "w") as zf:
        zf.writestr(
            "pages/pages.jsonl",
            "\n".join(json.dumps(page) for page in pages),
        )

    html = presentation.render_replay_html(
        "archivewebpage.wacz",
        "/archivewebpage/archivewebpage.wacz",
        wacz_path=wacz_path,
        fallback_url="https://fallback.example/",
    )

    assert 'data-source="/archivewebpage/archivewebpage.wacz?replay-index=' in html
    assert 'data-url="https://example.com/"' in html
    assert "navigator.serviceWorker.register(" in html
    assert "customElements.whenDefined('replay-web-page')" in html
    assert ".then(mountReplay)" in html
    assert "noWebWorker" in html
    assert "noCache" in html
    assert "rwp-page-loading" in html
    assert "archivebox-replay-ready" in html
    assert "openBrowser.href = window.location.href" in html
    assert "openBrowser.target = '_blank'" in html
    assert "openBrowser.rel = 'external noopener'" in html
    assert "openBrowser.textContent = 'Open in browser'" in html
    assert "You must view this snapshot in a browser." in html
    assert (
        "For technical reasons, WACZ requires service workers to view, and some platforms limit where service workers are available. "
        "You may need to make sure your ArchiveBox server is hosted with HTTPS and open this output in your default browser instead of within this app."
    ) in html
    assert (
        "#replay-root.replay-unavailable { box-sizing:border-box;min-height:100vh;display:grid;place-items:center"
        in html
    )
    assert '<script src="/replay/ui.js"></script>' in html
    assert "#replay-root, replay-web-page" in html

    onedomain_html = presentation.render_replay_html(
        "archivewebpage.wacz",
        "/snapshot/06a219240eb5778d8000f850baa5d427/archivewebpage/archivewebpage.wacz",
        wacz_path=wacz_path,
    )

    assert (
        '<script src="/snapshot/06a219240eb5778d8000f850baa5d427/replay/ui.js"></script>'
        in onedomain_html
    )
    assert (
        "scope: '/snapshot/06a219240eb5778d8000f850baa5d427/replay/'" in onedomain_html
    )
    assert (
        'data-replaybase="/snapshot/06a219240eb5778d8000f850baa5d427/replay/"'
        in onedomain_html
    )


def test_replay_index_contains_exact_metadata_but_no_warc_response_bytes():
    path = Path(__file__).parent / "fixtures/replay/iframetest.wacz"
    raw = path.read_bytes()
    index = presentation._replay_index(path)
    assert index["size"] == len(raw)
    ranges = [
        (offset, base64.b64decode(encoded)) for offset, encoded in index["ranges"]
    ]
    assert 0 < sum(len(data) for _, data in ranges) <= 512 * 1024
    with zipfile.ZipFile(path) as archive:
        for offset, data in ranges:
            assert data == raw[offset : offset + len(data)]
            for entry in archive.infolist():
                if not entry.filename.startswith("archive/"):
                    continue
                name_size, extra_size = struct.unpack_from(
                    "<HH",
                    raw,
                    entry.header_offset + 26,
                )
                start = entry.header_offset + 30 + name_size + extra_size
                end = start + entry.compress_size
                assert offset + len(data) <= start or offset >= end
        for name in ("pages/pages.jsonl", "datapackage.json"):
            entry = archive.getinfo(name)
            assert any(
                offset == entry.header_offset and len(data) > entry.compress_size
                for offset, data in ranges
            )


@pytest.mark.parametrize("scheme", ["auto", "light", "dark"])
@pytest.mark.parametrize("preference", ["light", "dark"])
def test_replay_canvas_preserves_recorded_color_scheme(
    tmp_path,
    httpserver,
    require_chrome_runtime,
    scheme,
    preference,
):
    """Check composited pixels: transparent dark documents can have white text on white."""
    from playwright.sync_api import sync_playwright
    from werkzeug.wrappers import Response
    from abx_plugins.plugins.base.testing import install_required_binary_from_config
    from abx_plugins.plugins.base.utils import load_config

    plugin_dir = Path(presentation.__file__).parent
    install_required_binary_from_config(
        plugin_dir,
        "archivewebpage",
        env=dict(os.environ),
    )
    config = load_config(plugin_dir / "config.json", hydrate_binaries=False)
    fixture = Path(__file__).parent / f"fixtures/replay/color-scheme-{scheme}.wacz"
    original = fixture.read_bytes()
    source = "/archivewebpage/capture.wacz"
    for asset in ("ui.js", "sw-index.js", "sw.js", "replay.html"):
        response = presentation.serve_replay_asset(f"replay/{asset}", config)
        assert response is not None
        body, content_type, headers = response
        httpserver.expect_request(f"/replay/{asset}").respond_with_data(
            body,
            content_type=content_type,
            headers=headers,
        )
    viewer = presentation.render_replay_html("capture.wacz", source, fixture)

    def serve_capture(request):
        if request.args.get("preview") == "1":
            return Response(viewer, content_type="text/html")
        if byte_range := request.headers.get("Range"):
            start, end = byte_range.removeprefix("bytes=").split("-")
            first = int(start) if start else max(0, len(original) - int(end))
            last = (
                min(int(end), len(original) - 1) if start and end else len(original) - 1
            )
            return Response(
                original[first : last + 1],
                status=206,
                headers={
                    "Content-Range": f"bytes {first}-{last}/{len(original)}",
                    "Accept-Ranges": "bytes",
                },
                content_type="application/wacz",
            )
        return Response(
            original,
            content_type="application/wacz",
            headers={"Accept-Ranges": "bytes"},
        )

    httpserver.expect_request(source).respond_with_handler(serve_capture)
    httpserver.expect_request("/").respond_with_data(
        f'<!doctype html><iframe id="replay" src="{source}?preview=1" style="width:100%;height:90vh"></iframe>'
        '<script>window.painted=false;addEventListener("message",event=>{'
        'if(event.source===document.getElementById("replay").contentWindow && '
        'event.data.type==="archivebox-replay-painted") painted=true;});</script>',
        content_type="text/html",
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=os.environ["CHROME_BINARY"],
            args=["--no-sandbox"],
        )
        page = browser.new_page(
            color_scheme=preference,
            viewport={"width": 800, "height": 600},
        )
        timings = {}
        ui_requests = 0
        for load in ("cold", "warm"):
            response = (
                page.goto(httpserver.url_for("/"), wait_until="domcontentloaded")
                if load == "cold"
                else page.reload(wait_until="domcontentloaded")
            )
            assert response is not None
            timings[load] = (
                response.request.timing["responseStart"]
                - response.request.timing["requestStart"]
            )
            page.wait_for_function("window.painted")
            replay = next(frame for frame in page.frames if "/replay/w/" in frame.url)
            assert replay.locator("p").inner_text() == "Readable archived text"
            screenshot = replay.locator("body").screenshot(
                path=str(tmp_path / f"replay-canvas-{load}.png"),
            )
            # Decode the actual screenshot using the browser's image decoder. DOM
            # colors alone miss transparent iframe compositing onto the host UI.
            pixels = page.evaluate(
                """async encoded => {
                const bytes = Uint8Array.from(atob(encoded), char => char.charCodeAt(0));
                const bitmap = await createImageBitmap(new Blob([bytes], {type: 'image/png'}));
                const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
                const context = canvas.getContext('2d');
                context.drawImage(bitmap, 0, 0);
                return [[48, 48], [120, 120]].map(([x,y]) => Array.from(context.getImageData(x,y,1,1).data));
            }""",
                base64.b64encode(screenshot).decode(),
            )
            (tmp_path / f"pixels-{load}.json").write_text(json.dumps(pixels))
            ink, canvas = pixels
            dark = scheme == "dark" or (scheme == "auto" and preference == "dark")
            assert all(value > 200 if dark else value < 50 for value in ink[:3]), pixels
            assert all(value < 50 if dark else value > 200 for value in canvas[:3]), (
                pixels
            )
            assert sum(abs(a - b) for a, b in zip(ink[:3], canvas[:3])) > 500, pixels
            assert fixture.read_bytes() == original
            requests = sum(
                request.path == "/replay/ui.js" for request, _ in httpserver.log
            )
            if load == "cold":
                assert requests > 0
                ui_requests = requests
            else:
                # The fix lives in the current preview, so an immutable cached UI
                # bundle must not prevent already-viewed captures from recovering.
                assert requests == ui_requests
        (tmp_path / "ttfb-ms.json").write_text(json.dumps(timings))
        browser.close()

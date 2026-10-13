from __future__ import annotations

import json
import base64
from html import escape as html_escape
import struct
import sys
import zipfile
from pathlib import Path

from abx_plugins.plugins.archivewebpage import presentation


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
                json.dumps(page)
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

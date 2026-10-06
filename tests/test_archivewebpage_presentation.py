from __future__ import annotations

import json
import zipfile
from pathlib import Path

from abx_plugins.plugins.archivewebpage import presentation


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

    assert 'data-source="/archivewebpage/archivewebpage.wacz"' in html
    assert 'data-url="https://example.com/"' in html
    assert "navigator.serviceWorker.register(" in html
    assert "customElements.whenDefined('replay-web-page')" in html
    assert ".then(mountReplay)" in html
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

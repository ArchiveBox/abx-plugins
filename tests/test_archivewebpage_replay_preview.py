from __future__ import annotations

import json
import hashlib
import os
import zipfile
from argparse import Namespace
from pathlib import Path

from abx_plugins.plugins.archivewebpage import replay_preview
from abx_plugins.plugins.base.testing import install_required_binary_from_config


def test_replay_backport_leaves_installed_recorder_unchanged(tmp_path: Path) -> None:
    env = {**os.environ, "ABXPKG_LIB_DIR": str(tmp_path / "lib")}
    installed = install_required_binary_from_config(
        Path(replay_preview.__file__).parent,
        "archivewebpage",
        env=env,
    )
    assert installed.abspath
    config = Namespace(ABXPKG_LIB_DIR=env["ABXPKG_LIB_DIR"])
    extension = replay_preview.find_extension_dir(config)
    assert extension is not None
    worker = extension / "sw.js"
    original = worker.read_bytes()
    result = replay_preview.serve_replay_asset("replay/sw.js", config)
    assert result is not None
    body, content_type, headers = result
    # This exact worker passed the real cookie-free WACZ replay regression.
    assert hashlib.sha256(body).hexdigest() == (
        "b5b66bd04eddae3342c53441fdfb1504193b38f9fa496d3767eaab90c4c4b68e"
    )
    assert worker.read_bytes() == original
    assert replay_preview.serve_replay_asset("replay/sw.js", config) == result
    assert content_type == "application/javascript; charset=utf-8"
    assert headers["Cache-Control"] == "no-cache"
    assert headers["ETag"] == f'"{hashlib.sha256(body).hexdigest()}"'
    assert "Last-Modified" not in headers


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
    html = replay_preview.render_preview_html(
        "archivewebpage.wacz",
        "/archivewebpage/archivewebpage.wacz",
        wacz_path=wacz_path,
        fallback_url="https://x.com/theSquashSH",
    )
    assert 'data-url="https://x.com/theSquashSH"' in html


def test_replay_preview_bootstrap_gates_ui_on_worker_and_exposes_readiness(
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

    html = replay_preview.render_preview_html(
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
    assert '<script src="/replay/ui.js"></script>' in html
    assert "#replay-root, replay-web-page" in html

    onedomain_html = replay_preview.render_preview_html(
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

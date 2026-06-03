from __future__ import annotations

import json
import zipfile
from pathlib import Path

from abx_plugins.plugins.archivewebpage import replay_preview


def test_replay_preview_bootstrap_always_appends_ui_after_service_worker_wait(
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

    assert 'source="/archivewebpage/archivewebpage.wacz"' in html
    assert 'url="https://example.com/"' in html
    assert "withTimeout(" in html
    assert "navigator.serviceWorker.register(" in html
    assert "setTimeout(resolve, 3000)" in html
    assert "appendReplayUi();\n        if ('serviceWorker' in navigator)" in html
    assert "s.src = '/replay/ui.js'" in html

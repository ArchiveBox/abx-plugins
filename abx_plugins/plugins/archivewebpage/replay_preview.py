"""ArchiveBox-side helpers for serving an in-browser WACZ/WARC replay viewer.

This module is consulted by ``archivebox.core.views._serve_snapshot_replay``
and ``archivebox.misc.serve_static`` via conditional imports. archivebox does
not depend on this plugin at all — if the import fails the relevant code
paths just fall through to default static-file serving.

The viewer is fully hermetic: every asset (``ui.js``, ``sw.js``, the embed
HTML, and the WACZ itself) is served from the snapshot host. We rely on the
``archivewebpage`` Chrome extension already being installed via the
chromewebstore provider — that extension ships pre-built ``ui.js`` and
``sw.js`` bundles (replayweb.page UI + wabac service worker) we can reuse.
"""

from __future__ import annotations

import json
import mimetypes
import zipfile
from pathlib import Path

from django import template
from django.http import HttpResponse
from django.utils.http import http_date

# Plugin install pins ``--name=archivewebpage`` in config.json, so the
# unpacked extension dir is always ``*__archivewebpage``.
_EXTENSION_NAME = "archivewebpage"

# Mapping of URL tail under ``/replay/`` to filename inside the unpacked
# archivewebpage extension. The replayweb.page replay frame (intercepted by
# the registered service worker) is what the iframe actually loads; ``sw.js``
# and ``ui.js`` are the static assets it boots from.
_REPLAY_ASSETS = {
    "sw.js": "sw.js",
    "ui.js": "ui.js",
}

_PLUGIN_DIR = Path(__file__).resolve().parent


def is_replay_target(filename_or_path: str) -> bool:
    """Return True for a path the plugin renders an embedded replay for."""
    name = filename_or_path.lower()
    return name.endswith((".wacz", ".warc", ".warc.gz"))


def find_extension_dir(config) -> Path | None:
    """Resolve the unpacked archivewebpage chrome extension on disk."""
    ext_root = Path(config.PERSONAS_DIR) / config.ACTIVE_PERSONA / "chrome_extensions"
    for child in ext_root.glob(f"*__{_EXTENSION_NAME}"):
        return child
    return None


def serve_replay_asset(rel_path: str, config) -> HttpResponse | None:
    """Serve ``/replay/{sw,ui}.js`` from the locally installed extension."""
    asset = rel_path.removeprefix("replay/").removeprefix("replay").lstrip("/")
    target = _REPLAY_ASSETS.get(asset)
    if target is None:
        return None
    ext_dir = find_extension_dir(config)
    if ext_dir is None:
        return None
    file_path = ext_dir / target
    if not file_path.is_file():
        return None

    body = file_path.read_bytes()
    content_type = (
        "application/javascript; charset=utf-8"
        if file_path.suffix == ".js"
        else mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    )
    response = HttpResponse(body, content_type=content_type)
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    response.headers["Last-Modified"] = http_date(file_path.stat().st_mtime)
    if file_path.name == "sw.js":
        # Allow the SW to claim the whole snapshot host scope rather than
        # only ``/replay/``; replayweb.page intercepts arbitrary in-archive
        # URLs once the worker activates.
        response.headers["Service-Worker-Allowed"] = "/"
    return response


def _first_archived_url(wacz_path: Path) -> str:
    """Extract the first archived URL from the WACZ's pages index.

    The snapshot's ``Snapshot.url`` is the URL the user *asked* to archive,
    but the WACZ records whatever URL the browser actually navigated to (e.g.
    the final URL after redirects). replayweb.page's ``url=`` param has to
    match an entry in ``pages/pages.jsonl`` exactly, so use the recorded one.
    """
    try:
        with zipfile.ZipFile(wacz_path) as zf:
            for name in ("pages/pages.jsonl", "pages/extraPages.jsonl"):
                try:
                    raw = zf.read(name).decode("utf-8", errors="replace")
                except KeyError:
                    continue
                for line in raw.splitlines():
                    line = line.strip()
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("format") == "json-pages-1.0":
                        continue  # header row, not a real page entry
                    url = record.get("url")
                    if url:
                        return url
    except (zipfile.BadZipFile, OSError):
        pass
    return ""


def render_preview_html(
    filename: str,
    output_path: str,
    wacz_path: Path | None = None,
    fallback_url: str = "",
) -> str:
    """Render the plugin's ``full.html`` template as the WACZ preview body.

    If a WACZ path is provided, the first archived URL from its pages index
    is used so replayweb.page can land directly in replay mode. Falls back to
    ``fallback_url`` (typically ``Snapshot.url``) if the WACZ has no readable
    pages index.
    """
    archived_url = ""
    if wacz_path is not None and wacz_path.suffix.lower() == ".wacz":
        archived_url = _first_archived_url(wacz_path)
    if not archived_url:
        archived_url = fallback_url or ""

    template_path = _PLUGIN_DIR / "templates" / "full.html"
    template_str = template_path.read_text(encoding="utf-8")
    tpl = template.Engine(debug=False).from_string(template_str)
    return tpl.render(
        template.Context(
            {
                "output_path": output_path,
                "output_path_raw": filename,
                "archived_url": archived_url,
                "plugin": _EXTENSION_NAME,
            },
        ),
    )


def preview_response_headers() -> dict[str, str]:
    """Headers ArchiveBox should attach to the rendered preview HTML."""
    return {
        "Content-Security-Policy": (
            "default-src 'self' data: blob:; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self' data: blob:; "
            "img-src 'self' data: blob:; "
            "frame-src 'self' data: blob:; "
            "worker-src 'self' blob:; "
            "object-src 'none'; "
            "base-uri 'none';"
        ),
        "X-Content-Type-Options": "nosniff",
    }

"""Helpers for serving an in-browser WACZ/WARC replay viewer.

The viewer is fully hermetic: every asset (``ui.js``, ``sw.js``, the embed
HTML, and the WACZ itself) is served from the snapshot host. We rely on the
``archivewebpage`` Chrome extension already being installed via the
chromewebstore provider — that extension ships pre-built ``ui.js`` and
``sw.js`` bundles (replayweb.page UI + wabac service worker) we can reuse.
"""

from __future__ import annotations

import json
import base64
import hashlib
import mimetypes
import struct
from email.utils import formatdate
from functools import lru_cache
from html import escape as html_escape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import zipfile
from pathlib import Path

# Plugin install pins ``--name=archivewebpage`` in config.json, so the
# unpacked extension dir is always ``*__archivewebpage``.
_EXTENSION_NAME = "archivewebpage"
ReplayAssetResponse = tuple[bytes, str, dict[str, str]]

# Mapping of URL tail under ``/replay/`` to filename inside the unpacked
# archivewebpage extension. The replayweb.page replay frame (intercepted by
# the registered service worker) is what the iframe actually loads; ``sw.js``
# and ``ui.js`` are the static assets it boots from.
_REPLAY_ASSETS = {
    "sw.js": "sw.js",
    "sw-index.js": "sw.js",
    "ui.js": "ui.js",
}

# Static fallbacks for the two bootstrap iframes the replayweb.page SW
# normally synthesizes (see ``sw.js`` near "replay.html"/"record.html"). The
# iframe ``<replay-web-page>`` creates points at ``/replay/replay.html?...``
# *before* the SW has finished activating, so without these the first iframe
# fetch 404s and ui.js silently reloads the iframe up to twice (~200ms each)
# while waiting for the SW to claim its scope. Serving the same HTML the SW
# would synthesize makes the first iframe load succeed regardless of SW
# lifecycle state — once the SW is active it intercepts these paths itself
# and our handler is never reached.
_REPLAY_STATIC_HTML = {
    "replay.html": (
        '<!doctype html>\n<html class="no-overflow">\n  <head>\n'
        "    <title>ReplayWeb.page</title>\n"
        '    <meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        '    <script src="./ui.js"></script>\n'
        "  </head>\n  <body>\n    <replay-app-main></replay-app-main>\n  </body>\n</html>\n"
    ),
    "record.html": (
        "<!doctype html>\n<html>\n  <head>\n"
        '    <meta charset="utf-8">\n'
        '    <script src="ui.js"></script>\n'
        "  </head>\n  <body>\n    <archive-web-page-app></archive-web-page-app>\n  </body>\n</html>\n"
    ),
}

_PLUGIN_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def _replay_worker_body(body: bytes) -> bytes:
    # The official 0.17.0 worker embeds Wombat's Storage proxy. Archived apps
    # can remove Object.prototype.__proto__; use actual prototype lookup so
    # their hardening does not disable localStorage/sessionStorage in replay.
    # Fail visibly on a dependency change instead of editing an unknown bundle.
    if (
        hashlib.sha256(body).hexdigest()
        != "eb449aec15884416b045f3e21d2f5c88db840968a38ccf1b6b12e1c7e587cec9"
    ):
        raise RuntimeError(
            "ArchiveWeb.page replay storage fix requires the pinned 0.17.0 worker bundle",
        )
    for before, after in (
        (
            b"var proto = target.__proto__;",
            b"var proto = Object.getPrototypeOf(target);",
        ),
        (
            b"(proto.__proto__ && proto.__proto__.hasOwnProperty(prop))",
            b"(Object.getPrototypeOf(proto) && Object.getPrototypeOf(proto).hasOwnProperty(prop))",
        ),
        (
            b"if (target.__proto__.hasOwnProperty(prop))",
            b"if (Object.getPrototypeOf(target).hasOwnProperty(prop))",
        ),
    ):
        if body.count(before) != 1:
            raise RuntimeError(
                "ArchiveWeb.page replay storage fix does not match the pinned worker bundle",
            )
        body = body.replace(before, after)
    return body


def is_replay_target(filename_or_path: str) -> bool:
    """Return True for a path the plugin renders an embedded replay for."""
    name = filename_or_path.lower()
    return name.endswith((".wacz", ".warc", ".warc.gz"))


def find_extension_dir(config) -> Path | None:
    """Resolve the unpacked archivewebpage chrome extension on disk."""
    ext_root = (
        Path(config.ABXPKG_LIB_DIR).expanduser() / "chromewebstore" / "extensions"
    )
    for child in ext_root.glob(f"*__{_EXTENSION_NAME}"):
        return child
    return None


def serve_replay_asset(rel_path: str, config) -> ReplayAssetResponse | None:
    """Serve ``/replay/{sw,ui}.js`` from the locally installed extension."""
    if rel_path == "replay":
        asset = ""
    elif rel_path.startswith("replay/"):
        asset = rel_path[len("replay/") :]
    else:
        asset = rel_path

    static_html = _REPLAY_STATIC_HTML.get(asset)
    if static_html is not None:
        return (
            static_html.encode("utf-8"),
            "text/html; charset=utf-8",
            {"Cache-Control": "public, max-age=31536000, immutable"},
        )

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
    if target == "sw.js":
        body = (
            _replay_worker_body(body)
            + b"\n"
            + (_PLUGIN_DIR / "replay_index.js").read_bytes()
        )
    content_type = (
        "application/javascript; charset=utf-8"
        if file_path.suffix == ".js"
        else mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    )
    headers = {
        "Cache-Control": "public, max-age=31536000, immutable",
        "Last-Modified": formatdate(file_path.stat().st_mtime, usegmt=True),
    }
    if file_path.name == "sw.js":
        # Allow the SW to claim the whole snapshot host scope rather than
        # only ``/replay/``; replayweb.page intercepts arbitrary in-archive
        # URLs once the worker activates.
        headers["Service-Worker-Allowed"] = "/"
    return body, content_type, headers


def serve_replay_asset_response(rel_path: str, config, response_factory):
    """Build a framework response for an archivewebpage /replay/* viewer asset."""
    replay_asset = serve_replay_asset(rel_path, config)
    if replay_asset is None:
        return None

    body, content_type, headers = replay_asset
    response = response_factory(body, content_type=content_type)
    for key, value in headers.items():
        response.headers[key] = value
    return response


def _page_request_url(url: str) -> str:
    """Drop the recorder's ``__wb_method=POST&<body>`` suffix from a page URL."""
    for marker in ("?__wb_method=", "&__wb_method="):
        if marker in url:
            return url.split(marker, 1)[0]
    return url


def _first_archived_url(wacz_path: Path, preferred_url: str = "") -> str:
    """Prefer the latest capture of the requested page, otherwise the first page.

    The snapshot's ``Snapshot.url`` is the URL the user *asked* to archive,
    but the WACZ records whatever URL the browser actually navigated to (e.g.
    the final URL after redirects). replayweb.page's ``url=`` param has to
    match a recorded page. Page index order is not navigation order: an
    incidental page can appear before the requested page.

    Optional Chrome navigation metadata identifies the final redirected page.
    Ignore missing, malformed, unrelated, or unrecorded navigation metadata.

    One URL can be recorded more than once: a bot-check interstitial is the
    first GET of it, and the page that cleared it may be a later form POST back
    to it (listed with a ``__wb_method=POST`` suffix). Replay the latest capture,
    the document the browser settled on.
    """
    preferred_urls = [preferred_url]
    # Navigation records the settled document before later scrolling can visit
    # unrelated pages. Use it only for this capture and only if the WACZ has it.
    try:
        navigation = json.loads(
            (wacz_path.parent.parent / "chrome/navigation.json").read_text(),
        )
        if (
            isinstance(navigation, dict)
            and navigation.get("url") == preferred_url
            and isinstance(navigation.get("finalUrl"), str)
        ):
            preferred_urls.insert(0, navigation["finalUrl"])
    except (OSError, ValueError):
        pass
    first_url = ""
    latest = {}
    try:
        with zipfile.ZipFile(wacz_path) as zf:
            for name in ("pages/pages.jsonl", "pages/extraPages.jsonl"):
                try:
                    raw = zf.read(name).decode("utf-8", errors="replace")
                except KeyError:
                    continue
                # JSONL records end at LF; Unicode separators can occur inside
                # recorded page text and must not split an otherwise valid row.
                for line in raw.split("\n"):
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
                        request_url = _page_request_url(url)
                        if request_url in preferred_urls:
                            ts = str(record.get("ts") or "")
                            if (
                                request_url not in latest
                                or ts >= latest[request_url][0]
                            ):
                                latest[request_url] = (ts, url)
                        if not first_url:
                            first_url = url
    except (zipfile.BadZipFile, OSError):
        pass
    return next((latest[url][1] for url in preferred_urls if url in latest), first_url)


def _replay_index(wacz_path: Path) -> dict:
    """Inline bounded ZIP headers and indexes, never archived response bytes.

    Wabac normally fetches these small ranges sequentially over the network.
    Supplying the exact compressed index bytes with the viewer preserves its
    ZIP parser/hash verification and leaves WARC response reads on demand.
    """
    limit = 512 * 1024
    try:
        before = wacz_path.stat()
        ranges = []
        with zipfile.ZipFile(wacz_path) as archive, wacz_path.open("rb") as source:
            for entry in archive.infolist():
                if limit < 30:
                    break  # No local header fits; do not read the remaining entries.
                source.seek(entry.header_offset)
                header = source.read(30)
                if len(header) != 30 or header[:4] != b"PK\x03\x04":
                    return {}
                name_size, extra_size = struct.unpack_from("<HH", header, 26)
                length = 30 + name_size + extra_size
                is_index = entry.filename in {
                    "datapackage.json",
                    "datapackage-digest.json",
                } or entry.filename.startswith(("pages/", "indexes/"))
                if is_index and length + entry.compress_size <= limit:
                    length += entry.compress_size
                if length > limit:
                    break
                source.seek(entry.header_offset)
                data = source.read(length)
                ranges.append(
                    [entry.header_offset, base64.b64encode(data).decode("ascii")],
                )
                limit -= length
            directory_size = before.st_size - archive.start_dir
            if 0 < directory_size <= limit:
                source.seek(archive.start_dir)
                ranges.append(
                    [
                        archive.start_dir,
                        base64.b64encode(source.read()).decode("ascii"),
                    ],
                )
        after = wacz_path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            return {}
        return {
            "size": before.st_size,
            "version": f"{before.st_mtime_ns}-{before.st_size}",
            "ranges": ranges,
        }
    except (OSError, ValueError, zipfile.BadZipFile):
        return {}


def _replay_base_for_output_path(output_path: str) -> str:
    """Return the route prefix used to serve replayweb.page assets.

    Subdomain replay serves snapshot files from the host root, so ``/replay/``
    is correct. One-domain replay serves the same files under
    ``/snapshot/<id>/...``; in that mode the service worker and ui.js must be
    loaded from ``/snapshot/<id>/replay/`` so they route back through
    ``SnapshotReplayView`` instead of escaping to a nonexistent root route.
    """
    path = urlsplit(output_path or "").path
    marker = "/archivewebpage/"
    if marker in path:
        prefix = path.split(marker, 1)[0].rstrip("/")
        if prefix:
            return f"{prefix}/replay/"
    return "/replay/"


def render_replay_html(
    filename: str,
    output_path: str,
    wacz_path: Path | None = None,
    fallback_url: str = "",
) -> str:
    """Render the plugin's private replay template for a saved WACZ.

    ``templates/full.html`` owns the replay document. This response hook limits
    it to replay archives and supplies the inspected page URL and replay asset
    base. Other outputs from the plugin, such as ``recording.json``, continue
    through their ordinary MIME-specific renderer.

    Prefer the recorded final navigation for ``fallback_url``, then that URL,
    over incidental pages in the WACZ. Otherwise use the first archived URL.
    Falls back to
    ``fallback_url`` (typically ``Snapshot.url``) if the WACZ has no readable
    pages index.
    """
    archived_url = ""
    if wacz_path is not None and wacz_path.suffix.lower() == ".wacz":
        archived_url = _first_archived_url(wacz_path, fallback_url)
    if not archived_url:
        archived_url = fallback_url or ""

    replay_index = (
        _replay_index(wacz_path)
        if wacz_path and wacz_path.suffix.lower() == ".wacz"
        else {}
    )
    if replay_index:
        # Separate old body-caching collections and invalidate indexes when a
        # capture is replaced. The raw endpoint ignores this version parameter.
        source = urlsplit(output_path)
        query = dict(parse_qsl(source.query))
        query["replay-index"] = replay_index["version"]
        output_path = urlunsplit(source._replace(query=urlencode(query)))
    replay_base = _replay_base_for_output_path(output_path)
    archived_url_attr = (
        f'url="{html_escape(archived_url, quote=True)}"' if archived_url else ""
    )
    return (
        (_PLUGIN_DIR / "templates" / "full.html")
        .read_text(encoding="utf-8")
        .replace(
            '{% if archived_url %}url="{{ archived_url }}"{% endif %}',
            archived_url_attr,
        )
        .replace("{{ output_path_raw }}", html_escape(filename, quote=True))
        .replace("{{ output_path }}", html_escape(output_path, quote=True))
        .replace("{{ archived_url }}", html_escape(archived_url, quote=True))
        .replace("{{ replay_base }}", html_escape(replay_base, quote=True))
        .replace("{{ replay_index }}", json.dumps(replay_index, separators=(",", ":")))
    )


def render_full_response(
    filename: str,
    output_path: str,
    *,
    wacz_path: Path | None = None,
    fallback_url: str = "",
    last_modified: str = "",
    etag: str = "",
    cache_control: str = "",
    content_encoding: str = "",
) -> tuple[str, str, dict[str, str]] | None:
    if not is_replay_target(filename):
        return None

    headers = {
        "Content-Disposition": f'inline; filename="{Path(filename).stem}.html"',
        **replay_response_headers(),
    }
    if last_modified:
        headers["Last-Modified"] = last_modified
    if etag:
        headers["ETag"] = etag
    if cache_control:
        headers["Cache-Control"] = cache_control
    if content_encoding:
        headers["Content-Encoding"] = content_encoding
    return (
        render_replay_html(
            filename,
            output_path,
            wacz_path=wacz_path,
            fallback_url=fallback_url,
        ),
        "text/html; charset=utf-8",
        headers,
    )


def replay_response_headers() -> dict[str, str]:
    """Headers ArchiveBox should attach to the rendered replay HTML."""
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

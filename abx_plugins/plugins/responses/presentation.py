import base64
import hashlib
import html
import json
import posixpath
import re
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urljoin, urlsplit

from abx_plugins.plugins.base.html import Resources
from typing import Any


def extra_snapshot_output_cards(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the Responses HTML card alongside the plugin's media gallery."""
    for output in outputs:
        if output["name"] != "responses":
            continue
        path = str(output.get("path") or "")
        if not path.startswith("responses/") or ".." in PurePosixPath(path).parts:
            continue
        files = output.get("output_files", {})
        info = files.get(path) or files.get(path.removeprefix("responses/")) or {}
        mime = str(info.get("mimetype") or "").split(";")[0].lower()
        if not info or info.get("size", 0) <= 0:
            continue
        if mime not in {"text/html", "application/xhtml+xml"} and PurePosixPath(
            path,
        ).suffix.lower() not in {".html", ".htm"}:
            continue
        return [
            {
                "name": "responses_html",
                "path": path,
                "folder_path": "responses",
                "ts": output.get("ts"),
                "size": 0,
                "result": None,
                "direct_preview_path": f"{path}?card=responses_html",
                "output_group": "html",
                "output_order": 1000,
            },
        ]
    return []


def _encoded_responses_image_url(image_url: str, page_url: str | None) -> str | None:
    raw_url = str(image_url or "").strip()
    if not raw_url or raw_url.startswith(
        ("#", "data:", "blob:", "about:", "javascript:"),
    ):
        return None

    absolute_url = urljoin(page_url or "", raw_url)
    if not absolute_url.startswith(("http://", "https://")):
        return None

    return quote(absolute_url, safe="").replace("%", "_")


def _saved_response_records(index: Path) -> list[dict]:
    records = []
    with index.open(errors="replace") as lines:
        for line in lines:
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
            except ValueError:
                continue
    return records


def _index_responses_paths_for_html_images(
    snapshot_root: Path,
    html_rel_path: str,
    encoded_urls: set[str],
    records: list[dict] | None = None,
) -> dict[str, str]:
    responses_root = snapshot_root / "responses"
    if not encoded_urls or not responses_root.is_dir():
        return {}

    best_matches: dict[str, str] = {}
    image_matches: set[str] = set()
    # Optional responses plugin file contract: index.jsonl records contain method,
    # url, status, and a path relative to responses/. No plugin code is imported.
    # Legacy captures without an index still use the filename lookup below.
    # The index retains complete URLs even when response filenames are shortened.
    index = responses_root / "index.jsonl"
    if index.is_file():
        resolved_root = responses_root.resolve()
        if records is None:
            records = _saved_response_records(index)
        for record in records:
            try:
                if record.get("method") != "GET" or record.get("status") != 200:
                    continue
                urls = {_encoded_responses_image_url(record.get("url", ""), None)}
                # Saved HTML still references the original request after a CDN
                # redirect. Its recorded GET alias must resolve to the same body.
                if (record.get("requestMethod") or record.get("method")) == "GET":
                    urls.add(
                        _encoded_responses_image_url(
                            record.get("requestUrl", ""),
                            None,
                        ),
                    )
                matches = encoded_urls & urls
                if not matches:
                    continue
                candidate = (responses_root / record.get("path", "")).resolve()
                if (
                    not candidate.is_relative_to(resolved_root)
                    or not candidate.is_file()
                ):
                    continue
                for encoded in matches:
                    best_matches[encoded] = posixpath.relpath(
                        candidate,
                        start=snapshot_root / posixpath.dirname(html_rel_path),
                    )
                    image_matches.add(encoded)
            except (ValueError, TypeError, AttributeError):
                continue
        # The index lists every saved response, so walking responses/ (thousands
        # of files on possibly-remote archive storage, on every HTML page view)
        # is only for legacy captures that predate it.
        return best_matches
    image_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"}
    for candidate in responses_root.rglob("*"):
        if not candidate.is_file():
            continue
        try:
            rel_path = candidate.relative_to(snapshot_root)
        except ValueError:
            continue
        relative_match = posixpath.relpath(
            rel_path.as_posix(),
            start=posixpath.dirname(html_rel_path) or ".",
        )
        candidate_name = candidate.name
        is_image = candidate.suffix.lower() in image_suffixes
        for encoded_url in encoded_urls:
            if (
                encoded_url in image_matches
                or f"__GET__{encoded_url}" not in candidate_name
            ):
                continue
            best_matches[encoded_url] = relative_match
            if is_image:
                # Preserve the old rglob behavior: the first image match wins,
                # otherwise the last matching response file is used.
                image_matches.add(encoded_url)
    return best_matches


def _rewrite_html_image_sources_to_responses(
    html_text: str,
    snapshot_root: Path,
    html_rel_path: str,
    page_url: str | None,
    replay_url: str | None = None,
    source_html: str | None = None,
) -> tuple[str, int]:
    # Parse real start tags, preserving script/comment data and untouched bytes.
    resources = Resources(html_text)
    resources.feed(html_text)
    spans = resources.spans
    inserted_sources = resources.inserted_sources
    background_styles = resources.background_styles
    stylesheet_integrities = resources.stylesheet_integrities
    base_href = resources.base_href
    if not any(
        src and not src.startswith(("#", "data:", "blob:", "about:", "javascript:"))
        for _, _, src in spans
    ):
        return html_text, 0

    records = None
    index = snapshot_root / "responses" / "index.jsonl"
    if index.is_file():
        records = _saved_response_records(index)
        for record in records:
            # Response HTML can be a different page from the snapshot URL.
            if (
                posixpath.normpath("responses/" + str(record.get("path", "")))
                == html_rel_path
            ):
                page_url = record.get("url") or page_url
    page_url = urljoin(page_url or "", base_href) if base_href else page_url
    encoded_urls_by_src = {
        src: _encoded_responses_image_url(src, page_url) for _, _, src in spans
    }
    wanted = {encoded for encoded in encoded_urls_by_src.values() if encoded}
    if not wanted:
        return html_text, 0

    # Optional dom plugin artifact: dom/output.html. Only image src and
    # data-canonical-src attributes are read; no scripts or plugin code execute.
    # Absence of this artifact simply disables proxy-to-original aliases.
    aliases: dict[str, str] = {}

    class ImageAliases(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            original = attrs.get("data-canonical-src")
            proxy = attrs.get("src")
            if tag == "img" and original and proxy:
                original_key = _encoded_responses_image_url(original, page_url)
                proxy_key = _encoded_responses_image_url(proxy, page_url)
                if original_key and proxy_key:
                    aliases[original_key] = proxy_key

    dom = snapshot_root / "dom" / "output.html"
    if dom.is_file() and dom.stat().st_size <= 10 * 1024 * 1024:
        # Reuse the original decoded DOM when serving that very file. Large DOMs
        # otherwise get read/decoded twice per request. Display text may already
        # be HTML-unescaped or rendered Markdown, so it is not an alias source.
        dom_html = (
            source_html
            if html_rel_path == "dom/output.html" and source_html is not None
            else dom.read_text(errors="replace")
        )
        ImageAliases().feed(dom_html)
    response_paths = _index_responses_paths_for_html_images(
        snapshot_root,
        html_rel_path,
        wanted | {aliases[key] for key in wanted if key in aliases},
        records=records,
    )
    rewrites = 0

    edits = []
    for start, end, resource_url in spans:
        encoded_url = encoded_urls_by_src.get(resource_url)
        local_path = response_paths.get(encoded_url or "") or response_paths.get(
            aliases.get(encoded_url or "", ""),
        )
        if not local_path:
            continue
        stylesheet_body, changed_stylesheet = "", 0
        if start in stylesheet_integrities:
            stylesheet = snapshot_root / posixpath.normpath(
                posixpath.join(posixpath.dirname(html_rel_path), local_path),
            )
            if stylesheet.stat().st_size <= 10 * 1024 * 1024:
                stylesheet_body, changed_stylesheet = _rewrite_css_sources_to_responses(
                    stylesheet.read_text(errors="replace"),
                    snapshot_root,
                    posixpath.normpath(
                        posixpath.join(posixpath.dirname(html_rel_path), local_path),
                    ),
                )
        if base_href:
            # An external <base> must never send a rewritten local asset live.
            if not replay_url:
                continue
            local_path = urljoin(replay_url, local_path)
        value = html.escape(local_path, quote=True)
        if start in background_styles:
            css_url = local_path.replace("\\", "\\\\").replace('"', r"\"")
            value = html.escape(f';background-image:url("{css_url}")', quote=True)
            if background_styles[start]:
                value = f' style="{value}"'
        elif start in inserted_sources:
            value = f' src="{value}"'
        edits.append((start, end, value))
        if changed_stylesheet:
            # Preserve integrity checking for the exact enriched representation;
            # raw downloads and unchanged stylesheet/script digests stay intact.
            integrity_start, integrity_end = stylesheet_integrities[start]
            digest = base64.b64encode(
                hashlib.sha384(stylesheet_body.encode("utf-8")).digest(),
            ).decode("ascii")
            edits.append(
                (integrity_start, integrity_end, f' integrity="sha384-{digest}"'),
            )
        rewrites += 1
    chunks = []
    previous = 0
    for start, end, value in sorted(edits):
        chunks.extend((html_text[previous:start], value))
        previous = end
    chunks.append(html_text[previous:])
    return "".join(chunks), rewrites


def _rewrite_css_sources_to_responses(
    css_text: str,
    snapshot_root: Path,
    rel_path: str,
) -> tuple[str, int]:
    index = snapshot_root / "responses" / "index.jsonl"
    if not index.is_file():
        return css_text, 0
    records = _saved_response_records(index)
    page_url = next(
        (
            record.get("url")
            for record in records
            if posixpath.normpath("responses/" + str(record.get("path", "")))
            == rel_path
        ),
        None,
    )
    if not page_url:
        # Mirrored stylesheets already have their own resource layout. Only
        # recorded response bodies have the original URL needed for resolution.
        return css_text, 0
    # Consume comments and ordinary strings as whole tokens so their apparent
    # url() text is inert. Escaped CSS URLs are left unchanged, never guessed.
    token = re.compile(
        r"/\*[\s\S]*?\*/|(?P<prefix>url\(\s*|@import\s+(?=[\"']))(?:\"(?P<double>[^\"\\]*)\"|'(?P<single>[^'\\]*)'|(?P<bare>[^\s()'\"\\]+))"
        r"|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'",
        re.IGNORECASE,
    )
    matches = [
        match
        for match in token.finditer(css_text)
        if match.group("prefix")
        and (
            not match.group("prefix").lower().startswith("url")
            or re.match(r"\s*\)", css_text[match.end() :])
        )
    ]
    wanted = {
        _encoded_responses_image_url(
            next(value for value in match.groups()[1:] if value is not None),
            page_url,
        )
        for match in matches
    }
    paths = _index_responses_paths_for_html_images(
        snapshot_root,
        rel_path,
        {url for url in wanted if url},
        records=records,
    )
    chunks = []
    previous = 0
    for match in matches:
        value = next(value for value in match.groups()[1:] if value is not None)
        local = paths.get(_encoded_responses_image_url(value, page_url) or "")
        if local is None:
            continue
        chunks.extend(
            (
                css_text[previous : match.start()],
                match.group("prefix"),
                '"',
                local.replace("\\", "\\\\").replace('"', r"\""),
                '"',
            ),
        )
        previous = match.end()
    chunks.append(css_text[previous:])
    return "".join(chunks), len(chunks) // 5


def rewrite_replay_document(
    text,
    *,
    snapshot_root,
    rel_path,
    content_type,
    page_url=None,
    replay_url=None,
    source_text=None,
):
    """Resolve saved HTML/CSS resources using this plugin's recorded responses."""
    if content_type == "text/css":
        return _rewrite_css_sources_to_responses(text, snapshot_root, rel_path)
    return _rewrite_html_image_sources_to_responses(
        text,
        snapshot_root,
        rel_path,
        page_url,
        replay_url,
        source_html=source_text,
    )


def replay_roots(snapshot_root: Path, url: str) -> tuple[str, ...]:
    """Locate this capture's original-host URL aliases."""
    host = urlsplit(url).hostname
    return (f"responses/{host.lower()}",) if host else ()

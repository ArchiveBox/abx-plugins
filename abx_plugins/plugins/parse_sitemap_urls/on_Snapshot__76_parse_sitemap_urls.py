#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "rich-click",
#   "abx-plugins",
#   "defusedxml>=0.7.1",
# ]
# ///
"""
Parse sitemap.xml (and sitemap-index, gzipped sitemaps, robots.txt
Sitemap: directives, and image/video/news extensions) and emit one
Snapshot record per discovered URL.

This is a standalone extractor that runs without ArchiveBox. Given any
seed URL the hook tries, in order:

1. If the URL points at a `.xml` / `.xml.gz` file, treat it as a sitemap.
2. If the URL points at a robots.txt, parse it for `Sitemap:` directives.
3. Otherwise treat the URL as a site root, probe robots.txt, then fall
   back to a list of common sitemap paths.

The host (ArchiveBox or abx-dl) owns the crawl frontier; this hook only
emits Snapshot JSONL records with an incremented `depth`. The host
applies its own max_depth / max_urls / dedup logic on top.

Security posture: every discovered URL passes through scheme allowlist,
optional same-host + private-IP guards, regex filters, and a global
visited-set. XML is parsed with `defusedxml` (no DTDs, no entities, no
external resolution). HTTP responses are size-capped before
decompression and the decompression itself is ratio-capped to neutralize
gzip bombs. Redirects are bounded and validated.
"""

from __future__ import annotations

import gzip
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse, urlsplit
from urllib.request import url2pathname
from xml.etree.ElementTree import Element, ParseError

import rich_click as click
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import iterparse as defused_iterparse

from abx_plugins.plugins.base.url_cleaning import sanitize_extracted_url
from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    emit_snapshot_record,
    emit_tag_record,
    get_extra_context,
    load_config,
    write_text_atomic,
)

PLUGIN_NAME = "parse_sitemap_urls"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)

URLS_FILE = Path("urls.jsonl")
NORESULTS_OUTPUT = "0 URLs parsed"

SITEMAP_NS = {
    "s": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "image": "http://www.google.com/schemas/sitemap-image/1.1",
    "video": "http://www.google.com/schemas/sitemap-video/1.1",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}
GZIP_MAGIC = b"\x1f\x8b"
UTF8_BOM = b"\xef\xbb\xbf"
UTF16_LE_BOM = b"\xff\xfe"
UTF16_BE_BOM = b"\xfe\xff"
ROBOTS_SITEMAP_RE = re.compile(r"^\s*sitemap\s*:\s*(\S+)\s*$", re.IGNORECASE)
TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
ALLOWED_REMOTE_SCHEMES = frozenset({"http", "https"})
ALLOWED_FALLBACK_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/wp-sitemap.xml",
    "/sitemap.xml.gz",
]

# Defensive caps; configurable via env.
DEFAULT_MAX_RESPONSE_BYTES = 50 * 1024 * 1024  # 50 MiB on the wire
DEFAULT_MAX_DECOMPRESSED_BYTES = 200 * 1024 * 1024  # 200 MiB after gunzip
DEFAULT_GZIP_MAX_RATIO = 100  # decompressed / compressed
DEFAULT_REGEX_INPUT_CAP = 8192  # max URL length passed to user regex


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _strip_query_and_fragment(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0]


def _is_xml_url(url: str) -> bool:
    lowered = _strip_query_and_fragment(url).lower()
    return lowered.endswith((".xml", ".xml.gz"))


def _is_robots_url(url: str) -> bool:
    """True when the URL path's basename is exactly `robots.txt`.

    A trailing match on `robots.txt` alone would also catch
    `foo-robots.txt`; we require the path basename to be the file.
    """
    path = _strip_query_and_fragment(url).lower()
    if not path:
        return False
    return path.rsplit("/", 1)[-1] == "robots.txt"


def _site_root(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return f"{parsed.scheme}://{parsed.netloc}"


def _normalize_url(raw: str, *, base_url: str | None = None) -> str:
    """Trim quoting/entity garbage, resolve scheme-relative URLs, drop fragments."""
    cleaned = sanitize_extracted_url(raw).strip()
    if not cleaned:
        return ""
    if cleaned.startswith("//") and base_url:
        parsed_base = urlparse(base_url)
        if parsed_base.scheme:
            cleaned = f"{parsed_base.scheme}:{cleaned}"
    cleaned, _ = urldefrag(cleaned)
    return cleaned.strip()


def _hosts_match(seed_host: str, candidate: str) -> bool:
    parsed = urlparse(candidate)
    return parsed.netloc.lower() == seed_host.lower()


def _ip_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


@dataclass(frozen=True)
class HostCheck:
    """Result of a private-host probe; distinguishes private from unresolvable."""

    private: bool
    reason: str


def _classify_host(netloc: str) -> HostCheck:
    """Classify a netloc for SSRF policy.

    Re-resolves DNS on every call. The check still has a TOCTOU window
    against the subsequent socket connect (urllib re-resolves), so this
    is best treated as a defense-in-depth layer alongside the scheme
    allowlist, response-size caps, and the operator's outbound firewall
    rules. A fully TOCTOU-safe design would require pinning to the
    resolved IP at connect time, which is out of scope for this plugin.
    """
    if not netloc:
        return HostCheck(True, "empty_netloc")
    # `urlsplit` correctly extracts hostnames from bracketed IPv6 forms
    # like [::1]:8080. Falling back to split(":") would yield "[" or
    # "[::1" depending on the form.
    parsed = urlsplit(f"//{netloc}")
    host = (parsed.hostname or "").strip()
    if not host:
        return HostCheck(True, "empty_host")
    try:
        ip = ipaddress.ip_address(host)
        return HostCheck(_ip_is_private(ip), "literal_ip")
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return HostCheck(True, "dns_unresolvable")
    for _family, _type, _proto, _canon, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _ip_is_private(ip):
            return HostCheck(True, "resolves_to_private")
    return HostCheck(False, "public")


def _is_private_host(netloc: str) -> bool:
    """Return True if netloc resolves to loopback / private / link-local."""
    return _classify_host(netloc).private


def _strip_bom(payload: bytes) -> bytes:
    if payload.startswith(UTF8_BOM):
        return payload[len(UTF8_BOM) :]
    if payload.startswith(UTF16_LE_BOM) or payload.startswith(UTF16_BE_BOM):
        try:
            decoded = payload.decode("utf-16")
        except UnicodeDecodeError:
            return payload
        # Re-emit as UTF-8 and align the XML declaration so the parser doesn't
        # choke on the apparent encoding/byte mismatch.
        decoded = re.sub(
            r'encoding\s*=\s*["\']\s*utf-?16(?:\s*-?\s*(?:le|be))?\s*["\']',
            'encoding="UTF-8"',
            decoded,
            count=1,
            flags=re.IGNORECASE,
        )
        return decoded.encode("utf-8")
    return payload


def _safe_decompress(payload: bytes, *, max_bytes: int, max_ratio: int) -> bytes:
    """Decompress gzip with hard caps. Raises ValueError on cap breach or
    corrupt input.

    Wraps the underlying `gzip.GzipFile` errors (``OSError`` from
    ``BadGzipFile`` / CRC failures, ``EOFError`` from truncation) so the
    walker can map a single exception type to a normal `failed`
    ArchiveResult.
    """
    compressed_size = len(payload)
    if compressed_size == 0:
        return payload
    try:
        decompressor = gzip.GzipFile(fileobj=io.BytesIO(payload))
        out = io.BytesIO()
        chunk_size = 64 * 1024
        while True:
            chunk = decompressor.read(chunk_size)
            if not chunk:
                break
            out.write(chunk)
            produced = out.tell()
            if produced > max_bytes:
                raise ValueError(
                    f"decompressed payload exceeded {max_bytes} bytes cap",
                )
            if max_ratio > 0 and produced > compressed_size * max_ratio:
                raise ValueError(
                    f"decompressed/compressed ratio exceeded {max_ratio}x cap",
                )
    except (OSError, EOFError) as exc:
        raise ValueError(f"gzip decompression failed: {exc}") from exc
    return out.getvalue()


def _maybe_decompress(
    payload: bytes,
    *,
    url_hint: str = "",
    max_bytes: int,
    max_ratio: int,
) -> bytes:
    # We only need to peek for the gzip magic bytes here; the .gz URL hint is
    # *not* sufficient on its own because `_fetch_bytes` may have already
    # decompressed a `Content-Encoding: gzip` body, leaving us with plain XML
    # whose URL still ends in `.gz`. Double-decompressing that would raise
    # `gzip.BadGzipFile` outside the caller's `ValueError` handler.
    if not payload.startswith(GZIP_MAGIC):
        return payload
    _ = url_hint
    return _safe_decompress(payload, max_bytes=max_bytes, max_ratio=max_ratio)


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _findall_ns(parent: Element, prefix: str, local_name: str) -> list[Element]:
    """Find children matching prefix:local_name in the sitemaps namespace and unnamespaced."""
    if prefix in SITEMAP_NS:
        results = parent.findall(f"{prefix}:{local_name}", SITEMAP_NS)
        if results:
            return results
    return parent.findall(local_name)


def _find_ns(parent: Element, prefix: str, local_name: str) -> Element | None:
    if prefix in SITEMAP_NS:
        found = parent.find(f"{prefix}:{local_name}", SITEMAP_NS)
        if found is not None:
            return found
    return parent.find(local_name)


def _compile_optional(pattern: str) -> re.Pattern[str] | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        click.echo(f"WARNING: invalid regex {pattern!r}: {exc}", err=True)
        return None


def _safe_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.strip())
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# HTTP fetch with retry, bounded redirects, body caps
# ---------------------------------------------------------------------------


@dataclass
class FetchOptions:
    timeout: int
    user_agent: str
    retries: int = 2
    backoff_seconds: float = 1.0
    max_redirects: int = 5
    verify_tls: bool = True
    accept_language: str = ""
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES
    gzip_max_ratio: int = DEFAULT_GZIP_MAX_RATIO
    allow_private_hosts: bool = False
    allow_file_urls: bool = False

    def headers(self) -> dict[str, str]:
        out: dict[str, str] = {
            "User-Agent": self.user_agent,
            "Accept": "application/xml, text/xml, application/x-gzip, */*;q=0.1",
            "Accept-Encoding": "gzip, identity",
        }
        if self.accept_language:
            out["Accept-Language"] = self.accept_language
        return out


def _build_ssl_context(verify: bool) -> ssl.SSLContext | None:
    if verify:
        return None
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class _BoundedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Cap redirects and reject targets that violate the fetch policy."""

    def __init__(self, options: FetchOptions) -> None:
        super().__init__()
        self._options = options
        # `HTTPRedirectHandler.max_redirections` is what the stdlib uses to
        # cap total redirects in the chain. Override per-instance via setattr
        # so the config knob actually takes effect; ``setattr`` keeps
        # type-checkers from flagging the ClassVar-vs-instance shape.
        setattr(self, "max_redirections", max(0, options.max_redirects))

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        target = urlparse(newurl)
        if target.scheme not in ALLOWED_REMOTE_SCHEMES:
            raise urllib.error.HTTPError(
                newurl,
                code,
                f"refusing redirect to disallowed scheme {target.scheme!r}",
                headers,
                fp,
            )
        if not self._options.allow_private_hosts and _is_private_host(target.netloc):
            raise urllib.error.HTTPError(
                newurl,
                code,
                f"refusing redirect to private host {target.netloc!r}",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener(options: FetchOptions) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = [_BoundedRedirectHandler(options)]
    ssl_context = _build_ssl_context(options.verify_tls)
    handlers.append(
        urllib.request.HTTPSHandler(context=ssl_context)
        if ssl_context
        else urllib.request.HTTPSHandler(),
    )
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = []  # we set our own headers per-request
    return opener


def _read_capped(response: Any, max_bytes: int) -> bytes:
    """Read at most max_bytes from a response. Raises ValueError on overrun.

    Reads in 64 KiB chunks, but requests one byte past `max_bytes` exactly
    once so the cap stays inclusive: a payload that is precisely
    `max_bytes` bytes succeeds; `max_bytes + 1` fails.
    """
    buf = io.BytesIO()
    while True:
        remaining_quota = max_bytes - buf.tell()
        if remaining_quota < 0:
            raise ValueError(f"response body exceeded {max_bytes} bytes cap")
        # Always ask for one byte beyond the remaining quota so we can detect
        # overrun without an off-by-one. When the quota hits zero we still
        # try to read one byte to confirm EOF.
        chunk = response.read(min(64 * 1024, remaining_quota + 1))
        if not chunk:
            return buf.getvalue()
        buf.write(chunk)
        if buf.tell() > max_bytes:
            raise ValueError(f"response body exceeded {max_bytes} bytes cap")


def _fetch_bytes(url: str, options: FetchOptions) -> bytes:
    """Fetch a URL with retry/backoff. Raises URLError / OSError / ValueError."""
    parsed = urlparse(url)
    if parsed.scheme == "file":
        if not options.allow_file_urls:
            raise ValueError(f"file:// not allowed by current policy: {url}")
        # url2pathname decodes percent-escapes (so file:// URLs with spaces work)
        # and handles Windows drive letters consistently.
        local_path = url2pathname(parsed.path)
        with open(local_path, "rb") as fh:
            data = fh.read(options.max_response_bytes + 1)
            if len(data) > options.max_response_bytes:
                raise ValueError(
                    f"file {local_path} exceeded {options.max_response_bytes} bytes cap",
                )
            return data
    if parsed.scheme not in ALLOWED_REMOTE_SCHEMES:
        raise ValueError(f"unsupported scheme {parsed.scheme!r} for {url}")
    if not options.allow_private_hosts and _is_private_host(parsed.netloc):
        raise ValueError(f"refusing fetch from private host {parsed.netloc!r}")

    last_error: BaseException | None = None
    opener = _build_opener(options)

    for attempt in range(max(0, options.retries) + 1):
        try:
            req = urllib.request.Request(url, headers=options.headers())
            with opener.open(req, timeout=options.timeout) as response:
                payload = _read_capped(response, options.max_response_bytes)
                content_encoding = (
                    response.headers.get("Content-Encoding") or ""
                ).lower()
                if content_encoding == "gzip":
                    payload = _safe_decompress(
                        payload,
                        max_bytes=options.max_decompressed_bytes,
                        max_ratio=options.gzip_max_ratio,
                    )
                return payload
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in TRANSIENT_HTTP_STATUSES and attempt < options.retries:
                _sleep_backoff(options.backoff_seconds, attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < options.retries:
                _sleep_backoff(options.backoff_seconds, attempt)
                continue
            raise

    assert last_error is not None  # for type-narrowing
    raise last_error


def _sleep_backoff(base: float, attempt: int) -> None:
    if base <= 0:
        return
    delay = base * (2**attempt)
    time.sleep(min(delay, 30.0))


def _parse_robots_txt(payload: bytes) -> list[str]:
    sitemaps: list[str] = []
    text = payload.decode("utf-8", errors="replace")
    for line in text.splitlines():
        match = ROBOTS_SITEMAP_RE.match(line)
        if match:
            candidate = _normalize_url(match.group(1))
            if candidate and candidate not in sitemaps:
                sitemaps.append(candidate)
    return sitemaps


# ---------------------------------------------------------------------------
# Sitemap parsing
# ---------------------------------------------------------------------------


@dataclass
class PageEntry:
    url: str
    lastmod: str | None = None
    priority: float | None = None
    changefreq: str | None = None
    extras: list[str] = field(default_factory=list)
    extra_tags: list[str] = field(default_factory=list)
    order_index: int = 0


def _build_page_entry(
    url_el: Element,
    *,
    base_url: str,
    emit_image_urls: bool,
    emit_video_urls: bool,
    emit_news_tag: bool,
    order_index: int,
) -> PageEntry | None:
    loc_el = _find_ns(url_el, "s", "loc")
    if loc_el is None or not loc_el.text:
        return None
    page_url = _normalize_url(loc_el.text, base_url=base_url)
    if not page_url:
        return None
    entry = PageEntry(url=page_url, order_index=order_index)

    lastmod_el = _find_ns(url_el, "s", "lastmod")
    if lastmod_el is not None and lastmod_el.text:
        entry.lastmod = lastmod_el.text.strip()

    changefreq_el = _find_ns(url_el, "s", "changefreq")
    if changefreq_el is not None and changefreq_el.text:
        entry.changefreq = changefreq_el.text.strip().lower()

    priority_el = _find_ns(url_el, "s", "priority")
    if priority_el is not None and priority_el.text:
        entry.priority = _safe_float(priority_el.text)

    if emit_image_urls:
        for image_el in _findall_ns(url_el, "image", "image"):
            image_loc = _find_ns(image_el, "image", "loc")
            if image_loc is not None and image_loc.text:
                cleaned = _normalize_url(image_loc.text, base_url=base_url)
                if cleaned:
                    entry.extras.append(cleaned)

    if emit_video_urls:
        for video_el in _findall_ns(url_el, "video", "video"):
            for video_loc_name in ("content_loc", "player_loc"):
                video_loc = _find_ns(video_el, "video", video_loc_name)
                if video_loc is not None and video_loc.text:
                    cleaned = _normalize_url(video_loc.text, base_url=base_url)
                    if cleaned:
                        entry.extras.append(cleaned)

    if emit_news_tag:
        for news_el in _findall_ns(url_el, "news", "news"):
            pub_el = _find_ns(news_el, "news", "publication")
            if pub_el is None:
                continue
            name_el = _find_ns(pub_el, "news", "name")
            if name_el is not None and name_el.text:
                entry.extra_tags.append(name_el.text.strip())

    return entry


def _stream_sitemap(
    payload: bytes,
    *,
    base_url: str,
    emit_image_urls: bool,
    emit_video_urls: bool,
    emit_news_tag: bool,
    next_order_start: int,
):
    """Stream `<url>` / `<sitemap>` elements out of a sitemap document.

    Yields `("page", PageEntry)` for urlset entries and
    `("child", str)` for sitemapindex children. Each element is freed
    immediately after it is processed AND the just-processed sibling is
    detached from the root's child list, so the resident XML tree
    stays at O(1) regardless of how many `<url>` elements the document
    contains. Yields nothing for unknown root tags. Raises `ValueError`
    on malformed XML so callers can map it to standard parse handling.
    """
    order_index = next_order_start
    root_element: Element | None = None
    root_local: str | None = None
    try:
        for event, elem in defused_iterparse(
            io.BytesIO(_strip_bom(payload)),
            events=("start", "end"),
        ):
            local = _strip_ns(elem.tag)
            if event == "start":
                if root_local is None:
                    root_local = local
                    root_element = elem
                continue
            # event == "end"
            yielded_child = False
            if local == "sitemap" and root_local == "sitemapindex":
                loc_el = _find_ns(elem, "s", "loc")
                if loc_el is not None and loc_el.text:
                    cleaned = _normalize_url(loc_el.text, base_url=base_url)
                    if cleaned:
                        yielded_child = True
                        yield ("child", cleaned)
            elif local == "url" and root_local == "urlset":
                entry = _build_page_entry(
                    elem,
                    base_url=base_url,
                    emit_image_urls=emit_image_urls,
                    emit_video_urls=emit_video_urls,
                    emit_news_tag=emit_news_tag,
                    order_index=order_index,
                )
                if entry is not None:
                    order_index += 1
                    yielded_child = True
                    yield ("page", entry)
            # Free the element and detach it from the root's child list so
            # memory stays bounded even for 500k-URL documents.
            if local in {"url", "sitemap"} and root_element is not None:
                elem.clear()
                # `remove(elem)` is O(n) on the child list; ET stores
                # children in a list. Detaching the head each time keeps
                # the per-iteration cost amortised O(1).
                try:
                    root_element.remove(elem)
                except ValueError:
                    pass
            elif local in {"urlset", "sitemapindex"}:
                elem.clear()
            _ = yielded_child
    except (ParseError, DefusedXmlException) as exc:
        # Both malformed XML and defusedxml's "no DTDs / no entities"
        # guards surface here; the walker maps any ValueError into a
        # standard `failed` ArchiveResult so the hook contract holds.
        raise ValueError(str(exc)) from exc


# ---------------------------------------------------------------------------
# URL acceptance policy
# ---------------------------------------------------------------------------


@dataclass
class UrlPolicy:
    """Final gate every emitted URL must pass."""

    seed_host: str
    allow_file_urls: bool
    allow_private_hosts: bool
    same_host_only: bool
    include_re: re.Pattern[str] | None
    exclude_re: re.Pattern[str] | None
    regex_input_cap: int = DEFAULT_REGEX_INPUT_CAP

    def reason_to_drop_fetch(self, url: str) -> str | None:
        """Gate for URLs we are about to fetch (seeds, child sitemaps).

        Applies only scheme + host policy; never regex / same-host. The
        regex filters describe which *pages* we want to emit, not which
        *sitemaps* we want to read.
        """
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme == "file":
            if not self.allow_file_urls:
                return "scheme_file"
            return None
        if scheme not in ALLOWED_REMOTE_SCHEMES:
            return f"scheme_{scheme or 'empty'}"
        if not parsed.netloc:
            return "no_netloc"
        if not self.allow_private_hosts and _is_private_host(parsed.netloc):
            return "private_host"
        return None

    def reason_to_drop_emit(self, url: str) -> str | None:
        """Gate for URLs we are about to emit as Snapshot records.

        Layers same-host + include/exclude regex on top of the fetch
        policy.
        """
        fetch_drop = self.reason_to_drop_fetch(url)
        if fetch_drop is not None:
            return fetch_drop
        if self.same_host_only and not _hosts_match(self.seed_host, url):
            return "host_mismatch"
        if self.include_re is not None or self.exclude_re is not None:
            scan_target = url[: self.regex_input_cap]
            if self.include_re is not None and not self.include_re.search(scan_target):
                return "include_filter"
            if self.exclude_re is not None and self.exclude_re.search(scan_target):
                return "exclude_filter"
        return None


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


@dataclass
class WalkerOptions:
    max_urls: int
    max_depth: int
    max_sitemaps: int
    priority_min: float
    changefreq_allowed: set[str]
    require_priority: bool
    emit_image_urls: bool
    emit_video_urls: bool
    emit_news_tag: bool
    restrict_child_to_seed_host: bool
    verbose: bool


class SitemapWalker:
    """Walk a tree of sitemap and sitemap-index documents."""

    def __init__(
        self,
        *,
        fetch: FetchOptions,
        options: WalkerOptions,
        policy: UrlPolicy,
    ) -> None:
        self.fetch = fetch
        self.options = options
        self.policy = policy
        self.visited_sitemaps: set[str] = set()
        self.seen_urls: set[str] = set()
        self.page_entries: list[PageEntry] = []
        self.sitemap_count = 0
        self.sitemap_attempts = 0
        self.skipped_filter = 0
        self.skipped_host = 0
        self.skipped_priority = 0
        self.skipped_changefreq = 0
        self.skipped_scheme = 0
        self.errors: list[str] = []
        self._order_counter = 0

    def walk(self, seed_url: str) -> None:
        self._walk_one(seed_url, depth=0)

    def _walk_one(self, sitemap_url: str, *, depth: int) -> None:
        if depth > self.options.max_depth:
            self.errors.append(f"max_depth reached at {sitemap_url}")
            return
        if sitemap_url in self.visited_sitemaps:
            return
        self.visited_sitemaps.add(sitemap_url)
        if len(self.page_entries) >= self.options.max_urls:
            return
        # Cap is on fetch *attempts*, not parsed successes — otherwise an
        # index pointing at thousands of 404 / timeout / refused children
        # could still trigger that many network calls.
        if (
            self.options.max_sitemaps > 0
            and self.sitemap_attempts >= self.options.max_sitemaps
        ):
            self.errors.append(
                f"max_sitemaps={self.options.max_sitemaps} reached; "
                f"refusing {sitemap_url}",
            )
            return
        self.sitemap_attempts += 1

        if self.options.verbose:
            click.echo(f"fetching sitemap {sitemap_url}", err=True)

        try:
            raw = _fetch_bytes(sitemap_url, self.fetch)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.errors.append(f"fetch failed for {sitemap_url}: {exc}")
            return

        try:
            payload = _maybe_decompress(
                raw,
                url_hint=sitemap_url,
                max_bytes=self.fetch.max_decompressed_bytes,
                max_ratio=self.fetch.gzip_max_ratio,
            )
        except ValueError as exc:
            self.errors.append(f"decompression failed for {sitemap_url}: {exc}")
            return

        # Stream the document and apply filters / dedup / max-urls inline so
        # we never materialize 50k entries when MAX_URLS is 10.
        deferred_children: list[str] = []
        try:
            for kind, value in _stream_sitemap(
                payload,
                base_url=sitemap_url,
                emit_image_urls=self.options.emit_image_urls,
                emit_video_urls=self.options.emit_video_urls,
                emit_news_tag=self.options.emit_news_tag,
                next_order_start=self._order_counter,
            ):
                if kind == "child" and isinstance(value, str):
                    deferred_children.append(value)
                    continue
                if kind != "page" or not isinstance(value, PageEntry):
                    continue
                entry = value
                self._order_counter = entry.order_index + 1
                if len(self.page_entries) >= self.options.max_urls:
                    break
                if not self._entry_passes_filters(entry):
                    continue
                if entry.url in self.seen_urls:
                    continue
                self.seen_urls.add(entry.url)
                self.page_entries.append(entry)
        except ValueError as exc:
            self.errors.append(f"not valid XML: {sitemap_url}: {exc}")
            return

        # XML parsed cleanly — count the visit even if the root tag wasn't
        # `<urlset>` or `<sitemapindex>` (treated as noresults, not failed).
        self.sitemap_count += 1

        for child_url in deferred_children:
            if len(self.page_entries) >= self.options.max_urls:
                return
            if (
                self.options.max_sitemaps > 0
                and self.sitemap_attempts >= self.options.max_sitemaps
            ):
                self.errors.append(
                    f"max_sitemaps={self.options.max_sitemaps} reached; "
                    f"refusing {child_url}",
                )
                return
            drop = self.policy.reason_to_drop_fetch(child_url)
            if drop is not None:
                self.errors.append(
                    f"refusing child sitemap {child_url} ({drop})",
                )
                continue
            # sitemaps.org §2.2: URLs in a sitemap must share the parent
            # sitemap's host. When SAME_HOST_ONLY is set we also enforce
            # this at the child-sitemap fetch boundary so a sitemap-index
            # on host A cannot pivot the walker onto host B.
            if self.options.restrict_child_to_seed_host and not _hosts_match(
                self.policy.seed_host,
                child_url,
            ):
                self.errors.append(
                    f"refusing child sitemap {child_url} (host_mismatch)",
                )
                continue
            self._walk_one(child_url, depth=depth + 1)

    def _entry_passes_filters(self, entry: PageEntry) -> bool:
        drop = self.policy.reason_to_drop_emit(entry.url)
        if drop is not None:
            if drop in {
                "scheme_file",
                "scheme_javascript",
                "no_netloc",
                "private_host",
            } or drop.startswith(
                "scheme_",
            ):
                self.skipped_scheme += 1
            elif drop == "host_mismatch":
                self.skipped_host += 1
            else:
                self.skipped_filter += 1
            return False
        if self.options.priority_min > 0.0:
            if entry.priority is None:
                if self.options.require_priority:
                    self.skipped_priority += 1
                    return False
            elif entry.priority < self.options.priority_min:
                self.skipped_priority += 1
                return False
        if self.options.changefreq_allowed:
            if (
                entry.changefreq is None
                or entry.changefreq not in self.options.changefreq_allowed
            ):
                self.skipped_changefreq += 1
                return False
        return True


# ---------------------------------------------------------------------------
# Seed resolution
# ---------------------------------------------------------------------------


def _resolve_sitemap_seeds(
    seed_url: str,
    *,
    fetch: FetchOptions,
    discover_from_robots: bool,
    fallback_paths: list[str],
) -> tuple[list[str], list[str]]:
    """Return (sitemap_urls, info_messages) for a seed URL."""
    info: list[str] = []

    if _is_xml_url(seed_url):
        return [seed_url], info

    if _is_robots_url(seed_url):
        try:
            payload = _fetch_bytes(seed_url, fetch)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            info.append(f"failed to fetch {seed_url}: {exc}")
            return [], info
        sitemaps = _parse_robots_txt(payload)
        if not sitemaps:
            info.append(f"robots.txt has no Sitemap: directives ({seed_url})")
        return sitemaps, info

    site_root = _site_root(seed_url)
    discovered: list[str] = []

    if discover_from_robots:
        robots_url = urljoin(site_root + "/", "robots.txt")
        try:
            payload = _fetch_bytes(robots_url, fetch)
            robots_sitemaps = _parse_robots_txt(payload)
            if robots_sitemaps:
                discovered.extend(robots_sitemaps)
                info.append(
                    f"discovered {len(robots_sitemaps)} sitemap(s) via {robots_url}",
                )
            else:
                info.append(
                    f"robots.txt found but had no Sitemap: lines ({robots_url})",
                )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            info.append(f"robots.txt unavailable ({robots_url}): {exc}")

    if not discovered:
        for path in fallback_paths:
            candidate = urljoin(site_root + "/", path.lstrip("/"))
            if candidate not in discovered:
                discovered.append(candidate)
        if discovered:
            info.append(
                f"falling back to {len(fallback_paths)} sitemap path(s) under {site_root}",
            )

    return discovered, info


# ---------------------------------------------------------------------------
# Sorting + persistence
# ---------------------------------------------------------------------------


def _sort_entries(entries: list[PageEntry], mode: str) -> list[PageEntry]:
    if mode == "lastmod":
        return sorted(entries, key=lambda e: e.lastmod or "", reverse=True)
    if mode == "priority":
        return sorted(
            entries,
            key=lambda e: e.priority if e.priority is not None else -1.0,
            reverse=True,
        )
    if mode == "order":
        return sorted(entries, key=lambda e: e.order_index)
    return sorted(entries, key=lambda e: e.url)


def persist_records(records: list[dict]) -> tuple[str, str]:
    if records:
        write_text_atomic(
            URLS_FILE,
            "\n".join(json.dumps(record) for record in records) + "\n",
        )
        return "succeeded", f"{len(records)} URLs parsed"
    URLS_FILE.unlink(missing_ok=True)
    return "noresults", NORESULTS_OUTPUT


def emit_result(status: str, output_str: str) -> None:
    emit_archive_result_record(status, output_str)
    if output_str:
        click.echo(output_str, err=True)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _cfg_str(name: str, default: str = "") -> str:
    value = getattr(CONFIG, name, default)
    return str(value) if value is not None else default


def _cfg_int(name: str, default: int) -> int:
    value = getattr(CONFIG, name, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cfg_float(name: str, default: float) -> float:
    value = getattr(CONFIG, name, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cfg_bool(name: str, default: bool) -> bool:
    value = getattr(CONFIG, name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)


def _cfg_list(name: str, default: list[str]) -> list[str]:
    value = getattr(CONFIG, name, default)
    if isinstance(value, list):
        return [str(item) for item in value]
    return list(default)


def _resolve_user_agent() -> str:
    explicit = _cfg_str("PARSE_SITEMAP_URLS_USER_AGENT", "")
    if explicit:
        return explicit
    return _cfg_str("USER_AGENT", "Mozilla/5.0 (compatible; ArchiveBox/1.0)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option(
    "--url",
    required=True,
    help="Seed URL: sitemap.xml, robots.txt, or site root",
)
@click.option(
    "--depth",
    type=int,
    default=0,
    help="Current crawl depth (relative to host frontier)",
)
def main(url: str, depth: int = 0) -> None:
    """Discover URLs from sitemap.xml (and friends) and emit Snapshot JSONL records."""
    extra_context = get_extra_context()
    if "snapshot_depth" in extra_context:
        depth = int(extra_context["snapshot_depth"])

    if not _cfg_bool("PARSE_SITEMAP_URLS_ENABLED", True):
        emit_result("skipped", "PARSE_SITEMAP_URLS_ENABLED=False")
        sys.exit(0)

    # file:// URLs are tolerated only when the seed itself is file://. This
    # blocks remote sitemap-index → file:// chains.
    seed_scheme = urlparse(url).scheme.lower()
    allow_file_urls = seed_scheme == "file" or _cfg_bool(
        "PARSE_SITEMAP_URLS_ALLOW_FILE_URLS",
        False,
    )
    allow_private_hosts = _cfg_bool(
        "PARSE_SITEMAP_URLS_ALLOW_PRIVATE_HOSTS",
        seed_scheme == "file",
    )

    fetch = FetchOptions(
        timeout=_cfg_int("PARSE_SITEMAP_URLS_TIMEOUT", _cfg_int("TIMEOUT", 60)),
        user_agent=_resolve_user_agent(),
        retries=_cfg_int("PARSE_SITEMAP_URLS_HTTP_RETRIES", 2),
        backoff_seconds=_cfg_float("PARSE_SITEMAP_URLS_HTTP_BACKOFF_SECONDS", 1.0),
        max_redirects=_cfg_int("PARSE_SITEMAP_URLS_HTTP_MAX_REDIRECTS", 5),
        verify_tls=_cfg_bool("PARSE_SITEMAP_URLS_VERIFY_TLS", True),
        accept_language=_cfg_str("PARSE_SITEMAP_URLS_ACCEPT_LANGUAGE", ""),
        max_response_bytes=_cfg_int(
            "PARSE_SITEMAP_URLS_MAX_RESPONSE_BYTES",
            DEFAULT_MAX_RESPONSE_BYTES,
        ),
        max_decompressed_bytes=_cfg_int(
            "PARSE_SITEMAP_URLS_MAX_DECOMPRESSED_BYTES",
            DEFAULT_MAX_DECOMPRESSED_BYTES,
        ),
        gzip_max_ratio=_cfg_int(
            "PARSE_SITEMAP_URLS_GZIP_MAX_RATIO",
            DEFAULT_GZIP_MAX_RATIO,
        ),
        allow_private_hosts=allow_private_hosts,
        allow_file_urls=allow_file_urls,
    )

    policy = UrlPolicy(
        seed_host=urlparse(url).netloc,
        allow_file_urls=allow_file_urls,
        allow_private_hosts=allow_private_hosts,
        same_host_only=_cfg_bool("PARSE_SITEMAP_URLS_SAME_HOST_ONLY", False),
        include_re=_compile_optional(
            _cfg_str("PARSE_SITEMAP_URLS_INCLUDE_REGEX", ""),
        ),
        exclude_re=_compile_optional(
            _cfg_str("PARSE_SITEMAP_URLS_EXCLUDE_REGEX", ""),
        ),
        regex_input_cap=_cfg_int(
            "PARSE_SITEMAP_URLS_REGEX_INPUT_CAP",
            DEFAULT_REGEX_INPUT_CAP,
        ),
    )

    walker_options = WalkerOptions(
        max_urls=_cfg_int("PARSE_SITEMAP_URLS_MAX_URLS", 5000),
        max_depth=_cfg_int("PARSE_SITEMAP_URLS_MAX_SITEMAP_DEPTH", 5),
        max_sitemaps=_cfg_int("PARSE_SITEMAP_URLS_MAX_SITEMAPS", 100),
        restrict_child_to_seed_host=_cfg_bool(
            "PARSE_SITEMAP_URLS_SAME_HOST_ONLY",
            False,
        ),
        priority_min=_cfg_float("PARSE_SITEMAP_URLS_PRIORITY_MIN", 0.0),
        changefreq_allowed={
            value.lower()
            for value in _cfg_list("PARSE_SITEMAP_URLS_CHANGEFREQ_ALLOWED", [])
            if value
        },
        require_priority=_cfg_bool("PARSE_SITEMAP_URLS_REQUIRE_PRIORITY", False),
        emit_image_urls=_cfg_bool("PARSE_SITEMAP_URLS_EMIT_IMAGE_URLS", False),
        emit_video_urls=_cfg_bool("PARSE_SITEMAP_URLS_EMIT_VIDEO_URLS", False),
        emit_news_tag=_cfg_bool("PARSE_SITEMAP_URLS_EMIT_NEWS_TAG", False),
        verbose=_cfg_bool("PARSE_SITEMAP_URLS_VERBOSE", False),
    )

    fallback_paths = _cfg_list(
        "PARSE_SITEMAP_URLS_FALLBACK_PATHS",
        list(ALLOWED_FALLBACK_PATHS),
    )
    discover_from_robots = _cfg_bool("PARSE_SITEMAP_URLS_DISCOVER_FROM_ROBOTS", True)

    seeds, info_messages = _resolve_sitemap_seeds(
        url,
        fetch=fetch,
        discover_from_robots=discover_from_robots,
        fallback_paths=fallback_paths,
    )
    for message in info_messages:
        click.echo(message, err=True)

    if not seeds:
        URLS_FILE.unlink(missing_ok=True)
        emit_result("noresults", "No sitemap URLs to fetch")
        sys.exit(0)

    walker = SitemapWalker(fetch=fetch, options=walker_options, policy=policy)
    seen_seeds: set[str] = set()
    for raw_seed in seeds:
        # Strip fragments and re-normalize so different surface spellings of
        # the same URL (CLI vs robots-derived vs fallback) deduplicate.
        seed, _ = urldefrag(raw_seed.strip())
        if not seed or seed in seen_seeds:
            continue
        seen_seeds.add(seed)
        if len(walker.page_entries) >= walker.options.max_urls:
            break
        # Apply fetch-time scheme/host policy to the seed. Emit-time filters
        # (regex, same-host) are layered on later, per page URL.
        drop = policy.reason_to_drop_fetch(seed)
        if drop is not None:
            walker.errors.append(f"refusing seed sitemap {seed} ({drop})")
            continue
        walker.walk(seed)

    for error in walker.errors:
        click.echo(error, err=True)

    if not walker.page_entries:
        URLS_FILE.unlink(missing_ok=True)
        if walker.sitemap_count == 0:
            emit_result("failed", "No valid sitemaps could be fetched/parsed")
            sys.exit(1)
        summary = _build_summary(0, walker)
        emit_result("noresults", summary)
        sys.exit(0)

    sort_mode = _cfg_str("PARSE_SITEMAP_URLS_SORT_BY", "url") or "url"
    ordered = _sort_entries(walker.page_entries, sort_mode)

    records: list[dict] = []
    extra_tags_seen: set[str] = set()
    skipped_extras = 0
    for entry in ordered:
        if len(records) >= walker_options.max_urls:
            break

        record: dict = {
            "type": "Snapshot",
            "url": entry.url,
            "plugin": PLUGIN_NAME,
            "depth": depth + 1,
        }
        if entry.lastmod:
            record["bookmarked_at"] = entry.lastmod
        records.append(record)

        for extra_url in entry.extras:
            if len(records) >= walker_options.max_urls:
                break
            if extra_url == entry.url or extra_url in walker.seen_urls:
                continue
            drop = policy.reason_to_drop_emit(extra_url)
            if drop is not None:
                skipped_extras += 1
                continue
            walker.seen_urls.add(extra_url)
            records.append(
                {
                    "type": "Snapshot",
                    "url": extra_url,
                    "plugin": PLUGIN_NAME,
                    "depth": depth + 1,
                    "tags": "sitemap-media",
                },
            )

        for tag in entry.extra_tags:
            if tag:
                extra_tags_seen.add(tag)

    for tag in sorted(extra_tags_seen):
        emit_tag_record(tag)

    for record in records:
        emit_snapshot_record(record)

    status, _ = persist_records(records)
    summary = _build_summary(len(records), walker, skipped_extras=skipped_extras)
    emit_result(status, summary)
    sys.exit(0)


def _build_summary(
    record_count: int,
    walker: SitemapWalker,
    *,
    skipped_extras: int = 0,
) -> str:
    return (
        f"{record_count} URLs parsed (visited {walker.sitemap_count} sitemap(s); "
        f"skipped_filter={walker.skipped_filter} "
        f"skipped_host={walker.skipped_host} "
        f"skipped_priority={walker.skipped_priority} "
        f"skipped_changefreq={walker.skipped_changefreq} "
        f"skipped_scheme={walker.skipped_scheme} "
        f"skipped_extras={skipped_extras})"
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
import hashlib
import json
import os
import re
import sys
import tempfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import rich_click as click
from abx_plugins.plugins.base.utils import emit_archive_result_record, load_config, write_text_atomic

CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / "archiveimport"


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and (urlsplit(req.full_url).scheme, urlsplit(req.full_url).netloc) != (urlsplit(newurl).scheme, urlsplit(newurl).netloc):
            redirected.remove_header("Authorization")
        return redirected


OPENER = build_opener(SafeRedirect())


def request(url, key=""):
    headers = {"User-Agent": CONFIG.USER_AGENT} if CONFIG.USER_AGENT else {}
    if key:
        headers["Authorization"] = f"ApiKey {key}"
    return OPENER.open(Request(url, headers=headers), timeout=CONFIG.ARCHIVEIMPORT_TIMEOUT)


def safe_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in name.split("/")) or "\\" in name:
        raise ValueError(f"Unsafe archive filename: {name!r}")
    return path


def download(url, destination, remaining, key="", expected_md5="", memento=None):
    if not destination.resolve().is_relative_to(OUTPUT_DIR.resolve()):
        raise ValueError("Archive path escapes output directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Existing snapshot output must survive an interrupted download.
    fd, temporary = tempfile.mkstemp(prefix=".archiveimport-", dir=destination.parent)
    size = 0
    digest = hashlib.md5()
    try:
        with os.fdopen(fd, "wb") as output, request(url, key) as response:
            if response.status == 206:
                content_range = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range)
                if not match or int(match.group(1)) + 1 != int(match.group(2)):
                    raise ValueError("Archive server returned a partial file")
            expected_length = response.headers.get("Content-Length")
            if memento is not None:
                timestamp = response.headers.get("Memento-Datetime")
                if not timestamp:
                    raise ValueError("Wayback did not return an archived capture")
                memento.update({"url": response.url, "datetime": timestamp,
                                "content_type": response.headers.get("Content-Type", ""),
                                "archive_source": response.headers.get("X-Archive-Src", "")})
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if remaining is not None and size > remaining:
                    raise ValueError("Archive exceeds ARCHIVEIMPORT_MAX_BYTES")
                digest.update(chunk)
                output.write(chunk)
        if expected_length and size != int(expected_length):
            raise ValueError("Archive download was incomplete")
        if destination.suffix == ".warc":
            with open(temporary, "rb") as source:
                if not source.read(8).startswith(b"WARC/1."):
                    raise ValueError("Archive is not a WARC file")
        if expected_md5 and digest.hexdigest().lower() != expected_md5.lower():
            raise ValueError(f"Checksum mismatch for {destination.name}")
        if destination.suffix == ".wacz" and not zipfile.is_zipfile(temporary):
            raise ValueError("Perma export is not a valid WACZ ZIP")
        os.replace(temporary, destination)
        return size
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def classify(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return None, None
    host = (parsed.hostname or "").lower()
    parts = parsed.path.strip("/").split("/")
    if host in {"archive.org", "www.archive.org"} and len(parts) >= 2 and parts[0] in {"details", "download"}:
        identifier = unquote(parts[1])
        if re.fullmatch(r"[A-Za-z0-9_.-]+", identifier) and identifier not in {".", ".."}:
            return "item", identifier
    if host == "ghostarchive.org" and len(parts) >= 2 and parts[0] == "archive" and re.fullmatch(r"[A-Za-z0-9]+", parts[1]):
        return "ghostarchive", parts[1]
    if host == "web.archive.org":
        match = re.fullmatch(r"/web/([0-9]{1,14})(?:[a-z]+_)?/(https?://.+)", parsed.path)
        if match:
            raw = f"https://web.archive.org/web/{match.group(1)}id_/{match.group(2)}"
            if parsed.query:
                raw += "?" + parsed.query
            return "wayback", raw
    if host in {"perma.cc", "www.perma.cc"} and re.fullmatch(r"[A-Za-z0-9]{4}-[A-Za-z0-9]{4}", parts[0]):
        return "perma", parts[0].upper()
    return None, None


def save_item(identifier):
    with request(f"https://archive.org/metadata/{quote(identifier, safe='')}") as response:
        metadata = json.load(response)
    if not metadata.get("metadata"):
        return None
    entries = metadata.get("files", [])
    if not isinstance(entries, list):
        raise ValueError("Invalid Internet Archive file list")
    used = 0
    captured = []
    for entry in entries:
        name = entry.get("name")
        if not name or entry.get("private") in (True, "true", "1"):
            continue
        path = safe_path(name)
        url = f"https://archive.org/download/{quote(identifier, safe='')}/{quote(name, safe='/')}"
        remaining = CONFIG.ARCHIVEIMPORT_MAX_BYTES - used if CONFIG.ARCHIVEIMPORT_MAX_BYTES else None
        print(f"[archiveimport] Downloading {name}", file=sys.stderr)
        size = download(url, OUTPUT_DIR / "files" / path, remaining, expected_md5=entry.get("md5", "") if not entry.get("summation") else "")
        used += size
        captured.append({"name": name, "size": size, "path": f"files/{name}"})
    metadata["archiveimport"] = {"source": "Internet Archive", "files": captured}
    write_text_atomic(OUTPUT_DIR / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    return "archiveimport/metadata.json"


class GhostArchivePage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.source = None

    def handle_starttag(self, tag, attrs):
        if tag == "replay-web-page":
            self.source = dict(attrs).get("source")


def save_ghostarchive(identifier):
    with request(f"https://ghostarchive.org/archive/{identifier}") as response:
        html = response.read(2 * 1024 * 1024 + 1)
    if len(html) > 2 * 1024 * 1024:
        raise ValueError("Ghostarchive replay page is too large")
    page = GhostArchivePage()
    page.feed(html.decode("utf-8", errors="replace"))
    source = urlsplit(page.source or "")
    if source.scheme != "https" or source.hostname != "ghostarchive.org" or not source.path.endswith(".warc"):
        return None
    name = f"{identifier}.warc"
    size = download(page.source, OUTPUT_DIR / name, CONFIG.ARCHIVEIMPORT_MAX_BYTES or None)
    write_text_atomic(OUTPUT_DIR / "metadata.json", json.dumps({"archiveimport": {"source": "Ghostarchive", "files": [{"name": name, "path": name, "size": size}]}}, indent=2))
    return "archiveimport/metadata.json"


def save_wayback(url):
    capture = {}
    size = download(url, OUTPUT_DIR / "response", CONFIG.ARCHIVEIMPORT_MAX_BYTES or None, memento=capture)
    metadata = {"archiveimport": {"source": "Wayback Machine raw response", "capture": capture,
                                  "files": [{"name": "response", "path": "response", "size": size}]}}
    write_text_atomic(OUTPUT_DIR / "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    return "archiveimport/metadata.json"


def save_perma(guid):
    key = CONFIG.ARCHIVEIMPORT_PERMA_API_KEY
    if not key:
        return None
    url = f"https://api.perma.cc/v1/archives/{guid}/download?file_format=wacz"
    limit = CONFIG.ARCHIVEIMPORT_MAX_BYTES or None
    download(url, OUTPUT_DIR / f"{guid}.wacz", limit, key=key)
    write_text_atomic(OUTPUT_DIR / "metadata.json", json.dumps({"archiveimport": {"source": "Perma.cc", "files": [{"name": f"{guid}.wacz", "path": f"{guid}.wacz"}]}}, indent=2))
    return f"archiveimport/{guid}.wacz"


@click.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.option("--url", required=True)
def main(url):
    if not CONFIG.ARCHIVEIMPORT_ENABLED:
        emit_archive_result_record("skipped", "ARCHIVEIMPORT_ENABLED=False")
        return
    kind, identifier = classify(url)
    if not kind:
        emit_archive_result_record("noresults", "No supported archive export for this URL")
        return
    if kind == "perma" and not CONFIG.ARCHIVEIMPORT_PERMA_API_KEY:
        emit_archive_result_record("noresults", "Perma.cc export requires an API key")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "item":
            output = save_item(identifier)
        elif kind == "wayback":
            output = save_wayback(identifier)
        elif kind == "ghostarchive":
            output = save_ghostarchive(identifier)
        else:
            output = save_perma(identifier)
        emit_archive_result_record("succeeded" if output else "noresults", output or "Archive export not found")
    except HTTPError as exc:
        print(f"[archiveimport] HTTP {exc.code} from {urlsplit(exc.url).hostname}", file=sys.stderr)
        if exc.code in (401, 403, 404):
            emit_archive_result_record("noresults", f"Archive unavailable: HTTP {exc.code}")
        else:
            print(f"[archiveimport] HTTP {exc.code}", file=sys.stderr)
            emit_archive_result_record("failed", f"Archive download failed: HTTP {exc.code}")
            sys.exit(1)
    except Exception as exc:
        print(f"[archiveimport] {type(exc).__name__}: {exc}", file=sys.stderr)
        emit_archive_result_record("failed", f"{type(exc).__name__}: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()

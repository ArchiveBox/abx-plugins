#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["rich-click"]
# ///
"""Save source maps for scripts captured by the responses plugin."""
import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote_to_bytes, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import rich_click as click

from abx_plugins.plugins.base.utils import emit_archive_result_record, load_config, write_text_atomic

CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT = SNAP_DIR / "sourcemaps"
MAX_BYTES = 32 * 1024 * 1024
REFERENCE = re.compile(rb"(?:/\/[#@]\s*sourceMappingURL\s*=\s*([^\s]+)|/\*[#@]\s*sourceMappingURL\s*=\s*([^\s*]+)\s*\*/)")


def get_map(reference, script_url, timeout):
    if reference.startswith("data:"):
        header, data = reference.split(",", 1)
        raw = base64.b64decode(data, validate=True) if ";base64" in header else unquote_to_bytes(data)
        return raw, "inline"
    url = urljoin(script_url, reference)
    if urlsplit(url).scheme not in ("http", "https"):
        return None, url
    request = Request(url, headers={"User-Agent": str(CONFIG.USER_AGENT)})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
    except (HTTPError, URLError, TimeoutError) as error:
        print(f"Source map unavailable: {url}: {error}", file=sys.stderr)
        return None, url
    return raw, url


def candidates(script, url, timeout):
    matches = list(REFERENCE.finditer(script))
    if matches:
        return [(matches[-1].group(1) or matches[-1].group(2)).decode("utf-8", "replace")]
    parts = urlsplit(url)
    if parts.scheme in ("http", "https"):
        # The responses manifest does not retain HTTP headers. Re-fetch the
        # script to discover a published SourceMap header when possible.
        try:
            request = Request(url, headers={"User-Agent": str(CONFIG.USER_AGENT)})
            with urlopen(request, timeout=timeout) as response:
                header = response.headers.get("SourceMap") or response.headers.get("X-SourceMap")
            if header:
                return [header.strip()]
        except (HTTPError, URLError, TimeoutError) as error:
            print(f"Source map header unavailable: {url}: {error}", file=sys.stderr)
    if not parts.path.endswith(".js"):
        return []
    # Some publishers omit the directive while publishing conventional map names.
    return [urlunsplit((parts.scheme, parts.netloc, parts.path + ".map", parts.query, "")),
            urlunsplit((parts.scheme, parts.netloc, parts.path[:-3] + ".map", parts.query, ""))]


@click.command(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
@click.option("--url", required=True)
def main(url):
    if not CONFIG.SOURCEMAPS_ENABLED:
        emit_archive_result_record("skipped", "SOURCEMAPS_ENABLED=False")
        return
    try:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        response_dir = SNAP_DIR / "responses"
        index = response_dir / "index.jsonl"
        if not index.is_file():
            raise FileNotFoundError("responses/index.jsonl is missing")
        records = []
        seen = set()
        for line in index.read_text().splitlines():
            entry = json.loads(line)
            script_url = entry.get("url", "")
            if entry.get("resourceType") != "script" or script_url in seen:
                continue
            seen.add(script_url)
            source = (response_dir / entry.get("path", "")).resolve()
            if not source.is_relative_to(response_dir.resolve()) or not source.is_file():
                continue
            if source.stat().st_size > MAX_BYTES:
                continue
            for reference in candidates(source.read_bytes(), script_url, int(CONFIG.SOURCEMAPS_TIMEOUT)):
                try:
                    raw, map_url = get_map(reference, script_url, int(CONFIG.SOURCEMAPS_TIMEOUT))
                    if not raw or len(raw) > MAX_BYTES:
                        continue
                    parsed = json.loads(raw)
                    if not isinstance(parsed, dict) or parsed.get("version") != 3 or not ("mappings" in parsed or "sections" in parsed):
                        continue
                except (ValueError, UnicodeError, OSError) as error:
                    print(f"Invalid source map for {script_url}: {error}", file=sys.stderr)
                    continue
                digest = hashlib.sha256(raw).hexdigest()
                filename = f"{digest}.map"
                target = OUTPUT / filename
                temp = target.with_suffix(".tmp")
                temp.write_bytes(raw)
                os.replace(temp, target)
                records.append({"script_url": script_url, "map_url": map_url, "path": filename, "sources": len(parsed.get("sources", []))})
                break
        if not records:
            emit_archive_result_record("noresults", "No source maps found")
            return
        write_text_atomic(OUTPUT / "index.json", json.dumps(records, indent=2) + "\n")
        emit_archive_result_record("succeeded", "sourcemaps/index.json")
    except Exception as error:
        print(f"Source map capture failed: {type(error).__name__}: {error}", file=sys.stderr)
        emit_archive_result_record("failed", str(error))
        sys.exit(1)


if __name__ == "__main__":
    main()

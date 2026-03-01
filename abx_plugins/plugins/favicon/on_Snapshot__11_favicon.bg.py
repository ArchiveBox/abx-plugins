#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "rich-click",
# ]
# ///
#
# Extract favicon from a URL and save it to the local filesystem.
# Supports multiple favicon sources including HTML link tags and Google's favicon service.
#
# Usage:
#     ./on_Snapshot__11_favicon.bg.py --url=<url> --snapshot-id=<snapshot-id>

import json
import os
import re
import sys

from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "favicon"
PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
OUTPUT_FILE = "favicon.ico"


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def get_env_int(name: str, default: int = 0) -> int:
    try:
        return int(get_env(name, str(default)))
    except ValueError:
        return default


def http_get(url: str, headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as response:
            return response.getcode() or 0, response.read()
    except HTTPError as e:
        return e.code, e.read()


def get_favicon(url: str) -> tuple[bool, str | None, str]:
    """
    Fetch favicon from URL.

    Returns: (success, output_path, error_message)
    """

    timeout = get_env_int("FAVICON_TIMEOUT") or get_env_int("TIMEOUT", 30)
    user_agent = get_env("USER_AGENT", "Mozilla/5.0 (compatible; ArchiveBox/1.0)")
    headers = {"User-Agent": user_agent}

    # Build list of possible favicon URLs
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    favicon_urls = [
        urljoin(base_url, "/favicon.ico"),
        urljoin(base_url, "/favicon.png"),
        urljoin(base_url, "/apple-touch-icon.png"),
    ]

    # Try to extract favicon URL from HTML link tags
    try:
        status_code, body = http_get(url, headers=headers, timeout=timeout)
        if 200 <= status_code < 300 and body:
            html = body.decode("utf-8", errors="replace")
            # Look for <link rel="icon" href="...">
            for match in re.finditer(
                r'<link[^>]+rel=["\'](?:shortcut )?icon["\'][^>]+href=["\']([^"\']+)["\']',
                html,
                re.I,
            ):
                favicon_urls.insert(0, urljoin(url, match.group(1)))

            # Also check reverse order: href before rel
            for match in re.finditer(
                r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\'](?:shortcut )?icon["\']',
                html,
                re.I,
            ):
                favicon_urls.insert(0, urljoin(url, match.group(1)))
    except Exception:
        pass  # Continue with default favicon URLs

    # Try each URL until we find one that works
    for favicon_url in favicon_urls:
        try:
            status_code, body = http_get(favicon_url, headers=headers, timeout=15)
            if 200 <= status_code < 300 and body:
                Path(OUTPUT_FILE).write_bytes(body)
                return True, OUTPUT_FILE, ""
        except Exception:
            continue

    # Try Google's favicon service as fallback
    try:
        google_url = f"https://www.google.com/s2/favicons?domain={parsed.netloc}"
        status_code, body = http_get(google_url, headers=headers, timeout=15)
        if 200 <= status_code < 300 and body:
            Path(OUTPUT_FILE).write_bytes(body)
            return True, OUTPUT_FILE, ""
    except Exception:
        pass

    return False, None, "No favicon found"


@click.command()
@click.option("--url", required=True, help="URL to extract favicon from")
@click.option("--snapshot-id", required=True, help="Snapshot UUID")
def main(url: str, snapshot_id: str):
    """Extract favicon from a URL."""

    output = None
    status = "failed"
    error = ""

    try:
        # Run extraction
        success, output, error = get_favicon(url)
        if success:
            status = "succeeded"
        else:
            status = "failed"

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        status = "failed"

    if error:
        print(f"ERROR: {error}", file=sys.stderr)

    # Output clean JSONL (no RESULT_JSON= prefix)
    result = {
        "type": "ArchiveResult",
        "status": status,
        "output_str": output or error or "",
    }
    print(json.dumps(result))

    sys.exit(0 if status == "succeeded" else 1)


if __name__ == "__main__":
    main()

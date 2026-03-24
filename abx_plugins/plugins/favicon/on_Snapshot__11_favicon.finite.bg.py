#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "rich-click",
#   "abx-plugins",
# ]
# ///
#
# Extract favicon from a URL and save it to the local filesystem.
# Supports multiple favicon sources including HTML link tags and a configurable fallback provider.
#
# Usage:
#     ./on_Snapshot__11_favicon.finite.bg.py --url=<url>

import os
import re
import sys

from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from abx_plugins.plugins.base.utils import emit_archive_result_record, get_config

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "favicon"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = get_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
OUTPUT_FILE = "favicon.ico"
SUCCESS_OUTPUT = f"{PLUGIN_DIR}/{OUTPUT_FILE}"


def http_get(url: str, headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as response:
            return response.getcode() or 0, response.read()
    except HTTPError as e:
        return e.code, e.read()


def save_favicon(body: bytes) -> str:
    Path(OUTPUT_FILE).write_bytes(body)
    return OUTPUT_FILE


def build_provider_url(provider_template: str, domain: str) -> str:
    if not provider_template:
        return ""

    if "{domain}" in provider_template:
        return provider_template.format(domain=quote(domain, safe=""))

    if "{}" in provider_template:
        return provider_template.format(quote(domain, safe=""))

    return provider_template


def get_favicon(url: str) -> tuple[bool, str | None, str]:
    """
    Fetch favicon from URL.

    Returns: (success, output_path, error_message)
    """

    config = get_config()
    timeout = config.FAVICON_TIMEOUT
    user_agent = config.FAVICON_USER_AGENT or "Mozilla/5.0 (compatible; ArchiveBox/1.0)"
    provider_template = (config.FAVICON_PROVIDER or "").strip()
    headers = {"User-Agent": user_agent}

    # Build list of possible favicon URLs
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    domain = parsed.hostname or parsed.netloc

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
            status_code, body = http_get(favicon_url, headers=headers, timeout=timeout)
            if 200 <= status_code < 300 and body:
                return True, save_favicon(body), ""
        except Exception:
            continue

    # Try configured provider as final fallback.
    provider_url = build_provider_url(provider_template, domain)
    if provider_url:
        try:
            status_code, body = http_get(provider_url, headers=headers, timeout=timeout)
            if 200 <= status_code < 300 and body:
                return True, save_favicon(body), ""
        except Exception:
            pass

    return False, None, "No favicon found"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL to extract favicon from")
def main(url: str):
    """Extract favicon from a URL."""
    output = None
    status = "failed"
    error = ""
    output_path = OUTPUT_DIR / OUTPUT_FILE

    try:
        # Run extraction
        success, output, error = get_favicon(url)
        if success and output_path.exists() and output_path.stat().st_size > 0:
            status = "succeeded"
            output = OUTPUT_FILE
        elif output_path.exists() and output_path.stat().st_size > 0:
            status = "succeeded"
            output = OUTPUT_FILE
            error = ""
        else:
            status = "failed"
            output = None
            error = error or "No favicon found"

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        status = "failed"

    if error:
        print(f"ERROR: {error}", file=sys.stderr)

    emit_archive_result_record(
        status,
        SUCCESS_OUTPUT if output else (error or ""),
    )

    sys.exit(0 if status == "succeeded" else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12,<3.14"
# ///
# ruff: noqa: E402
#
# Extract favicon from a URL and save it to the local filesystem.
# Supports multiple favicon sources including HTML link tags and a configurable fallback provider.
#
# Usage:
#     ./on_Snapshot__11_favicon.finite.bg.py --url=<url>

import signal

# Snapshot cleanup sends SIGTERM to the whole hook process group as the polite
# shutdown signal before the hard SIGKILL deadline. This hook is finite work, so
# treating SIGTERM as "stop now" can turn normal cleanup into a failed
# ArchiveResult. Installing SIG_IGN before imports that may perform setup keeps
# cleanup from interrupting the result path; SIGKILL still enforces the hard
# timeout if the hook does not finish.
signal.signal(signal.SIGTERM, signal.SIG_IGN)

import os
import base64
import json
import re
import subprocess
import sys

from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

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


class HttpDeadlineExceeded(TimeoutError):
    pass


def http_get(url: str, headers: dict[str, str], timeout: int) -> tuple[int, bytes]:
    # urllib's socket timeout does not bound total request time when the peer
    # accepts the connection and delays the response. FAVICON_TIMEOUT is the
    # per-candidate extractor budget, so run the blocking fetch in a plain
    # Python child process and let subprocess enforce the deadline. A thread is
    # not enough here: a blocked urllib worker can keep interpreter shutdown
    # waiting, which turns one slow favicon endpoint into a full snapshot stall.
    fetch_code = r"""
import base64, json, sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen
url = sys.argv[1]
headers = json.loads(sys.argv[2])
timeout = int(sys.argv[3])
req = Request(url, headers=headers)
try:
    with urlopen(req, timeout=timeout) as response:
        body = response.read()
        print(json.dumps({"ok": True, "status": response.getcode() or 0, "body": base64.b64encode(body).decode("ascii")}))
except HTTPError as e:
    body = e.read()
    print(json.dumps({"ok": True, "status": e.code, "body": base64.b64encode(body).decode("ascii")}))
except BaseException as e:
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
"""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", fetch_code, url, json.dumps(headers), str(timeout)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise HttpDeadlineExceeded(f"timed out after {timeout} seconds") from err
    if proc.returncode != 0:
        raise RuntimeError(
            proc.stderr.strip() or f"favicon fetch exited {proc.returncode}",
        )
    result = json.loads(proc.stdout.strip() or "{}")
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "favicon fetch failed")
    return int(result["status"]), base64.b64decode(result.get("body") or "")


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

    timeout = int(CONFIG.FAVICON_TIMEOUT)
    library_version = os.environ.get("LIBRARY_VERSION", "0.0.1")
    user_agent = (
        f"ArchiveBox/{library_version} (+https://github.com/ArchiveBox/ArchiveBox/)"
    )
    provider_template = (CONFIG.FAVICON_PROVIDER or "").strip()
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
        except HttpDeadlineExceeded:
            break
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
            status = "noresults"
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

    sys.exit(0 if status in ("succeeded", "noresults", "skipped") else 1)


if __name__ == "__main__":
    main()

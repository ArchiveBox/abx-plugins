#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
# ruff: noqa: E402
#
# Submit a URL to archive.org for archiving and save the resulting archive.org link.
#
# Usage:
#     ./on_Snapshot__00_archivedotorg.finite.bg.py --url=<url> > events.jsonl

# Start before browser setup/recorders/navigation: this request needs only the
# URL and waits mostly on a remote service, so its latency can overlap every
# browser extractor. Other downloaders stay after navigation to avoid competing
# with browser startup. Grouping this lightweight submission with them delayed
# it until ~40s after acceptance and put its remote wait on the sealing path.

import signal
import sys

# Snapshot cleanup sends SIGTERM to the whole hook process group as the polite
# shutdown signal before the hard SIGKILL deadline. This hook is a finite
# downloader, so treating SIGTERM as "stop now" corrupts the normal contract:
# an in-flight download becomes a failed ArchiveResult even though cleanup would
# have allowed it to finish within the hook timeout. Installing SIG_IGN before
# any heavy imports or network work makes cleanup mean "finish promptly if you
# can"; the later SIGKILL deadline still enforces the hard stop.
signal.signal(signal.SIGTERM, signal.SIG_IGN)

if any(arg == "--url" or arg.startswith("--url=") for arg in sys.argv[1:]):
    print("archive.org submission started", flush=True)

import os
from http.client import RemoteDisconnected
from ipaddress import ip_address
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from abx_plugins.plugins.base.utils import emit_archive_result_record, load_config

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "archivedotorg"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)
OUTPUT_FILE = "archive.org.txt"


def should_skip_archivedotorg_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return "URL has no hostname"
    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
    ):
        return "URL is local/private"
    try:
        address = ip_address(hostname)
    except ValueError:
        return ""
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
    ):
        return "URL is local/private"
    return ""


def submit_to_archivedotorg(url: str) -> tuple[bool, str | None, str]:
    """
    Submit URL to archive.org Wayback Machine.

    Returns: (success, output_path, error_message)
    """

    def log(message: str) -> None:
        print(f"[archivedotorg] {message}", file=sys.stderr)

    config = load_config()
    timeout = config.ARCHIVEDOTORG_TIMEOUT
    library_version = os.environ.get("LIBRARY_VERSION", "0.0.1")
    user_agent = (
        f"ArchiveBox/{library_version} (+https://github.com/ArchiveBox/ArchiveBox/)"
    )

    submit_url = f"https://web.archive.org/save/{url}"
    log(f"Submitting to Wayback Machine (timeout={timeout}s)")
    log(f"GET {submit_url}")

    try:
        print("submitting to archive.org...", file=sys.stderr)
        req = Request(submit_url, headers={"User-Agent": user_agent})
        response = urlopen(req, timeout=timeout)
        final_url = response.url
        status = response.status
        headers = response.headers
        body = response.read().decode("utf-8", errors="replace")
        log(f"HTTP {status} final_url={final_url}")

        # Check for successful archive
        content_location = (
            headers["Content-Location"] if "Content-Location" in headers else ""
        )
        x_archive_orig_url = (
            headers["X-Archive-Orig-Url"] if "X-Archive-Orig-Url" in headers else ""
        )
        if content_location:
            log(f"Content-Location: {content_location}")
        if x_archive_orig_url:
            log(f"X-Archive-Orig-Url: {x_archive_orig_url}")

        # A save-request URL is not proof that a capture exists. Only replace
        # a previous result once Wayback returns an actual replay location.
        archive_url = (
            urljoin("https://web.archive.org", content_location)
            if content_location
            else final_url
        )
        archived = urlparse(archive_url)
        if archived.hostname == "web.archive.org" and archived.path.startswith("/web/"):
            Path(OUTPUT_FILE).write_text(archive_url, encoding="utf-8")
            log(f"Saved archive URL -> {archive_url}")
            return True, OUTPUT_FILE, ""
        if "RobotAccessControlException" in body:
            return False, None, "Archive.org blocked submission by robots.txt"
        return False, None, "Archive.org returned no archive URL"

    except HTTPError as e:
        return False, None, f"Archive.org returned HTTP {e.code}"
    except TimeoutError:
        return False, None, f"Request timed out after {timeout} seconds"
    except RemoteDisconnected as e:
        return False, None, f"RemoteDisconnected: {e}"
    except URLError as e:
        return False, None, f"URLError: {e.reason}"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL to submit to archive.org")
def main(url: str):
    """Submit a URL to archive.org for archiving."""

    config = load_config()

    # Check if feature is enabled
    if not config.ARCHIVEDOTORG_ENABLED:
        print(
            "Skipping archive.org submission (ARCHIVEDOTORG_ENABLED=False)",
            file=sys.stderr,
        )
        emit_archive_result_record("skipped", "ARCHIVEDOTORG_ENABLED=False")
        sys.exit(0)

    try:
        skip_reason = should_skip_archivedotorg_url(url)
        if skip_reason:
            emit_archive_result_record("noresults", skip_reason)
            sys.exit(0)

        # Run extraction
        success, output, error = submit_to_archivedotorg(url)

        if success:
            # Success - emit ArchiveResult with output file
            emit_archive_result_record(
                "succeeded",
                f"{PLUGIN_DIR}/{OUTPUT_FILE}" if output else "",
            )
            sys.exit(0)
        else:
            print(f"ERROR: {error}", file=sys.stderr)
            emit_archive_result_record("failed", error)
            sys.exit(1)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result_record("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()

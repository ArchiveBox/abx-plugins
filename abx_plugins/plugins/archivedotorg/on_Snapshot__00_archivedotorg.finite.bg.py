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
import re
from http.client import RemoteDisconnected
from ipaddress import ip_address
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from abx_plugins.plugins.base.utils import (
    build_request,
    emit_archive_result_record,
    iri_to_uri,
    load_config,
    write_text_atomic,
)

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


def log_http_error(error: HTTPError) -> None:
    # Keep service rejection evidence without dumping cookies or unbounded HTML.
    print(f"[archivedotorg] HTTP {error.code}", file=sys.stderr)
    for name in ("Content-Type", "Retry-After"):
        if value := error.headers.get(name):
            print(f"[archivedotorg] {name}: {value[:256]}", file=sys.stderr)
    try:
        body = error.read(2048).decode("utf-8", errors="replace")
        print(
            f"[archivedotorg] Error response body (first 2048 bytes): {body!r}",
            file=sys.stderr,
        )
    except TimeoutError:
        raise
    except OSError as exc:
        print(
            f"[archivedotorg] Error response body unavailable: {type(exc).__name__}",
            file=sys.stderr,
        )


def submit_to_archivedotorg(url: str) -> tuple[bool, str | None, str]:
    """
    Submit URL to archive.org Wayback Machine.

    Returns: (success, output_path, error_message)
    """

    def log(message: str) -> None:
        print(f"[archivedotorg] {message}", file=sys.stderr)

    config = load_config()
    timeout = config.ARCHIVEDOTORG_TIMEOUT
    user_agent = config.USER_AGENT

    save_endpoint = config.ARCHIVEDOTORG_SAVE_URL
    # Save Page Now takes the target in the request path, so a non-ASCII
    # snapshot URL must become an ASCII URI or http.client refuses to send it.
    submit_url = f"{save_endpoint}{iri_to_uri(url)}"
    log(f"Submitting to Wayback Machine (timeout={timeout}s)")
    log(f"GET {submit_url}")

    def deadline_expired(_signum, _frame):
        raise TimeoutError(f"Submission exceeded {timeout} seconds")

    # Socket timeouts reset on each read/redirect. This standalone POSIX hook
    # needs one deadline even when a server keeps trickling bytes indefinitely.
    previous_alarm_handler = signal.signal(signal.SIGALRM, deadline_expired)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        print("submitting to archive.org...", file=sys.stderr)
        req = build_request(submit_url, headers={"User-Agent": user_agent})
        try:
            with urlopen(req, timeout=timeout) as response:
                final_url = response.url
                log(f"HTTP {response.status} final_url={final_url}")
                content_location = response.headers.get("Content-Location", "")
                if content_location:
                    log(f"Content-Location: {content_location}")
                if original := response.headers.get("X-Archive-Orig-Url"):
                    log(f"X-Archive-Orig-Url: {original}")

                # A save-request or wildcard search URL is not capture proof.
                # Accept only timestamped replay locations on the configured origin.
                archive_url = (
                    urljoin(save_endpoint, content_location)
                    if content_location
                    else final_url
                )
                archived = urlparse(archive_url)
                save = urlparse(save_endpoint)
                archived_origin = (
                    archived.scheme,
                    archived.hostname,
                    archived.port or (443 if archived.scheme == "https" else 80),
                )
                save_origin = (
                    save.scheme,
                    save.hostname,
                    save.port or (443 if save.scheme == "https" else 80),
                )
                confirmed = (
                    archived_origin == save_origin
                    and not archived.username
                    and not archived.password
                    and re.match(
                        r"^/web/[0-9]{14}(?:[a-z]+_)?/https?://[^/]+",
                        archived.path,
                    )
                )
                # Replay proof is already in the headers/final URL. Read only
                # the bounded diagnostic body when there is no confirmed output.
                body = (
                    ""
                    if confirmed
                    else response.read(2048).decode("utf-8", errors="replace")
                )
        except HTTPError as error:
            with error:
                log_http_error(error)
            return False, None, f"Archive.org returned HTTP {error.code}"
        # The network exchange is complete. Do not interrupt publication after
        # its atomic rename and misreport a confirmed new output as a timeout.
        signal.setitimer(signal.ITIMER_REAL, 0)
        if confirmed:
            write_text_atomic(Path(OUTPUT_FILE), archive_url)
            log(f"Saved archive URL -> {archive_url}")
            return True, OUTPUT_FILE, ""
        if "RobotAccessControlException" in body:
            return False, None, "Archive.org blocked submission by robots.txt"
        return False, None, "Archive.org returned no archive URL"

    except TimeoutError:
        return False, None, f"Request timed out after {timeout} seconds"
    except RemoteDisconnected as e:
        return False, None, f"RemoteDisconnected: {e}"
    except URLError as e:
        return False, None, f"URLError: {e.reason}"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_alarm_handler)


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

        if success and output:
            emit_archive_result_record(
                "succeeded",
                f"{PLUGIN_DIR}/{OUTPUT_FILE}",
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

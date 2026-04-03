#!/usr/bin/env -S uv run --active --script
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
# Submit a URL to archive.org for archiving and save the resulting archive.org link.
#
# Usage:
#     ./on_Snapshot__08_archivedotorg.finite.bg.py --url=<url> > events.jsonl

import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
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


def submit_to_archivedotorg(url: str) -> tuple[bool, str | None, str]:
    """
    Submit URL to archive.org Wayback Machine.

    Returns: (success, output_path, error_message)
    """

    def log(message: str) -> None:
        print(f"[archivedotorg] {message}", file=sys.stderr)

    config = load_config()
    timeout = config.ARCHIVEDOTORG_TIMEOUT
    user_agent = (
        config.ARCHIVEDOTORG_USER_AGENT or "Mozilla/5.0 (compatible; ArchiveBox/1.0)"
    )

    submit_url = f"https://web.archive.org/save/{url}"
    log(f"Submitting to Wayback Machine (timeout={timeout}s)")
    log(f"GET {submit_url}")

    try:
        print("submitting to archive.org...")
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

        # Build archive URL
        if content_location:
            archive_url = f"https://web.archive.org{content_location}"
            Path(OUTPUT_FILE).write_text(archive_url, encoding="utf-8")
            log(f"Saved archive URL -> {archive_url}")
            return True, OUTPUT_FILE, ""
        elif "web.archive.org" in final_url:
            # We were redirected to an archive page
            Path(OUTPUT_FILE).write_text(final_url, encoding="utf-8")
            log(f"Redirected to archive page -> {final_url}")
            return True, OUTPUT_FILE, ""
        else:
            # Check for errors in response
            if "RobotAccessControlException" in body:
                # Blocked by robots.txt - save submit URL for manual retry
                Path(OUTPUT_FILE).write_text(submit_url, encoding="utf-8")
                log("Blocked by robots.txt, saved submit URL for manual retry")
                return True, OUTPUT_FILE, ""  # Consider this a soft success
            else:
                # Save submit URL anyway
                Path(OUTPUT_FILE).write_text(submit_url, encoding="utf-8")
                log("No archive URL returned, saved submit URL for manual retry")
                return True, OUTPUT_FILE, ""

    except HTTPError as e:
        if e.code >= 400:
            return False, None, f"HTTP {e.code}"
        return False, None, f"HTTPError: {e}"
    except TimeoutError:
        return False, None, f"Request timed out after {timeout} seconds"
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
            emit_archive_result_record("failed", error or "")
            sys.exit(1)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result_record("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()

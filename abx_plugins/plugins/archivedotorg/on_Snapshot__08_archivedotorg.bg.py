#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "rich-click",
#   "requests",
# ]
# ///
#
# Submit a URL to archive.org for archiving and save the resulting archive.org link.
#
# Usage:
#     ./on_Snapshot__08_archivedotorg.bg.py --url=<url> --snapshot-id=<uuid> > events.jsonl

import json
import os
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import load_config

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "archivedotorg"
PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
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

    try:
        requests: Any = import_module("requests")
    except ModuleNotFoundError:
        return False, None, "requests library not installed"

    config = load_config()
    timeout = config.ARCHIVEDOTORG_TIMEOUT
    user_agent = config.ARCHIVEDOTORG_USER_AGENT or "Mozilla/5.0 (compatible; ArchiveBox/1.0)"

    submit_url = f"https://web.archive.org/save/{url}"
    log(f"Submitting to Wayback Machine (timeout={timeout}s)")
    log(f"GET {submit_url}")

    try:
        response = requests.get(
            submit_url,
            timeout=timeout,
            headers={"User-Agent": user_agent},
            allow_redirects=True,
        )
        log(f"HTTP {response.status_code} final_url={response.url}")

        # Check for successful archive
        content_location = response.headers.get("Content-Location", "")
        x_archive_orig_url = response.headers.get("X-Archive-Orig-Url", "")
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
        elif "web.archive.org" in response.url:
            # We were redirected to an archive page
            Path(OUTPUT_FILE).write_text(response.url, encoding="utf-8")
            log(f"Redirected to archive page -> {response.url}")
            return True, OUTPUT_FILE, ""
        else:
            # Check for errors in response
            if "RobotAccessControlException" in response.text:
                # Blocked by robots.txt - save submit URL for manual retry
                Path(OUTPUT_FILE).write_text(submit_url, encoding="utf-8")
                log("Blocked by robots.txt, saved submit URL for manual retry")
                return True, OUTPUT_FILE, ""  # Consider this a soft success
            elif response.status_code >= 400:
                return False, None, f"HTTP {response.status_code}"
            else:
                # Save submit URL anyway
                Path(OUTPUT_FILE).write_text(submit_url, encoding="utf-8")
                log("No archive URL returned, saved submit URL for manual retry")
                return True, OUTPUT_FILE, ""

    except requests.Timeout:
        return False, None, f"Request timed out after {timeout} seconds"
    except requests.RequestException as e:
        return False, None, f"{type(e).__name__}: {e}"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


@click.command()
@click.option("--url", required=True, help="URL to submit to archive.org")
@click.option("--snapshot-id", required=True, help="Snapshot UUID")
def main(url: str, snapshot_id: str):
    """Submit a URL to archive.org for archiving."""

    config = load_config()

    # Check if feature is enabled
    if not config.ARCHIVEDOTORG_ENABLED:
        print(
            "Skipping archive.org submission (ARCHIVEDOTORG_ENABLED=False)",
            file=sys.stderr,
        )
        print(json.dumps({
            "type": "ArchiveResult",
            "status": "skipped",
            "output_str": "ARCHIVEDOTORG_ENABLED=False",
        }))
        sys.exit(0)

    try:
        # Run extraction
        success, output, error = submit_to_archivedotorg(url)

        if success:
            # Success - emit ArchiveResult with output file
            result = {
                "type": "ArchiveResult",
                "status": "succeeded",
                "output_str": OUTPUT_FILE if output else "",
            }
            print(json.dumps(result))
            sys.exit(0)
        else:
            print(f"ERROR: {error}", file=sys.stderr)
            print(json.dumps({
                "type": "ArchiveResult",
                "status": "failed",
                "output_str": error or "",
            }))
            sys.exit(1)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        print(json.dumps({
            "type": "ArchiveResult",
            "status": "failed",
            "output_str": error,
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()

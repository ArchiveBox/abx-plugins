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
# Clones a git repository from a provided URL into the current working directory.
# Supports configurable git arguments and timeout via environment variables.
#
# Usage:
#     ./on_Snapshot__05_git.finite.bg.py --url=<url> > events.jsonl

import os
import subprocess
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import emit_archive_result_record, load_config

import rich_click as click


# Extractor metadata
PLUGIN_NAME = "git"
BIN_NAME = "git"
BIN_PROVIDERS = "env,apt,brew"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)


def rel_output(path_str: str | None) -> str | None:
    if not path_str:
        return path_str
    path = Path(path_str)
    resolved = path.resolve()
    if not resolved.exists():
        return path_str
    try:
        return str(resolved.relative_to(SNAP_DIR.resolve()))
    except Exception:
        return path.name or path_str


def is_git_url(url: str) -> bool:
    """Check if URL looks like a git repository."""
    git_patterns = [
        ".git",
        "github.com",
        "gitlab.com",
        "bitbucket.org",
        "git://",
        "ssh://git@",
    ]
    return any(p in url.lower() for p in git_patterns)


def clone_git(url: str, binary: str) -> tuple[bool, str | None, str]:
    """
    Clone git repository.

    Returns: (success, output_path, error_message)
    """
    config = load_config()
    timeout = config.GIT_TIMEOUT
    git_args = config.GIT_ARGS
    git_args_extra = config.GIT_ARGS_EXTRA

    cmd = [binary, *git_args, *git_args_extra, url, OUTPUT_DIR]

    try:
        result = subprocess.run(cmd, timeout=timeout)

        if result.returncode == 0 and Path(OUTPUT_DIR).is_dir():
            return True, str(OUTPUT_DIR), ""
        else:
            return False, None, f"git clone failed (exit={result.returncode})"

    except subprocess.TimeoutExpired:
        return False, None, f"Timed out after {timeout} seconds"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="Git repository URL")
def main(url: str):
    """Clone a git repository from a URL."""

    output = None
    status = "failed"
    error = ""

    try:
        # Check if URL looks like a git repo
        if not is_git_url(url):
            print(f"Skipping git clone for non-git URL: {url}", file=sys.stderr)
            emit_archive_result_record("noresults", "Not a git URL")
            sys.exit(0)

        config = load_config()
        # Get binary from environment
        binary = config.GIT_BINARY

        # Run extraction
        success, output, error = clone_git(url, binary)
        status = "succeeded" if success else "failed"

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        status = "failed"

    if error:
        print(f"ERROR: {error}", file=sys.stderr)

    # Output clean JSONL (no RESULT_JSON= prefix)
    emit_archive_result_record(status, rel_output(output) or error or "")

    sys.exit(0 if status == "succeeded" else 1)


if __name__ == "__main__":
    main()

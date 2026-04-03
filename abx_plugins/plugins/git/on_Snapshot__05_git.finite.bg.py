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
# Clones a git repository from a provided URL into the current working directory.
# Supports configurable git arguments and timeout via environment variables.
#
# Usage:
#     ./on_Snapshot__05_git.finite.bg.py --url=<url> > events.jsonl

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

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
    """Return True only for URLs we can normalize into a cloneable repo URL."""
    return normalize_git_url(url) is not None


def normalize_git_url(url: str) -> str | None:
    """Normalize common repository page URLs to a repo clone URL."""
    lower_url = url.lower()
    if (
        lower_url.startswith("git://")
        or lower_url.startswith("ssh://git@")
        or lower_url.startswith("git@")
    ):
        return url
    if ".git" in lower_url:
        return url

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host.removeprefix("www.")

    git_domains = {
        domain.strip().lower()
        for domain in CONFIG.GIT_DOMAINS.split(",")
        if domain.strip()
    }
    if host not in git_domains:
        return None

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2:
        return None

    owner = path_parts[0]
    repo = path_parts[1]
    if not owner or not repo:
        return None

    return urlunsplit((parsed.scheme, parsed.netloc, f"/{owner}/{repo}", "", ""))


def clone_git(url: str, binary: str) -> tuple[bool, str | None, str]:
    """
    Clone git repository.

    Returns: (success, output_path, error_message)
    """
    config = load_config()
    timeout = config.GIT_TIMEOUT
    repo_dir = OUTPUT_DIR
    git_dir = repo_dir / ".git"
    exclude_file = git_dir / "info" / "exclude"

    try:
        if not git_dir.is_dir():
            init_cmd = [binary, "-C", repo_dir, "init"]
            add_remote_cmd = [binary, "-C", repo_dir, "remote", "add", "origin", url]
            for cmd, error_prefix in (
                (init_cmd, "git init failed"),
                (add_remote_cmd, "git remote add failed"),
            ):
                result = subprocess.run(cmd, timeout=timeout)
                if result.returncode != 0:
                    return False, None, f"{error_prefix} (exit={result.returncode})"

            exclude_file.parent.mkdir(parents=True, exist_ok=True)
            existing_excludes = (
                exclude_file.read_text() if exclude_file.exists() else ""
            )
            extra_excludes = [
                f"{PLUGIN_DIR}.jsonl",
                f"{Path(__file__).name}.sh",
                f"{Path(__file__).stem}.stdout.log",
                f"{Path(__file__).stem}.stderr.log",
                f"{Path(__file__).stem}.pid",
                f"{Path(__file__).stem}.stdout.*.log",
                f"{Path(__file__).stem}.stderr.*.log",
            ]
            with exclude_file.open("a") as fh:
                for pattern in extra_excludes:
                    if pattern not in existing_excludes:
                        fh.write(f"{pattern}\n")

        if git_dir.is_dir():
            set_remote_cmd = [
                binary,
                "-C",
                repo_dir,
                "remote",
                "set-url",
                "origin",
                url,
            ]
            fetch_cmd = [binary, "-C", repo_dir, "fetch", "--depth=1", "origin"]
            reset_cmd = [binary, "-C", repo_dir, "reset", "--hard", "FETCH_HEAD"]
            submodule_cmd = [
                binary,
                "-C",
                repo_dir,
                "submodule",
                "update",
                "--init",
                "--recursive",
                "--depth=1",
            ]
            for cmd, error_prefix in (
                (set_remote_cmd, "git remote set-url failed"),
                (fetch_cmd, "git fetch failed"),
                (reset_cmd, "git reset failed"),
                (submodule_cmd, "git submodule update failed"),
            ):
                result = subprocess.run(cmd, timeout=timeout)
                if result.returncode != 0:
                    return False, None, f"{error_prefix} (exit={result.returncode})"
            return True, str(repo_dir), ""
        return False, None, "git init failed"

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
        git_url = normalize_git_url(url)
        if git_url is None:
            print(f"Skipping git clone for non-git URL: {url}", file=sys.stderr)
            emit_archive_result_record("noresults", "Not a git URL")
            sys.exit(0)
        if git_url != url:
            print(f"Normalizing git URL to repo root: {git_url}", file=sys.stderr)

        config = load_config()
        # Get binary from environment
        binary = config.GIT_BINARY

        # Run extraction
        success, output, error = clone_git(git_url, binary)
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

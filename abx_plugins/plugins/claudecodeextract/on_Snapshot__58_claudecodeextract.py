#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich-click",
# ]
# ///
"""
Extract or transform snapshot content using Claude Code AI agent.

Requires the claudecode plugin to have installed Claude Code CLI.
Runs a user-configurable prompt against the snapshot directory,
allowing Claude to read existing extractor outputs and generate
new derived content.

Usage: on_Snapshot__58_claudecodeextract.py --url=<url> --snapshot-id=<uuid>
Output: Creates claudecodeextract/ directory with AI-generated output files

Environment variables:
    CLAUDECODEEXTRACT_ENABLED: Enable AI extraction (default: false)
    CLAUDECODEEXTRACT_PROMPT: Custom prompt for extraction
    CLAUDECODEEXTRACT_TIMEOUT: Timeout in seconds (default: 120)
    CLAUDECODEEXTRACT_MODEL: Claude model to use (default: sonnet)
    CLAUDECODEEXTRACT_MAX_TURNS: Max agentic turns (default: 10)
    ANTHROPIC_API_KEY: API key for Claude
"""

import json
import os
import sys
from pathlib import Path

import rich_click as click

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from claudecode.claudecode_utils import (
    build_system_prompt,
    emit_archive_result,
    get_env,
    get_env_bool,
    get_env_int,
    run_claude_code,
)


# Extractor metadata
PLUGIN_NAME = "claudecodeextract"
PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", SNAP_DIR.parent)).resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
# NOTE: OUTPUT_DIR is created after building the system prompt so that
# get_snapshot_metadata() doesn't list our own empty dir as an extractor output

DEFAULT_PROMPT = (
    "Read all the previously extracted outputs in this snapshot directory "
    "(readability/, mercury/, defuddle/, htmltotext/, dom/, singlefile/, etc.). "
    "Using the best available source, generate a clean, well-formatted Markdown "
    "representation of the page content. Save the output as content.md in your "
    "output directory."
)


@click.command()
@click.option("--url", required=True, help="URL being archived")
@click.option("--snapshot-id", required=True, help="Snapshot UUID")
def main(url: str, snapshot_id: str):
    """Extract or transform content using Claude Code AI agent."""

    try:
        # Check if enabled
        if not get_env_bool("CLAUDECODEEXTRACT_ENABLED", False):
            print("Skipping Claude Code extraction (CLAUDECODEEXTRACT_ENABLED=False)", file=sys.stderr)
            emit_archive_result("skipped", "CLAUDECODEEXTRACT_ENABLED=False")
            sys.exit(0)

        # Check for API key
        api_key = get_env("ANTHROPIC_API_KEY")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
            emit_archive_result("failed", "ANTHROPIC_API_KEY not set")
            sys.exit(1)

        # Get configuration
        user_prompt = get_env("CLAUDECODEEXTRACT_PROMPT", DEFAULT_PROMPT)
        timeout = get_env_int("CLAUDECODEEXTRACT_TIMEOUT") or get_env_int("CLAUDECODE_TIMEOUT", 120)
        model = get_env("CLAUDECODEEXTRACT_MODEL") or get_env("CLAUDECODE_MODEL", "sonnet")
        max_turns = get_env_int("CLAUDECODEEXTRACT_MAX_TURNS") or get_env_int("CLAUDECODE_MAX_TURNS", 10)

        # Build system prompt with snapshot context
        system_prompt = build_system_prompt(
            snap_dir=SNAP_DIR,
            crawl_dir=CRAWL_DIR,
            extra_context=(
                f"You are processing the snapshot for URL: {url}\n"
                f"Snapshot ID: {snapshot_id}\n\n"
                f"Your output directory is: {OUTPUT_DIR}\n"
                f"IMPORTANT: You MUST save all output files to exactly this directory: {OUTPUT_DIR}\n"
                "Do NOT save files anywhere else. Do NOT print output to stdout instead of writing files.\n"
                "You have read access to all sibling extractor directories in the snapshot."
            ),
        )

        # Create output dir after system prompt is built (so it's not listed as an extractor)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # Compose the full prompt
        full_prompt = (
            f"URL being archived: {url}\n\n"
            f"Your output directory (save files here): {OUTPUT_DIR}\n\n"
            f"Task:\n{user_prompt}"
        )

        # Run Claude Code
        stdout, stderr, returncode = run_claude_code(
            prompt=full_prompt,
            work_dir=SNAP_DIR,
            system_prompt=system_prompt,
            timeout=timeout,
            max_turns=max_turns,
            model=model,
            allowed_tools=[
                "Read",
                "Write",
                "Bash(cat:*)",
                "Bash(ls:*)",
                "Bash(find:*)",
                "Bash(head:*)",
                "Bash(tail:*)",
                "Bash(wc:*)",
            ],
            session_log_path=OUTPUT_DIR / "session.json",
        )

        if stderr:
            print(stderr, file=sys.stderr)

        # Save Claude's response
        if stdout:
            response_path = OUTPUT_DIR / "response.txt"
            response_path.write_text(stdout, encoding="utf-8")

        if returncode != 0:
            error_detail = stderr.strip().split("\n")[-1] if stderr else f"exit={returncode}"
            emit_archive_result("failed", f"Claude Code failed: {error_detail}")
            sys.exit(1)

        # Check what files were created (exclude metadata files that aren't actual extraction output)
        METADATA_FILES = {"response.txt", "session.json"}
        output_files = [
            f.name for f in OUTPUT_DIR.iterdir()
            if f.is_file() and not f.name.startswith(".") and f.name not in METADATA_FILES
        ]
        if not output_files:
            emit_archive_result("noresults", "No output files generated")
            sys.exit(0)

        output_str = ", ".join(sorted(output_files))
        print(f"[+] Claude Code generated: {output_str}", file=sys.stderr)
        emit_archive_result("succeeded", output_str)
        sys.exit(0)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic-settings",
#     "jambo",
#     "rich-click",
#     "abx-plugins",
# ]
# ///
"""
Extract or transform snapshot content using Claude Code AI agent.

Requires the claudecode plugin to have installed Claude Code CLI.
Runs a user-configurable prompt against the snapshot directory,
allowing Claude to read existing extractor outputs and generate
new derived content.

Usage: on_Snapshot__58_claudecodeextract.py --url=<url>
Output: Creates claudecodeextract/ directory with AI-generated output files

Environment variables:
    CLAUDECODEEXTRACT_ENABLED: Enable AI extraction (default: false)
    CLAUDECODEEXTRACT_PROMPT: Custom prompt for extraction
    CLAUDECODEEXTRACT_TIMEOUT: Timeout in seconds (default: 120)
    CLAUDECODEEXTRACT_MODEL: Claude model to use (default: claude-sonnet-4-6)
    CLAUDECODEEXTRACT_MAX_TURNS: Max agentic turns (default: 50)
    ANTHROPIC_API_KEY: API key for Claude
"""

import sys
from pathlib import Path

import rich_click as click

# Add parent directory to path for imports
from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    get_extra_context,
    load_config,
)
from abx_plugins.plugins.claudecode.claudecode_utils import (
    build_system_prompt,
    run_claude_code,
)


# Extractor metadata
PLUGIN_NAME = "claudecodeextract"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
CRAWL_DIR = Path(CONFIG.CRAWL_DIR or SNAP_DIR.parent).resolve()
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


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL being archived")
@click.option(
    "--snapshot-id",
    default="",
    help="Snapshot UUID override from EXTRA_CONTEXT (rarely needed)",
)
def main(url: str, snapshot_id: str):
    """Extract or transform content using Claude Code AI agent."""

    try:
        snapshot_id = snapshot_id or str(get_extra_context().get("snapshot_id") or "")

        # Check if enabled
        if not CONFIG.CLAUDECODEEXTRACT_ENABLED:
            print(
                "Skipping Claude Code extraction (CLAUDECODEEXTRACT_ENABLED=False)",
                file=sys.stderr,
            )
            emit_archive_result_record("skipped", "CLAUDECODEEXTRACT_ENABLED=False")
            sys.exit(0)

        # Check for API key
        api_key = str(CONFIG.ANTHROPIC_API_KEY or "")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
            emit_archive_result_record("failed", "ANTHROPIC_API_KEY not set")
            sys.exit(1)

        # Get configuration
        user_prompt = str(CONFIG.CLAUDECODEEXTRACT_PROMPT or DEFAULT_PROMPT)
        timeout = int(CONFIG.CLAUDECODEEXTRACT_TIMEOUT)
        model = str(CONFIG.CLAUDECODEEXTRACT_MODEL)
        max_turns = int(CONFIG.CLAUDECODEEXTRACT_MAX_TURNS)

        # Build system prompt with snapshot context
        system_prompt = build_system_prompt(
            snap_dir=SNAP_DIR,
            crawl_dir=CRAWL_DIR,
            extra_context=(
                f"You are processing the snapshot for URL: {url}\n"
                f"Snapshot ID: {snapshot_id}\n\n"
                f"Snapshot directory (your working directory): {SNAP_DIR}\n"
                f"Your output directory: {OUTPUT_DIR}\n\n"
                "## Scope & Permissions\n"
                f"You may READ any files within the snapshot directory: {SNAP_DIR}\n"
                f"You may CREATE and UPDATE files inside your output directory: {OUTPUT_DIR}\n"
                "Do NOT modify source extractor outputs outside your output directory.\n"
                "Do NOT print output to stdout instead of writing files.\n\n"
                "## Required Deliverable\n"
                "You must complete the task exactly as requested in the user prompt.\n"
                "Do not merely describe the output in your response. You must use the Write/Edit tools to create the file or files themselves.\n"
                "Before finishing, verify the output file or files exist and are non-empty by reading them or listing them from the filesystem.\n\n"
                "CRITICAL RESTRICTION: You MUST NOT read from or write to any path "
                f"outside of {SNAP_DIR}. Do not use absolute paths to other directories. "
                "Do not use .. to escape the snapshot directory."
            ),
        )

        # Create output dir after system prompt is built (so it's not listed as an extractor)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # Compose the full prompt
        full_prompt = (
            f"URL being archived: {url}\n\n"
            f"Your output directory (save files here): {OUTPUT_DIR}\n\n"
            f"Task:\n{user_prompt}\n\n"
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
            error_detail = (
                stderr.strip().split("\n")[-1] if stderr else f"exit={returncode}"
            )
            emit_archive_result_record("failed", f"Claude Code failed: {error_detail}")
            sys.exit(1)

        # Check what files were created (exclude metadata files that aren't actual extraction output)
        METADATA_FILES = {"response.txt", "session.json"}
        output_files = [
            f"{PLUGIN_DIR}/{f.name}"
            for f in OUTPUT_DIR.iterdir()
            if f.is_file()
            and not f.name.startswith(".")
            and f.name not in METADATA_FILES
        ]
        if not output_files:
            emit_archive_result_record("noresults", "No output files generated")
            sys.exit(0)

        output_str = ", ".join(sorted(output_files))
        print(f"[+] Claude Code generated: {output_str}", file=sys.stderr)
        emit_archive_result_record("succeeded", output_str)
        sys.exit(0)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result_record("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()

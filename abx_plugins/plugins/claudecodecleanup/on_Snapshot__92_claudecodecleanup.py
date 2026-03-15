#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "rich-click",
# ]
# ///
"""
Clean up redundant/duplicate snapshot outputs using Claude Code AI agent.

Runs near the end of the snapshot pipeline (priority 92, before hashes at 93)
to analyze all extractor outputs, identify duplicates and redundant files,
and keep only the best version of each.

Requires the claudecode plugin to have installed Claude Code CLI.

Usage: on_Snapshot__92_claudecodecleanup.py --url=<url> --snapshot-id=<uuid>
Output: Creates claudecodecleanup/ directory with cleanup_report.txt

Environment variables:
    CLAUDECODECLEANUP_ENABLED: Enable AI cleanup (default: false)
    CLAUDECODECLEANUP_PROMPT: Custom prompt for cleanup behavior
    CLAUDECODECLEANUP_TIMEOUT: Timeout in seconds (default: 120)
    CLAUDECODECLEANUP_MODEL: Claude model to use (default: sonnet)
    CLAUDECODECLEANUP_MAX_TURNS: Max agentic turns (default: 15)
    ANTHROPIC_API_KEY: API key for Claude
"""

import json
import os
import sys
from pathlib import Path

import rich_click as click

# Add parent directory to path for imports
sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import emit_archive_result, get_env, get_env_bool, get_env_int
from claudecode.claudecode_utils import build_system_prompt, run_claude_code


# Extractor metadata
PLUGIN_NAME = "claudecodecleanup"
PLUGIN_DIR = Path(__file__).resolve().parent.name
SNAP_DIR = Path(os.environ.get("SNAP_DIR", ".")).resolve()
CRAWL_DIR = Path(os.environ.get("CRAWL_DIR", SNAP_DIR.parent)).resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
# NOTE: OUTPUT_DIR is created after building the system prompt so that
# get_snapshot_metadata() doesn't list our own empty dir as an extractor output

DEFAULT_PROMPT = (
    "Analyze all the extractor output directories in this snapshot. "
    "Look for duplicate or redundant outputs across plugins "
    "(e.g. multiple HTML extractions, multiple text extractions, "
    "multiple URL extraction outputs, etc.). "
    "For each group of similar outputs, inspect the content and determine "
    "which version is the best quality. Delete the inferior/redundant versions, "
    "keeping only the best one. Also remove any unnecessary temporary files, "
    "empty directories, or incomplete outputs. "
    "Write a summary of what you cleaned up to cleanup_report.txt in your output directory."
)


@click.command()
@click.option("--url", required=True, help="URL being archived")
@click.option("--snapshot-id", required=True, help="Snapshot UUID")
def main(url: str, snapshot_id: str):
    """Clean up redundant snapshot outputs using Claude Code AI agent."""

    try:
        # Check if enabled
        if not get_env_bool("CLAUDECODECLEANUP_ENABLED", False):
            print("Skipping Claude Code cleanup (CLAUDECODECLEANUP_ENABLED=False)", file=sys.stderr)
            emit_archive_result("skipped", "CLAUDECODECLEANUP_ENABLED=False")
            sys.exit(0)

        # Check for API key
        api_key = get_env("ANTHROPIC_API_KEY")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
            emit_archive_result("failed", "ANTHROPIC_API_KEY not set")
            sys.exit(1)

        # Get configuration
        user_prompt = get_env("CLAUDECODECLEANUP_PROMPT", DEFAULT_PROMPT)
        timeout = get_env_int("CLAUDECODECLEANUP_TIMEOUT") or get_env_int("CLAUDECODE_TIMEOUT", 120)
        model = get_env("CLAUDECODECLEANUP_MODEL") or get_env("CLAUDECODE_MODEL", "sonnet")
        max_turns = get_env_int("CLAUDECODECLEANUP_MAX_TURNS") or get_env_int("CLAUDECODE_MAX_TURNS", 15)

        # Build system prompt with snapshot context
        system_prompt = build_system_prompt(
            snap_dir=SNAP_DIR,
            crawl_dir=CRAWL_DIR,
            extra_context=(
                f"You are performing cleanup on the snapshot for URL: {url}\n"
                f"Snapshot ID: {snapshot_id}\n\n"
                f"Snapshot directory (your working directory): {SNAP_DIR}\n"
                f"Your output directory: {OUTPUT_DIR}\n\n"
                "## Scope & Permissions\n"
                f"You have FULL permissions (read, write, rename, move, delete) within "
                f"the snapshot directory: {SNAP_DIR}\n"
                "You may run any bash commands you need (rm, mv, cp, find, etc.) as long as "
                "ALL paths are within the snapshot directory above.\n\n"
                "CRITICAL RESTRICTION: You MUST NOT read, write, modify, or delete anything "
                f"outside of {SNAP_DIR}. Do not use absolute paths to other directories. "
                "Do not use .. to escape the snapshot directory. Every file operation must "
                "target a path within the snapshot directory.\n\n"
                "## Procedure\n"
                "1. First, list and inspect all extractor output directories\n"
                "2. Identify groups of similar/redundant outputs\n"
                "3. Compare quality within each group\n"
                "4. Delete only the clearly inferior/redundant versions\n"
                "5. Never delete the hashes/ directory or any .json metadata files\n"
                "6. REQUIRED: You MUST write a detailed report of what was cleaned up "
                f"and why to exactly this path: {OUTPUT_DIR}/cleanup_report.txt\n"
                "   This file MUST exist when you are done. Always create it, even if "
                "   no cleanup was needed (in that case, explain why nothing was removed)."
            ),
        )

        # Create output dir after system prompt is built (so it's not listed as an extractor)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # Compose the full prompt
        full_prompt = (
            f"URL being archived: {url}\n\n"
            f"Snapshot directory: {SNAP_DIR}\n"
            f"Your output directory: {OUTPUT_DIR}\n\n"
            f"Task:\n{user_prompt}\n\n"
            f"IMPORTANT: When finished, you MUST write your report to "
            f"{OUTPUT_DIR}/cleanup_report.txt"
        )

        # Run Claude Code with full permissions within SNAP_DIR.
        # Path scoping is enforced via system prompt (Claude Code's --allowedTools
        # cannot restrict by path, only by command name).
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
                "Edit",
                "Bash",
                "Glob",
                "Grep",
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

        # Check for cleanup report
        report_path = OUTPUT_DIR / "cleanup_report.txt"
        if report_path.exists():
            report_size = report_path.stat().st_size
            print(f"[+] Cleanup report: {report_size} bytes", file=sys.stderr)
            emit_archive_result("succeeded", "cleanup_report.txt")
        else:
            # Check for output files (exclude metadata that isn't actual cleanup output)
            METADATA_FILES = {"response.txt", "session.json"}
            output_files = [
                f.name for f in OUTPUT_DIR.iterdir()
                if f.is_file() and not f.name.startswith(".") and f.name not in METADATA_FILES
            ]
            if output_files:
                emit_archive_result("succeeded", ", ".join(sorted(output_files)))
            else:
                emit_archive_result("succeeded", "cleanup completed (no report)")

        sys.exit(0)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()

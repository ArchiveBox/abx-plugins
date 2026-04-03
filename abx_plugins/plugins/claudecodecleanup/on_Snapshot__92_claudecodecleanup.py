#!/usr/bin/env -S uv run --script
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
Clean up redundant/duplicate snapshot outputs using Claude Code AI agent.

Runs near the end of the snapshot pipeline (priority 92, before hashes at 93)
to analyze all extractor outputs, identify duplicates and redundant files,
and keep only the best version of each.

Requires the claudecode plugin to have installed Claude Code CLI.

Usage: on_Snapshot__92_claudecodecleanup.py --url=<url>
Output: Creates claudecodecleanup/ directory with cleanup_report.txt

Environment variables:
    CLAUDECODECLEANUP_ENABLED: Enable AI cleanup (default: false)
    CLAUDECODECLEANUP_PROMPT: Custom prompt for cleanup behavior
    CLAUDECODECLEANUP_TIMEOUT: Timeout in seconds (default: 180)
    CLAUDECODECLEANUP_MODEL: Claude model to use (default: claude-sonnet-4-6)
    CLAUDECODECLEANUP_MAX_TURNS: Max agentic turns (default: 50)
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
PLUGIN_NAME = "claudecodecleanup"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
CRAWL_DIR = Path(CONFIG.CRAWL_DIR or SNAP_DIR.parent).resolve()
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
    """Clean up redundant snapshot outputs using Claude Code AI agent."""

    try:
        snapshot_id = snapshot_id or str(get_extra_context().get("snapshot_id") or "")

        # Check if enabled
        if not CONFIG.CLAUDECODECLEANUP_ENABLED:
            print(
                "Skipping Claude Code cleanup (CLAUDECODECLEANUP_ENABLED=False)",
                file=sys.stderr,
            )
            emit_archive_result_record("skipped", "CLAUDECODECLEANUP_ENABLED=False")
            sys.exit(0)

        # Check for API key
        api_key = str(CONFIG.ANTHROPIC_API_KEY or "")
        if not api_key:
            print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
            emit_archive_result_record("failed", "ANTHROPIC_API_KEY not set")
            sys.exit(1)

        # Get configuration
        user_prompt = str(CONFIG.CLAUDECODECLEANUP_PROMPT or DEFAULT_PROMPT)
        timeout = int(CONFIG.CLAUDECODECLEANUP_TIMEOUT)
        model = str(CONFIG.CLAUDECODECLEANUP_MODEL)
        max_turns = int(CONFIG.CLAUDECODECLEANUP_MAX_TURNS)

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
                f"You may write your report to {OUTPUT_DIR}/cleanup_report.txt and you may "
                "modify or delete redundant extractor outputs inside the snapshot directory.\n"
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
                "   no cleanup was needed (in that case, explain why nothing was removed).\n"
                "7. The task is not complete until cleanup_report.txt exists on disk.\n"
                "   Do not merely describe the cleanup in your response. You must use Write/Edit "
                "   to create the file itself, then verify it exists and is non-empty."
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
            f"{OUTPUT_DIR}/cleanup_report.txt\n"
            "Completion requirements:\n"
            "1. cleanup_report.txt must exist on disk before you finish\n"
            "2. Do not rely on your chat response as the report\n"
            "3. Verify the file exists and is non-empty before finishing"
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
            error_detail = (
                stderr.strip().split("\n")[-1] if stderr else f"exit={returncode}"
            )
            emit_archive_result_record("failed", f"Claude Code failed: {error_detail}")
            sys.exit(1)

        # Check for cleanup report
        report_path = OUTPUT_DIR / "cleanup_report.txt"
        if report_path.exists():
            report_size = report_path.stat().st_size
            print(f"[+] Cleanup report: {report_size} bytes", file=sys.stderr)
            emit_archive_result_record("succeeded", f"{PLUGIN_DIR}/cleanup_report.txt")
        else:
            emit_archive_result_record("failed", "cleanup_report.txt was not created")

        sys.exit(0)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result_record("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()

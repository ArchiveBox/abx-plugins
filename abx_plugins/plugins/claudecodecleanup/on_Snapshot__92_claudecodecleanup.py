#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""
Clean up redundant/duplicate snapshot outputs using Claude Code AI agent.

Runs near the end of the snapshot pipeline (priority 92, before hashes at 93)
to analyze all extractor outputs, identify duplicates and redundant files,
and keep only the best version of each.

Resolves the Claude Code CLI through the claudecode plugin's required binaries.

Usage: on_Snapshot__92_claudecodecleanup.py --url=<url>
Output: Creates claudecodecleanup/ directory with cleanup_report.txt

Environment variables:
    CLAUDECODECLEANUP_ENABLED: Enable AI cleanup (default: false)
    CLAUDECODECLEANUP_PROMPT: Custom prompt for cleanup behavior
    CLAUDECODECLEANUP_TIMEOUT: Timeout in seconds (default: 180)
    CLAUDECODECLEANUP_MODEL: Claude model to use (default: claude-sonnet-4-6)
    CLAUDECODECLEANUP_MAX_TURNS: Max agentic turns (default: 50)
    ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN: Claude Code auth
"""

import os
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
    "Complete one cleanup pass in at most three Bash calls. First, use one Bash call "
    "to inspect every listed extractor output as a single batch: recursively collect "
    "each file's path, size, and type; hash same-size duplicate candidates; and collect "
    "at most 200 bytes per text-like file with at most 64 KiB of inspection output total. "
    "Do not run a separate command per file. If that batch leaves a "
    "genuine ambiguity, use at most one additional batched Bash call covering all "
    "ambiguous files together; otherwise skip it. From that evidence, keep the best "
    "output in each redundant group and, in one Bash call, delete only clearly inferior "
    "duplicates, incomplete or failed outputs, and empty directories; when uncertain, "
    "keep the output. Never delete hashes/ or any JSON metadata. Never read, modify, "
    "rename, or delete ArchiveBox process-control files ending in .stdout.log, "
    ".stderr.log, .pid, or .sh; they may belong to processes still running. Then stop "
    "using tools and return a concise final report. Name every extractor directory "
    "inspected, list every deletion, summarize every retained duplicate group, and keep "
    "the report under 500 words. Do not re-list, re-read, verify, narrate further, or "
    "revisit any decision."
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

        # Check for real Claude Code auth before doing expensive prompt setup.
        # The CLI accepts either the API key path or the OAuth token path used
        # by the official GitHub Action; do not force one credential type.
        api_key = str(CONFIG.ANTHROPIC_API_KEY or "")
        oauth_token = str(
            getattr(CONFIG, "CLAUDE_CODE_OAUTH_TOKEN", "")
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            or "",
        )
        if not api_key and not oauth_token:
            print(
                "ERROR: ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN not set",
                file=sys.stderr,
            )
            emit_archive_result_record("failed", "Claude Code auth not set")
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
                "You may modify or delete redundant extractor outputs inside the snapshot "
                "directory.\n"
                "CRITICAL RESTRICTION: You MUST NOT read, write, modify, or delete anything "
                f"outside of {SNAP_DIR}. Do not use absolute paths to other directories. "
                "Do not use .. to escape the snapshot directory. Every file operation must "
                "target a path within the snapshot directory.\n\n"
                "## Invariants\n"
                "- Never delete hashes/ or any .json metadata file.\n"
                "- Never read, write, modify, rename, or delete ArchiveBox process-control "
                "files ending in .stdout.log, .stderr.log, .pid, or .sh; they may belong "
                "to processes that are still running.\n"
                "- Inspect a file or directory at most once and do not revisit completed decisions.\n"
                "- Make one cleanup pass; do not repeatedly inventory or verify the snapshot.\n"
                "- Finish with a concise, non-empty final response that reports the cleanup, "
                "even when nothing was removed. The hook will save that response."
            ),
        )

        # Create output dir after system prompt is built (so it's not listed as an extractor)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # Compose the full prompt
        full_prompt = (
            f"URL being archived: {url}\n\n"
            f"Task:\n{user_prompt}\n\n"
            "Return the cleanup report as your final response and then stop."
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
            allowed_tools=["Bash"],
            session_log_path=OUTPUT_DIR / "session.json",
        )

        if stderr:
            print(stderr, file=sys.stderr)

        # Claude performs inspection and cleanup; the hook persists its final report.
        if stdout:
            response_path = OUTPUT_DIR / "response.txt"
            response_path.write_text(stdout, encoding="utf-8")

        if returncode != 0:
            error_detail = (
                stderr.strip().split("\n")[-1] if stderr else f"exit={returncode}"
            )
            emit_archive_result_record("failed", f"Claude Code failed: {error_detail}")
            sys.exit(1)

        if stdout.strip():
            (OUTPUT_DIR / "cleanup_report.txt").write_text(stdout, encoding="utf-8")

        # Check for cleanup report
        report_path = OUTPUT_DIR / "cleanup_report.txt"
        if report_path.exists():
            report_size = report_path.stat().st_size
            print(f"[+] Cleanup report: {report_size} bytes", file=sys.stderr)
            emit_archive_result_record("succeeded", f"{PLUGIN_DIR}/cleanup_report.txt")
        else:
            emit_archive_result_record("failed", "cleanup_report.txt was not created")
            sys.exit(1)

        sys.exit(0)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result_record("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()

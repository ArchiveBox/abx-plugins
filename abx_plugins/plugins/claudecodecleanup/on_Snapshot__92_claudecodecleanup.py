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
import json
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
from abx_plugins.plugins.claudecodecleanup.cleanup_utils import (
    apply_cleanup_deletions,
    build_cleanup_inventory_with_capabilities,
    ensure_owned_output_dir,
    validate_snapshot_ledger,
    write_owned_output_file,
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
    "Use the deterministic inventory supplied below; do not inventory the "
    "snapshot again. From that evidence, keep the best output in each redundant group and "
    "select deletion ids only for clearly inferior duplicates, incomplete or failed outputs, "
    "and empty directories; when uncertain, keep the output. Never request deletion of "
    "hashes/, claudecodecleanup/, JSON metadata, or ArchiveBox process-control files. "
    "Return a concise report naming every extractor directory considered, every requested "
    "deletion, and every retained duplicate group."
)

DELETION_PLAN_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "delete_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Opaque deletion ids present in the supplied inventory.",
        },
        "report": {
            "type": "string",
            "description": "Concise cleanup decision report under 500 words.",
        },
    },
    "required": ["delete_ids", "report"],
    "additionalProperties": False,
}


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

        _snap_dir, allowed_directories = validate_snapshot_ledger(
            SNAP_DIR,
            snapshot_id,
            url,
        )

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
                "You have no filesystem or shell tools. Decide only from the supplied "
                "inventory and return the required structured deletion plan. ArchiveBox "
                "will validate and apply safe relative paths.\n\n"
                "## Invariants\n"
                "- Never request hashes/, claudecodecleanup/, JSON metadata, or process-control files.\n"
                "- Every delete_ids item must be an opaque id from FILE_METADATA or EMPTY_DIRECTORIES.\n"
                "- Finish with a concise, non-empty report even when nothing should be removed."
            ),
        )

        # Create output dir after system prompt is built (so it's not listed as an extractor)
        ensure_owned_output_dir(SNAP_DIR, OUTPUT_DIR)

        inventory, capabilities = build_cleanup_inventory_with_capabilities(
            SNAP_DIR,
            OUTPUT_DIR,
            allowed_directories=allowed_directories,
        )

        # Compose the full prompt
        full_prompt = (
            f"URL being archived: {url}\n\n"
            f"Task:\n{user_prompt}\n\n"
            f"Deterministic snapshot inventory:\n{inventory}\n\n"
            "Return the structured deletion plan and report."
        )

        stdout, stderr, returncode = run_claude_code(
            prompt=full_prompt,
            work_dir=OUTPUT_DIR,
            system_prompt=system_prompt,
            timeout=timeout,
            max_turns=max_turns,
            model=model,
            allowed_tools=[],
            json_schema=DELETION_PLAN_SCHEMA,
            isolated=True,
            session_log_path=OUTPUT_DIR / "session.json",
        )

        if stderr:
            print(stderr, file=sys.stderr)

        if returncode != 0:
            error_detail = (
                stderr.strip().split("\n")[-1] if stderr else f"exit={returncode}"
            )
            emit_archive_result_record("failed", f"Claude Code failed: {error_detail}")
            sys.exit(1)

        plan = json.loads(stdout)
        requested_ids = plan["delete_ids"]
        report = str(plan["report"]).strip()
        if not report:
            raise ValueError("Claude Code returned an empty cleanup report")
        deleted_paths = apply_cleanup_deletions(
            SNAP_DIR,
            OUTPUT_DIR,
            snapshot_id,
            url,
            capabilities,
            requested_ids,
        )
        applied = "\n".join(f"- {path}" for path in deleted_paths) or "- None"
        final_report = f"{report}\n\nApplied deletions:\n{applied}\n"
        write_owned_output_file(SNAP_DIR, OUTPUT_DIR, "response.txt", final_report)
        write_owned_output_file(
            SNAP_DIR,
            OUTPUT_DIR,
            "cleanup_report.txt",
            final_report,
        )

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

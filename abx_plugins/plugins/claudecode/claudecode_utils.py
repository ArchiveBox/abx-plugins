#!/usr/bin/env python3
"""
Shared utilities for Claude Code plugins.

Provides functions to spawn Claude Code CLI with appropriate system prompts
describing the crawl/snapshot directory layout and current metadata.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def get_env_bool(name: str, default: bool = False) -> bool:
    val = get_env(name, "").lower()
    if val in ("true", "1", "yes", "on"):
        return True
    if val in ("false", "0", "no", "off"):
        return False
    return default


def get_env_int(name: str, default: int = 0) -> int:
    try:
        return int(get_env(name, str(default)))
    except ValueError:
        return default


def get_crawl_metadata(crawl_dir: Path) -> dict:
    """Read crawl metadata from the crawl directory."""
    metadata = {
        "crawl_dir": str(crawl_dir),
    }

    # Try to read crawl metadata files
    for meta_file in ("crawl.json", "metadata.json", "config.json"):
        meta_path = crawl_dir / meta_file
        if meta_path.exists():
            try:
                metadata[meta_file] = json.loads(meta_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

    return metadata


def get_snapshot_metadata(snap_dir: Path) -> dict:
    """Read snapshot metadata from the snapshot directory."""
    metadata = {
        "snap_dir": str(snap_dir),
    }

    # List existing extractor output directories
    if snap_dir.exists():
        extractor_dirs = []
        for item in sorted(snap_dir.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                files = []
                try:
                    files = [f.name for f in item.iterdir() if f.is_file()]
                except OSError:
                    pass
                extractor_dirs.append({
                    "name": item.name,
                    "files": files,
                })
        metadata["extractor_outputs"] = extractor_dirs

    return metadata


def build_system_prompt(
    snap_dir: Path | None = None,
    crawl_dir: Path | None = None,
    extra_context: str = "",
) -> str:
    """Build a system prompt describing the archive environment."""
    parts = []

    parts.append(
        "You are an AI agent running inside ArchiveBox, a self-hosted web archiving tool. "
        "You have access to the filesystem and can read/write files."
    )

    parts.append(
        "\n## Directory Layout\n"
        "ArchiveBox organizes archives in a two-level hierarchy:\n"
        "- **Crawl directory** (`CRAWL_DIR`): The top-level directory for a crawl job. "
        "Contains crawl-wide config, logs, and plugin outputs.\n"
        "- **Snapshot directory** (`SNAP_DIR`): Each URL being archived gets its own snapshot directory "
        "inside the crawl. Contains per-URL extractor outputs.\n"
    )

    if crawl_dir and crawl_dir.exists():
        crawl_meta = get_crawl_metadata(crawl_dir)
        parts.append(f"\n## Current Crawl\n```\nCRAWL_DIR={crawl_meta['crawl_dir']}\n```\n")

    if snap_dir and snap_dir.exists():
        snap_meta = get_snapshot_metadata(snap_dir)
        parts.append(f"\n## Current Snapshot\n```\nSNAP_DIR={snap_meta['snap_dir']}\n```\n")

        if snap_meta.get("extractor_outputs"):
            parts.append("### Extractor Outputs Available\n")
            for ext in snap_meta["extractor_outputs"]:
                file_list = ", ".join(ext["files"][:10])
                if len(ext["files"]) > 10:
                    file_list += f", ... (+{len(ext['files']) - 10} more)"
                parts.append(f"- **{ext['name']}/**: {file_list}")
            parts.append("")

    parts.append(
        "\n## Snapshot Directory Layout\n"
        "Each snapshot directory contains subdirectories for each extractor plugin:\n"
        "```\n"
        "<snap_dir>/\n"
        "  favicon/           # Favicon files\n"
        "  screenshot/        # Full-page screenshot PNG\n"
        "  dom/               # Raw DOM HTML dump\n"
        "  singlefile/        # SingleFile self-contained HTML\n"
        "  readability/       # Readability article extraction (content.html, content.txt, article.json)\n"
        "  mercury/           # Mercury parser extraction\n"
        "  defuddle/          # Defuddle extraction\n"
        "  htmltotext/        # HTML-to-text conversion\n"
        "  wget/              # wget mirror of the page\n"
        "  media/             # Media files (youtube-dl)\n"
        "  pdf/               # PDF rendering of the page\n"
        "  headers/           # HTTP headers\n"
        "  hashes/            # File hashes (Merkle tree)\n"
        "  ...\n"
        "```\n"
    )

    if extra_context:
        parts.append(f"\n## Additional Instructions\n{extra_context}\n")

    return "\n".join(parts)


def run_claude_code(
    prompt: str,
    work_dir: str | Path,
    system_prompt: str = "",
    timeout: int = 120,
    max_turns: int = 10,
    model: str = "sonnet",
    allowed_tools: list[str] | None = None,
) -> tuple[str, str, int]:
    """
    Run Claude Code CLI with the given prompt and configuration.

    Returns: (stdout, stderr, returncode)
    """
    binary = get_env("CLAUDECODE_BINARY", "claude")

    cmd = [binary]

    # Add print flag for non-interactive output
    cmd.extend(["--print"])

    # Add model
    cmd.extend(["--model", model])

    # Add max turns
    cmd.extend(["--max-turns", str(max_turns)])

    # Add system prompt
    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    # Add allowed tools (restrict to safe tools by default)
    if allowed_tools:
        for tool in allowed_tools:
            cmd.extend(["--allowedTools", tool])
    else:
        # Default: allow read, write, and bash (no destructive tools)
        cmd.extend(["--allowedTools", "Read"])
        cmd.extend(["--allowedTools", "Write"])
        cmd.extend(["--allowedTools", "Bash(cat:*)"])
        cmd.extend(["--allowedTools", "Bash(ls:*)"])
        cmd.extend(["--allowedTools", "Bash(find:*)"])
        cmd.extend(["--allowedTools", "Bash(head:*)"])
        cmd.extend(["--allowedTools", "Bash(tail:*)"])
        cmd.extend(["--allowedTools", "Bash(wc:*)"])

    # Add the prompt
    cmd.extend(["--", prompt])

    env = os.environ.copy()

    # Ensure API key is set
    api_key = get_env("ANTHROPIC_API_KEY")
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key

    print(f"[*] Running Claude Code in {work_dir}...", file=sys.stderr)
    print(f"[*] Model: {model}, Max turns: {max_turns}, Timeout: {timeout}s", file=sys.stderr)

    try:
        result = subprocess.run(
            cmd,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "", f"Claude Code timed out after {timeout} seconds", 1
    except FileNotFoundError:
        return "", f"Claude Code binary not found: {binary}", 1
    except Exception as e:
        return "", f"{type(e).__name__}: {e}", 1


def emit_archive_result(status: str, output_str: str) -> None:
    print(
        json.dumps(
            {
                "type": "ArchiveResult",
                "status": status,
                "output_str": output_str,
            }
        )
    )

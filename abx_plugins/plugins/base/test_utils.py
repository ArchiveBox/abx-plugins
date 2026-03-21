"""Shared test utilities for abx plugins.

Provides common helpers used across plugin test files:
- Plugin/hook discovery (get_plugin_dir, get_hook_script)
- JSONL output parsing (parse_jsonl_output, parse_jsonl_records)
- Hook execution (run_hook, run_hook_and_parse)

Usage::

    from abx_plugins.plugins.base.test_utils import (
        get_plugin_dir, get_hook_script,
        parse_jsonl_output, parse_jsonl_records,
        run_hook, run_hook_and_parse,
    )
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SNAPSHOT_ISOLATION_ENV_KEYS = ("HOME", "SNAP_DIR", "LIB_DIR", "PERSONAS_DIR")


def get_plugin_dir(test_file: str) -> Path:
    """Get the plugin directory from a test file path.

    Usage:
        PLUGIN_DIR = get_plugin_dir(__file__)

    Args:
        test_file: The __file__ of the test module (e.g., test_screenshot.py)

    Returns:
        Path to the plugin directory (e.g., plugins/screenshot/)
    """
    return Path(test_file).parent.parent


def get_hook_script(plugin_dir: Path, pattern: str) -> Path | None:
    """Find a hook script in a plugin directory by pattern.

    Usage:
        HOOK = get_hook_script(PLUGIN_DIR, 'on_Snapshot__*_screenshot.*')

    Args:
        plugin_dir: Path to the plugin directory
        pattern: Glob pattern to match

    Returns:
        Path to the hook script or None if not found
    """
    matches = list(plugin_dir.glob(pattern))
    return matches[0] if matches else None


def parse_jsonl_output(
    stdout: str,
    record_type: str = "ArchiveResult",
) -> dict[str, Any] | None:
    """Parse JSONL output from hook stdout and return the first matching record.

    Usage:
        result = parse_jsonl_output(stdout)
        if result and result['status'] == 'succeeded':
            print("Success!")

    Args:
        stdout: The stdout from a hook execution
        record_type: The 'type' field to look for (default: 'ArchiveResult')

    Returns:
        The parsed JSON dict or None if not found
    """
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
            if record.get("type") == record_type:
                return record
        except json.JSONDecodeError:
            continue
    return None


def parse_jsonl_records(stdout: str) -> list[dict[str, Any]]:
    """Parse all JSONL records from stdout."""
    records: list[dict[str, Any]] = []
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def find_snapshot_env_path_collisions(
    env: Mapping[str, str | os.PathLike[str]],
) -> list[str]:
    """Return collisions where runtime support dirs overlap with SNAP_DIR."""
    raw_paths = {
        key: value for key in SNAPSHOT_ISOLATION_ENV_KEYS if (value := env.get(key))
    }
    if "SNAP_DIR" not in raw_paths:
        return []

    paths = {
        key: Path(value).expanduser().resolve(strict=False)
        for key, value in raw_paths.items()
    }
    snap_dir = paths["SNAP_DIR"]
    collisions: list[str] = []

    for key, path in paths.items():
        if key == "SNAP_DIR":
            continue
        if path == snap_dir:
            collisions.append(f"{key} must not equal SNAP_DIR ({snap_dir})")
            continue
        if snap_dir in path.parents:
            collisions.append(f"{key} must not be nested under SNAP_DIR ({path})")

    return collisions


def assert_isolated_snapshot_env(
    env: Mapping[str, str | os.PathLike[str]],
) -> None:
    """Assert that support dirs cannot pollute SNAP_DIR in tests."""
    collisions = find_snapshot_env_path_collisions(env)
    if collisions:
        details = "; ".join(collisions)
        raise AssertionError(
            f"Test runtime directories must stay isolated from SNAP_DIR. {details}",
        )


def run_hook(
    hook_script: Path,
    url: str,
    snapshot_id: str,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 60,
    extra_args: list[str] | None = None,
) -> tuple[int, str, str]:
    """Run a hook script and return (returncode, stdout, stderr).

    Usage:
        returncode, stdout, stderr = run_hook(
            HOOK, 'https://example.com', 'test-snap-123',
            cwd=tmpdir, env=os.environ.copy()
        )

    Args:
        hook_script: Path to the hook script
        url: URL to process
        snapshot_id: Snapshot ID
        cwd: Working directory (default: current dir)
        env: Environment dict (default: os.environ copy)
        timeout: Timeout in seconds
        extra_args: Additional arguments to pass

    Returns:
        Tuple of (returncode, stdout, stderr)
    """
    if env is None:
        env = os.environ.copy()

    assert_isolated_snapshot_env(env)

    cmd = [str(hook_script)]

    cmd.extend([f"--url={url}", f"--snapshot-id={snapshot_id}"])
    if extra_args:
        cmd.extend(extra_args)

    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def run_hook_and_parse(
    hook_script: Path,
    url: str,
    snapshot_id: str,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 60,
    extra_args: list[str] | None = None,
    record_type: str = "ArchiveResult",
) -> tuple[int, dict[str, Any] | None, str]:
    """Run a hook and parse the first matching JSONL record.

    Convenience wrapper combining run_hook() + parse_jsonl_output().

    Returns:
        Tuple of (returncode, parsed_record_or_None, stderr)
    """
    returncode, stdout, stderr = run_hook(
        hook_script,
        url,
        snapshot_id,
        cwd=cwd,
        env=env,
        timeout=timeout,
        extra_args=extra_args,
    )
    record = parse_jsonl_output(stdout, record_type=record_type)
    return returncode, record, stderr

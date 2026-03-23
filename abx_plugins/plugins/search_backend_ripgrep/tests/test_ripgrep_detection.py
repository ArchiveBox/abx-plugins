#!/usr/bin/env python3
"""
Tests for ripgrep binary detection and archivebox install functionality.

Guards against regressions in:
1. Ripgrep hook not resolving binary names via shutil.which()
2. SEARCH_BACKEND_ENGINE not being passed to hook environment
"""

import os
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_ripgrep_hook_detects_binary_from_path():
    """Test that ripgrep hook finds binary using abx-pkg when env var is just a name."""
    hook_path = next(
        Path(__file__).parent.parent.glob("on_Install__50_ripgrep*.py"),
    )

    assert shutil.which("rg"), "ripgrep not installed"

    # Set SEARCH_BACKEND_ENGINE to enable the hook
    env = os.environ.copy()
    env["SEARCH_BACKEND_ENGINE"] = "ripgrep"
    env["RIPGREP_BINARY"] = "rg"  # Just the name, not the full path (this was the bug)

    result = subprocess.run(
        [str(hook_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0, f"Hook failed: {result.stderr}"

    # Parse JSONL output (filter out non-JSON lines)
    lines = [
        line
        for line in result.stdout.strip().split("\n")
        if line.strip() and line.strip().startswith("{")
    ]
    assert len(lines) >= 1, "Expected at least 1 JSONL line (BinaryRequest)"

    binary = json.loads(lines[0])
    assert binary["type"] == "BinaryRequest"
    assert binary["name"] == "rg"
    assert "binproviders" in binary, "Expected binproviders declaration"


def test_ripgrep_hook_skips_when_backend_not_ripgrep():
    """Test that ripgrep hook exits silently when search backend is not ripgrep."""
    hook_path = next(
        Path(__file__).parent.parent.glob("on_Install__50_ripgrep*.py"),
    )

    env = os.environ.copy()
    env["SEARCH_BACKEND_ENGINE"] = "sqlite"  # Different backend

    result = subprocess.run(
        [str(hook_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0, (
        "Hook should exit successfully when backend is not ripgrep"
    )
    assert result.stdout.strip() == "", (
        "Hook should produce no output when backend is not ripgrep"
    )


def test_ripgrep_hook_handles_absolute_path():
    """Test that ripgrep hook exits successfully when RIPGREP_BINARY is a valid absolute path."""
    hook_path = next(
        Path(__file__).parent.parent.glob("on_Install__50_ripgrep*.py"),
    )

    rg_path = shutil.which("rg")
    assert rg_path, "ripgrep not installed"

    env = os.environ.copy()
    env["SEARCH_BACKEND_ENGINE"] = "ripgrep"
    env["RIPGREP_BINARY"] = rg_path  # Full absolute path

    result = subprocess.run(
        [str(hook_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0, (
        f"Hook should exit successfully when binary already configured: {result.stderr}"
    )
    lines = [
        line
        for line in result.stdout.strip().split("\n")
        if line.strip().startswith("{")
    ]
    assert lines, "Expected BinaryRequest JSONL output when backend is ripgrep"


def test_ripgrep_only_detected_when_backend_enabled():
    """
    Test ripgrep validation hook behavior with different SEARCH_BACKEND_ENGINE settings.

    Guards against ripgrep being detected when not needed.
    """
    import subprocess
    from pathlib import Path

    assert shutil.which("rg"), "ripgrep not installed"

    hook_path = next(
        Path(__file__).parent.parent.glob("on_Install__50_ripgrep*.py"),
    )

    # Test 1: With ripgrep backend - should output BinaryRequest record
    env1 = os.environ.copy()
    env1["SEARCH_BACKEND_ENGINE"] = "ripgrep"
    env1["RIPGREP_BINARY"] = "rg"

    result1 = subprocess.run(
        [str(hook_path)],
        capture_output=True,
        text=True,
        env=env1,
        timeout=10,
    )

    assert result1.returncode == 0, (
        f"Hook should succeed with ripgrep backend: {result1.stderr}"
    )
    # Should output BinaryRequest JSONL when backend is ripgrep
    assert "BinaryRequest" in result1.stdout, (
        "Should output BinaryRequest when backend=ripgrep"
    )

    # Test 2: With different backend - should output nothing
    env2 = os.environ.copy()
    env2["SEARCH_BACKEND_ENGINE"] = "sqlite"
    env2["RIPGREP_BINARY"] = "rg"

    result2 = subprocess.run(
        [str(hook_path)],
        capture_output=True,
        text=True,
        env=env2,
        timeout=10,
    )

    assert result2.returncode == 0, (
        "Hook should exit successfully when backend is not ripgrep"
    )
    assert result2.stdout.strip() == "", (
        "Hook should produce no output when backend is not ripgrep"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
Tests for the hashes plugin.

Tests the real merkle tree generation with actual files.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent.parent))
from base.test_utils import parse_jsonl_output


# Get the path to the hashes hook
PLUGIN_DIR = Path(__file__).parent.parent
HASHES_HOOK = PLUGIN_DIR / "on_Snapshot__93_hashes.py"


class TestHashesPlugin:
    """Test the hashes plugin."""

    def test_hashes_hook_exists(self):
        """Hashes hook script should exist."""
        assert HASHES_HOOK.exists(), f"Hook not found: {HASHES_HOOK}"

    def test_hashes_generates_tree_for_files(self):
        """Hashes hook should generate merkle tree for files in snapshot directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a mock snapshot directory structure
            snap_dir = Path(temp_dir) / "snap"
            snap_dir.mkdir(parents=True, exist_ok=True)

            # Create output directory for hashes
            output_dir = snap_dir / "hashes"
            output_dir.mkdir()

            # Create some test files
            (snap_dir / "index.html").write_text("<html><body>Test</body></html>")
            (snap_dir / "screenshot.png").write_bytes(
                b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
            )

            subdir = snap_dir / "media"
            subdir.mkdir()
            (subdir / "video.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")

            # Run the hook from the output directory
            env = os.environ.copy()
            env["HASHES_ENABLED"] = "true"
            env["SNAP_DIR"] = str(snap_dir)

            result = subprocess.run(
                [str(HASHES_HOOK),
                    "--url=https://example.com",
                    "--snapshot-id=test-snapshot",
                ],
                capture_output=True,
                text=True,
                cwd=str(output_dir),  # Hook expects to run from output dir
                env=env,
                timeout=30,
            )

            # Should succeed
            assert result.returncode == 0, f"Hook failed: {result.stderr}"

            # Check output file exists
            output_file = output_dir / "hashes.json"
            assert output_file.exists(), "hashes.json not created"

            # Parse and verify output
            with open(output_file) as f:
                data = json.load(f)

            assert "root_hash" in data
            assert "files" in data
            assert "metadata" in data

            result_json = parse_jsonl_output(result.stdout)
            assert result_json["type"] == "ArchiveResult"
            assert result_json["status"] == "succeeded"

            # Should have indexed our test files
            file_paths = [f["path"] for f in data["files"]]
            assert "index.html" in file_paths
            assert "screenshot.png" in file_paths

            # Verify metadata
            assert data["metadata"]["file_count"] > 0
            assert data["metadata"]["total_size"] > 0
            total_size_mb = data["metadata"]["total_size"] / 1_000_000
            assert result_json["output_str"] == f'{total_size_mb:.1f}MB {data["root_hash"][:12]}'

    def test_hashes_skips_when_disabled(self):
        """Hashes hook should skip when HASHES_ENABLED=false."""
        with tempfile.TemporaryDirectory() as temp_dir:
            snap_dir = Path(temp_dir) / "snap"
            snap_dir.mkdir(parents=True, exist_ok=True)
            output_dir = snap_dir / "hashes"
            output_dir.mkdir()

            env = os.environ.copy()
            env["HASHES_ENABLED"] = "false"
            env["SNAP_DIR"] = str(snap_dir)

            result = subprocess.run(
                [str(HASHES_HOOK),
                    "--url=https://example.com",
                    "--snapshot-id=test-snapshot",
                ],
                capture_output=True,
                text=True,
                cwd=str(output_dir),
                env=env,
                timeout=30,
            )

            # Should succeed (exit 0) but skip
            assert result.returncode == 0
            assert "skipped" in result.stdout

    def test_hashes_handles_empty_directory(self):
        """Hashes hook should handle empty snapshot directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            snap_dir = Path(temp_dir) / "snap"
            snap_dir.mkdir(parents=True, exist_ok=True)
            output_dir = snap_dir / "hashes"
            output_dir.mkdir()

            env = os.environ.copy()
            env["HASHES_ENABLED"] = "true"
            env["SNAP_DIR"] = str(snap_dir)

            result = subprocess.run(
                [str(HASHES_HOOK),
                    "--url=https://example.com",
                    "--snapshot-id=test-snapshot",
                ],
                capture_output=True,
                text=True,
                cwd=str(output_dir),
                env=env,
                timeout=30,
            )

            # Should succeed even with empty directory
            assert result.returncode == 0, f"Hook failed: {result.stderr}"

            # Check output file exists
            output_file = output_dir / "hashes.json"
            assert output_file.exists()

            with open(output_file) as f:
                data = json.load(f)

            # Should have empty file list
            assert data["metadata"]["file_count"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
Tests for the hashes plugin.

Tests the real merkle tree generation with actual files.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output


# Get the path to the hashes hook
PLUGIN_DIR = Path(__file__).parent.parent
HASHES_HOOK = PLUGIN_DIR / "on_Snapshot__93_hashes.py"


def test_hashes_preserves_independent_outputs_on_real_wget_rerun(tmp_path, httpserver):
    """Deduplication cannot let a later extractor overwrite another saved output."""
    import shutil

    wget = shutil.which("wget")
    assert wget, "Real wget must be installed"
    wget_hook = PLUGIN_DIR.parent / "wget" / "on_Snapshot__35_wget.finite.bg.py"
    red = b"body { color: red; }\n"
    blue = b"body { color: blue; }\n"
    html = '<html><head><link rel="stylesheet" href="/style.css"></head><body>Archived article</body></html>'
    httpserver.expect_request("/").respond_with_data(html, content_type="text/html")
    httpserver.expect_request("/style.css").respond_with_data(
        red,
        content_type="text/css",
    )
    env = {
        **os.environ,
        "SNAP_DIR": str(tmp_path),
        "WGET_BINARY": wget,
        "HASHES_ENABLED": "true",
        "WGET_WARC_ENABLED": "false",
    }

    def run_hook(hook):
        result = subprocess.run(
            [str(hook), f"--url={httpserver.url_for('/')}"],
            env=env,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return result

    run_hook(wget_hook)
    downloaded = next((tmp_path / "wget").rglob("style.css"))
    retained = tmp_path / "responses" / "retained.css"
    retained.parent.mkdir()
    retained.write_bytes(downloaded.read_bytes())
    os.chmod(downloaded, 0o644)
    os.chmod(retained, 0o644)
    os.utime(downloaded, ns=(1600000000000000000, 1600000000000000000))
    os.utime(retained, ns=(1700000000000000000, 1700000000000000000))
    before = {path: path.stat().st_mtime_ns for path in (downloaded, retained)}
    run_hook(HASHES_HOOK)
    after = {path: path.stat().st_mtime_ns for path in before}
    httpserver.clear()
    httpserver.expect_request("/").respond_with_data(html, content_type="text/html")
    httpserver.expect_request("/style.css").respond_with_data(
        blue,
        content_type="text/css",
    )
    run_hook(wget_hook)
    assert downloaded.read_bytes() == blue
    assert retained.read_bytes() == red, (
        "Re-running Wget changed another extractor's retained bytes"
    )
    assert after == before, (
        "Deduplication changed independently preserved file timestamps"
    )
    assert downloaded.stat().st_ino != retained.stat().st_ino


class TestHashesPlugin:
    """Test the hashes plugin."""

    def test_hashes_hook_exists(self):
        """Hashes hook script should exist."""
        assert HASHES_HOOK.exists(), f"Hook not found: {HASHES_HOOK}"

    def test_hashes_generates_tree_for_files(
        self,
        tmp_path: Path,
        real_html_snapshot,
    ):
        """Hashes hook should index outputs produced by real capture hooks."""
        snap_dir = real_html_snapshot(
            tmp_path,
            "https://archivebox.io/tests/",
            "hashes",
        )
        output_dir = snap_dir / "hashes"
        output_dir.mkdir()
        env = os.environ.copy()
        env["HASHES_ENABLED"] = "true"
        env["SNAP_DIR"] = str(snap_dir)

        result = subprocess.run(
            [str(HASHES_HOOK), "--url=https://example.com"],
            capture_output=True,
            text=True,
            cwd=output_dir,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

        output_file = output_dir / "hashes.json"
        data = json.loads(output_file.read_text())
        result_json = parse_jsonl_output(result.stdout)
        assert result_json is not None and result_json["status"] == "succeeded"
        file_paths = [file["path"] for file in data["files"]]
        assert "dom/output.html" in file_paths
        assert "title/title.txt" in file_paths
        assert data["metadata"]["file_count"] >= 2
        assert data["metadata"]["total_size"] > 0
        total_size_mb = data["metadata"]["total_size"] / 1_000_000
        assert result_json["output_str"] == (
            f"{total_size_mb:.1f}MB {data['root_hash'][:12]}"
        )

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
                [
                    str(HASHES_HOOK),
                    "--url=https://example.com",
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

    def test_hashes_keeps_duplicate_outputs_independently_writable(
        self,
        tmp_path: Path,
    ):
        """Space sharing must retain distinct inodes and independent future writes."""
        snap_dir = tmp_path / "snap"
        output_dir = snap_dir / "hashes"
        first = snap_dir / "responses" / "image.png"
        duplicate = snap_dir / "wget" / "image.png"
        for path in (output_dir, first.parent, duplicate.parent):
            path.mkdir(parents=True)
        first.write_bytes(b"same archived image" * 1024)
        duplicate.write_bytes(first.read_bytes())

        result = subprocess.run(
            [str(HASHES_HOOK), "--url=https://example.com"],
            capture_output=True,
            text=True,
            cwd=output_dir,
            env={**os.environ, "HASHES_ENABLED": "true", "SNAP_DIR": str(snap_dir)},
            timeout=30,
        )

        assert result.returncode == 0, result.stderr
        assert first.read_bytes() == duplicate.read_bytes()
        assert first.stat().st_ino != duplicate.stat().st_ino
        phases = re.findall(
            r"phase=(hash|dedupe) complete elapsed_ms=(\d+\.\d+) files=(\d+) bytes=(\d+)",
            result.stderr,
        )
        assert [phase[0] for phase in phases] == ["hash", "dedupe"], result.stderr
        for _, elapsed, count, size in phases:
            assert float(elapsed) >= 0
            assert int(count) == 2
            assert int(size) == len(first.read_bytes()) * 2
        assert {
            row["path"]
            for row in json.loads((output_dir / "hashes.json").read_text())["files"]
        } >= {
            "responses/image.png",
            "wget/image.png",
        }
        duplicate.write_bytes(b"updated extractor output")
        assert first.read_bytes() == b"same archived image" * 1024

    def test_hashes_leaves_readonly_duplicates_intact(self, tmp_path: Path):
        """An optional space optimization cannot damage read-only saved files."""
        snap_dir = tmp_path / "snap"
        output_dir = snap_dir / "hashes"
        first = snap_dir / "responses" / "image.png"
        duplicate = snap_dir / "readonly" / "image.png"
        for path in (output_dir, first.parent, duplicate.parent):
            path.mkdir(parents=True)
        first.write_bytes(b"same archived image")
        duplicate.write_bytes(first.read_bytes())
        duplicate.chmod(0o444)
        result = subprocess.run(
            [str(HASHES_HOOK), "--url=https://example.com"],
            capture_output=True,
            text=True,
            cwd=output_dir,
            env={**os.environ, "HASHES_ENABLED": "true", "SNAP_DIR": str(snap_dir)},
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert first.read_bytes() == duplicate.read_bytes() == b"same archived image"
        assert first.stat().st_ino != duplicate.stat().st_ino
        assert duplicate.stat().st_mode & 0o777 == 0o444

    def test_hashes_merges_extra_context_into_archive_result(self):
        """Hashes hook should merge orchestrator-provided extra context into JSONL output."""
        with tempfile.TemporaryDirectory() as temp_dir:
            snap_dir = Path(temp_dir) / "snap"
            snap_dir.mkdir(parents=True, exist_ok=True)
            output_dir = snap_dir / "hashes"
            output_dir.mkdir()
            (snap_dir / "index.html").write_text("<html><body>Test</body></html>")

            env = os.environ.copy()
            env["HASHES_ENABLED"] = "true"
            env["SNAP_DIR"] = str(snap_dir)
            env["EXTRA_CONTEXT"] = json.dumps(
                {
                    "snapshot_id": "snap-123",
                    "machine_id": "machine-123",
                },
            )

            result = subprocess.run(
                [
                    str(HASHES_HOOK),
                    "--url=https://example.com",
                ],
                capture_output=True,
                text=True,
                cwd=str(output_dir),
                env=env,
                timeout=30,
            )

            assert result.returncode == 0, f"Hook failed: {result.stderr}"
            result_json = parse_jsonl_output(result.stdout)
            assert result_json is not None
            assert result_json["type"] == "ArchiveResult"
            assert result_json["snapshot_id"] == "snap-123"
            assert result_json["machine_id"] == "machine-123"

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
                [
                    str(HASHES_HOOK),
                    "--url=https://example.com",
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


def test_hook_receives_cli_args(tmp_path):
    """Hook should receive CLI arguments."""
    snap_dir = tmp_path / "snapshot"
    output_dir = snap_dir / "hashes"
    output_dir.mkdir(parents=True)
    (snap_dir / "source.txt").write_text("real CLI argument input", encoding="utf-8")
    hook_path = HASHES_HOOK

    result = subprocess.run(
        [str(hook_path), "--url=https://example.com/real-hook-argument"],
        cwd=output_dir,
        capture_output=True,
        text=True,
        env={**os.environ, "SNAP_DIR": str(snap_dir)},
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    source_hash = hashlib.sha256(b"real CLI argument input").hexdigest()
    assert records == [
        {
            "type": "ArchiveResult",
            "status": "succeeded",
            "output_str": f"0.0MB {source_hash[:12]}",
        },
    ]
    hashes = json.loads((output_dir / "hashes.json").read_text())
    assert hashes["files"][0]["hash"] == source_hash


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_hashes_include_chrome_evidence_but_not_profiles_or_session_markers(tmp_path):
    retained = {
        "chrome/random_seed.json": '{"random_seed": 12345}',
        "chrome/cmd.sh": "chrome --js-flags=--random-seed=12345",
        "chrome/extensions/extension.json": '{"version": "1.0"}',
    }
    excluded = {
        "chrome/browser.json": '{"ready": true}',
        "chrome/chrome.pid": "123",
        "chrome/cdp_url.txt": "ws://localhost:9222/devtools/browser/session",
        "chrome/navigation.json": '{"url": "https://example.com"}',
        "chrome/chrome_profile/Default/Preferences": "{}",
        "chrome/archivebox-extensions/example/manifest.json": "{}",
        ".persona/Default/chrome_profile/Local State": "{}",
    }
    for name, content in (retained | excluded).items():
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    result = subprocess.run(
        [str(HASHES_HOOK), "--url=https://example.com"],
        cwd=tmp_path,
        env=os.environ | {"SNAP_DIR": str(tmp_path), "HASHES_ENABLED": "true"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((tmp_path / "hashes/hashes.json").read_text())
    assert {row["path"]: row["hash"] for row in manifest["files"]} == {
        name: hashlib.sha256(content.encode()).hexdigest()
        for name, content in retained.items()
    }

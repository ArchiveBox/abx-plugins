"""
Tests for the claudecodecleanup plugin.

Tests verify:
1. Hook script exists
2. Config schema is valid and declares claudecode dependency
3. Hook runs at priority 92 (before hashes at 93)
4. Hook skips when disabled
5. Hook fails gracefully when API key is missing
6. Full cleanup pipeline runs against real snapshot with duplicates (integration, requires Claude Code auth)
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import (
    get_plugin_dir,
    get_hook_script,
    parse_jsonl_output,
    run_hook,
)
from abx_plugins.plugins.claudecodecleanup.cleanup_utils import (
    apply_cleanup_deletions,
    build_cleanup_inventory,
    build_cleanup_inventory_with_capabilities,
    ensure_owned_output_dir,
    write_owned_output_file,
)


PLUGIN_DIR = get_plugin_dir(__file__)
_CLEANUP_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_claudecodecleanup*")
if _CLEANUP_HOOK is None:
    raise FileNotFoundError(f"Cleanup hook not found in {PLUGIN_DIR}")
CLEANUP_HOOK = _CLEANUP_HOOK
TEST_URL = "https://example.com"


def write_snapshot_ledger(
    snap_dir: Path,
    snapshot_id: str,
    url: str = TEST_URL,
) -> None:
    (snap_dir / "index.jsonl").write_text(
        json.dumps({"type": "Snapshot", "id": snapshot_id, "url": url}) + "\n",
    )


def append_snapshot_ledger_record(snap_dir: Path, record: dict[str, object]) -> None:
    with (snap_dir / "index.jsonl").open("a") as ledger:
        ledger.write(json.dumps(record) + "\n")


def hook_extra_context(snapshot_id: str, plugin_name: str) -> list[str]:
    """Pass the same ArchiveResult identity fields supplied by the real bus."""
    return [
        "--extra-context="
        + json.dumps({"snapshot_id": snapshot_id, "plugin": plugin_name}),
    ]


def create_snapshot_with_real_outputs(
    root: Path,
    snapshot_id: str,
    real_html_snapshot,
) -> Path:
    """Run real extractors, one real failed extractor, and hashes."""
    snap_dir = real_html_snapshot(root, TEST_URL, snapshot_id)
    write_snapshot_ledger(snap_dir, snapshot_id)
    for plugin_name in ("title", "dom"):
        record = parse_jsonl_output((snap_dir / plugin_name / "stdout.log").read_text())
        assert record is not None
        assert record["snapshot_id"] == snapshot_id
        assert record["plugin"] == plugin_name
        append_snapshot_ledger_record(snap_dir, record)
    env = os.environ.copy()
    env["SNAP_DIR"] = str(snap_dir)

    for plugin_name in ("readability", "htmltotext", "mercury"):
        plugin_dir = PLUGIN_DIR.parent / plugin_name
        hook = get_hook_script(plugin_dir, f"on_Snapshot__*_{plugin_name}.*")
        assert hook is not None
        output_dir = snap_dir / plugin_name
        output_dir.mkdir()
        returncode, stdout, stderr = run_hook(
            hook,
            TEST_URL,
            snapshot_id,
            cwd=output_dir,
            env=env,
            timeout=120,
            extra_args=hook_extra_context(snapshot_id, plugin_name),
        )
        record = parse_jsonl_output(stdout)
        assert returncode == 0, stderr
        assert record is not None and record["status"] == "succeeded", record
        assert record["snapshot_id"] == snapshot_id
        assert record["plugin"] == plugin_name
        append_snapshot_ledger_record(snap_dir, record)
        (output_dir / "stdout.log").write_text(stdout)

    failed_dir = snap_dir / "screenshot"
    failed_dir.mkdir()
    screenshot_hook = get_hook_script(
        PLUGIN_DIR.parent / "screenshot",
        "on_Snapshot__*_screenshot.*",
    )
    assert screenshot_hook is not None
    returncode, stdout, stderr = run_hook(
        screenshot_hook,
        TEST_URL,
        snapshot_id,
        cwd=failed_dir,
        env=env,
        timeout=30,
        extra_args=hook_extra_context(snapshot_id, "screenshot"),
    )
    assert returncode != 0, (stdout, stderr)
    failed_record = parse_jsonl_output(stdout)
    assert failed_record is not None and failed_record["status"] == "failed"
    assert failed_record["snapshot_id"] == snapshot_id
    assert failed_record["plugin"] == "screenshot"
    append_snapshot_ledger_record(snap_dir, failed_record)
    (failed_dir / "stdout.log").write_text(stdout)
    (failed_dir / "stderr.log").write_text(stderr)

    hashes_dir = snap_dir / "hashes"
    hashes_dir.mkdir()
    hashes_hook = get_hook_script(
        PLUGIN_DIR.parent / "hashes",
        "on_Snapshot__*_hashes.*",
    )
    assert hashes_hook is not None
    returncode, stdout, stderr = run_hook(
        hashes_hook,
        TEST_URL,
        snapshot_id,
        cwd=hashes_dir,
        env=env,
        timeout=30,
        extra_args=hook_extra_context(snapshot_id, "hashes"),
    )
    record = parse_jsonl_output(stdout)
    assert returncode == 0, stderr
    assert record is not None and record["status"] == "succeeded", record
    return snap_dir


class TestClaudeCodeCleanupPlugin:
    """Test the claudecodecleanup plugin."""

    def test_hook_exists(self):
        """Hook script should exist."""
        assert CLEANUP_HOOK.exists(), f"Hook not found: {CLEANUP_HOOK}"

    def test_hook_runs_at_priority_92(self):
        """Hook should be at priority 92 (after extractors, before hashes at 93)."""
        assert "__92_" in CLEANUP_HOOK.name, (
            f"Expected priority 92 in hook name: {CLEANUP_HOOK.name}"
        )

    def test_config_json_exists_and_valid(self):
        """config.json should exist and declare claudecode dependency."""
        config_path = PLUGIN_DIR / "config.json"
        assert config_path.exists(), "config.json not found"

        config = json.loads(config_path.read_text())
        assert config.get("$schema") == "http://json-schema.org/draft-07/schema#"
        assert "claudecode" in config.get("required_plugins", [])
        assert "CLAUDECODECLEANUP_ENABLED" in config["properties"]
        assert "CLAUDECODECLEANUP_PROMPT" in config["properties"]
        assert "CLAUDECODECLEANUP_MODEL" in config["properties"]
        assert "CLAUDECODECLEANUP_TIMEOUT" in config["properties"]
        assert "CLAUDECODECLEANUP_MAX_TURNS" in config["properties"]

    def test_config_has_default_prompt(self):
        """Config should have a sensible default prompt about cleanup."""
        config_path = PLUGIN_DIR / "config.json"
        config = json.loads(config_path.read_text())
        default_prompt = config["properties"]["CLAUDECODECLEANUP_PROMPT"]["default"]
        assert len(default_prompt) > 50, "Default prompt should be meaningful"
        assert (
            "duplicate" in default_prompt.lower()
            or "redundant" in default_prompt.lower()
        )

    def test_config_has_higher_max_turns_than_extract(self):
        """Cleanup should have higher default max turns than extract."""
        cleanup_config = json.loads((PLUGIN_DIR / "config.json").read_text())
        extract_config_path = PLUGIN_DIR.parent / "claudecodeextract" / "config.json"
        extract_config = json.loads(extract_config_path.read_text())

        cleanup_max = cleanup_config["properties"]["CLAUDECODECLEANUP_MAX_TURNS"][
            "default"
        ]
        extract_max = extract_config["properties"]["CLAUDECODEEXTRACT_MAX_TURNS"][
            "default"
        ]
        assert cleanup_max >= extract_max, (
            f"Cleanup max_turns ({cleanup_max}) should be >= extract max_turns ({extract_max})"
        )

    def test_templates_exist(self):
        """Template files should exist."""
        templates_dir = PLUGIN_DIR / "templates"
        assert (templates_dir / "icon.html").exists()
        assert (templates_dir / "card.html").exists()
        assert (templates_dir / "full.html").exists()

    def test_cleanup_inventory_is_complete_bounded_and_excludes_owned_files(
        self,
        tmp_path,
    ):
        """Inventory should inspect real files once without exposing owned files."""
        snap_dir = tmp_path / "snap"
        output_dir = snap_dir / "claudecodecleanup"
        (snap_dir / "readability").mkdir(parents=True)
        (snap_dir / "mercury").mkdir()
        (snap_dir / "pdf").mkdir()
        (snap_dir / "empty-extractor").mkdir()
        output_dir.mkdir()

        duplicate = b"same extracted text"
        (snap_dir / "readability" / "content.txt").write_bytes(duplicate)
        (snap_dir / "mercury" / "content.txt").write_bytes(duplicate)
        (snap_dir / "readability" / "metadata.json").write_text('{"ok": true}')
        (snap_dir / "pdf" / "output.pdf").write_bytes(b"%PDF-1.7 unique output")
        (snap_dir / "screenshot").mkdir()
        (snap_dir / "screenshot" / "screenshot.png").write_text(
            "<!doctype html><title>503 Service Unavailable</title>",
        )
        (snap_dir / "readability" / "hook.stderr.log").write_text("private log")
        (output_dir / "session.json").write_text('{"owned": true}')

        inventory, capabilities = build_cleanup_inventory_with_capabilities(
            snap_dir,
            output_dir,
            max_bytes=4096,
        )

        assert len(inventory.encode("utf-8")) <= 4096
        assert '"files_inspected": 5' in inventory
        assert (
            '"paths": ["mercury/content.txt", "readability/content.txt"]' in inventory
        )
        assert "metadata.json" in inventory
        assert "pdf/output.pdf" in inventory
        assert '"content_kind": "application/pdf"' in inventory
        assert "screenshot/screenshot.png" in inventory
        assert '"content_kind": "text/html"' in inventory
        assert "503 Service Unavailable" in inventory
        assert '"empty-extractor"' in inventory
        assert "hook.stderr.log" not in inventory
        assert "claudecodecleanup/session.json" not in inventory
        assert all(
            f'"id": "{capability_id}"' in inventory for capability_id in capabilities
        )

    def test_cleanup_inventory_records_directory_symlinks(self, tmp_path):
        """Directory symlinks are evidence, but their targets are never traversed."""
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("outside snapshot")
        (snap_dir / "linked-extractor").symlink_to(
            outside_dir,
            target_is_directory=True,
        )

        inventory = build_cleanup_inventory(
            snap_dir,
            snap_dir / "claudecodecleanup",
        )

        assert '"path": "linked-extractor"' in inventory
        assert '"content_kind": "inode/symlink"' in inventory
        assert "secret.txt" not in inventory

    def test_cleanup_inventory_processes_bounded_batch_at_traversal_limit(
        self,
        tmp_path,
    ):
        """Reaching a traversal cap must retain the bounded evidence already read."""
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        for number in range(5):
            (snap_dir / f"output-{number}.txt").write_text(f"output {number}")

        inventory = build_cleanup_inventory(
            snap_dir,
            snap_dir / "claudecodecleanup",
            max_files=3,
            max_directories=2,
            max_filesystem_entries=4,
        )

        assert '"files_inspected": 3' in inventory
        assert '"traversal_limit_reached": true' in inventory
        assert '"inventory_truncated": true' in inventory

    def test_cleanup_inventory_rejects_unbounded_sample_size(self, tmp_path):
        """Invalid sample sizes cannot turn a bounded prefix read into read-all."""
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()

        with pytest.raises(ValueError, match="sample_bytes must be positive"):
            build_cleanup_inventory(
                snap_dir,
                snap_dir / "claudecodecleanup",
                sample_bytes=-1,
            )

    def test_cleanup_output_is_owned_and_rejects_symlinks(self, tmp_path):
        """Hook-owned files must stay inside a real snapshot child directory."""
        snap_dir = tmp_path / "snap"
        snap_dir.mkdir()
        output_dir = snap_dir / "claudecodecleanup"
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        output_dir.symlink_to(outside_dir, target_is_directory=True)

        with pytest.raises(ValueError, match="must not be a symlink"):
            ensure_owned_output_dir(snap_dir, output_dir)

        output_dir.unlink()
        report_path = write_owned_output_file(
            snap_dir,
            output_dir,
            "cleanup_report.txt",
            "kept all unique outputs",
        )
        assert report_path.read_text() == "kept all unique outputs"
        assert not (outside_dir / "cleanup_report.txt").exists()

    def test_cleanup_inventory_excludes_output_through_snapshot_alias(self, tmp_path):
        """Canonical path comparison must exclude owned output through an alias."""
        snap_dir = tmp_path / "real-snap"
        snap_dir.mkdir()
        snap_alias = tmp_path / "snap-alias"
        snap_alias.symlink_to(snap_dir, target_is_directory=True)
        output_dir = snap_alias / "claudecodecleanup"
        output_dir.mkdir()
        (output_dir / "session.json").write_text('{"owned": true}')
        (snap_dir / "title").mkdir()
        (snap_dir / "title" / "title.txt").write_text("Example Domain")

        inventory = build_cleanup_inventory(snap_alias, output_dir)

        assert '"files_inspected": 1' in inventory
        assert "title/title.txt" in inventory
        assert "claudecodecleanup/session.json" not in inventory

    def test_cleanup_deletion_plan_is_bound_to_snapshot_identity(self, tmp_path):
        snap_dir = tmp_path / "expected-snapshot"
        target = snap_dir / "screenshot" / "output.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"failed screenshot")
        write_snapshot_ledger(snap_dir, "expected-snapshot")
        append_snapshot_ledger_record(
            snap_dir,
            {
                "type": "ArchiveResult",
                "snapshot_id": "expected-snapshot",
                "plugin": "screenshot",
            },
        )
        _inventory, capabilities = build_cleanup_inventory_with_capabilities(
            snap_dir,
            snap_dir / "claudecodecleanup",
        )

        with pytest.raises(ValueError, match="is absent from"):
            apply_cleanup_deletions(
                snap_dir,
                snap_dir / "claudecodecleanup",
                "different-snapshot",
                TEST_URL,
                capabilities,
                ["file-00001"],
            )

        assert target.read_bytes() == b"failed screenshot"

    def test_cleanup_deletion_plan_applies_only_safe_relative_paths(self, tmp_path):
        snap_dir = tmp_path / "test-output"
        screenshot_dir = snap_dir / "screenshot"
        screenshot_dir.mkdir(parents=True)
        (screenshot_dir / "output.png").write_bytes(b"failed screenshot")
        (screenshot_dir / "metadata.json").write_text('{"status": "failed"}')
        outside = tmp_path / "outside.txt"
        outside.write_text("must remain")
        (screenshot_dir / "outside-link").symlink_to(outside)
        (snap_dir / ".git" / "objects").mkdir(parents=True)
        (snap_dir / ".git" / "objects" / "canary").write_text("repository data")
        (snap_dir / "abx_dl").mkdir()
        (snap_dir / "abx_dl" / "source.py").write_text("source code")
        write_snapshot_ledger(snap_dir, "snapshot-id")
        append_snapshot_ledger_record(
            snap_dir,
            {
                "type": "ArchiveResult",
                "snapshot_id": "snapshot-id",
                "plugin": "screenshot",
            },
        )
        inventory, capabilities = build_cleanup_inventory_with_capabilities(
            snap_dir,
            snap_dir / "claudecodecleanup",
            allowed_directories={"screenshot"},
        )
        selected_ids = [
            capability_id
            for capability_id, capability in capabilities.items()
            if capability["path"]
            in {"screenshot/output.png", "screenshot/outside-link"}
        ]
        assert len(selected_ids) == 2
        assert ".git" not in inventory
        assert "abx_dl/source.py" not in inventory
        assert all(
            capability["path"] != "screenshot" for capability in capabilities.values()
        )

        deleted = apply_cleanup_deletions(
            snap_dir,
            snap_dir / "claudecodecleanup",
            "snapshot-id",
            TEST_URL,
            capabilities,
            selected_ids,
        )

        assert "screenshot/output.png" in deleted
        assert "screenshot/outside-link" in deleted
        assert not (screenshot_dir / "output.png").exists()
        assert (screenshot_dir / "metadata.json").is_file()
        assert outside.read_text() == "must remain"
        assert (
            snap_dir / ".git" / "objects" / "canary"
        ).read_text() == "repository data"
        assert (snap_dir / "abx_dl" / "source.py").read_text() == "source code"

        with pytest.raises(ValueError, match="Unknown cleanup capability ids"):
            apply_cleanup_deletions(
                snap_dir,
                snap_dir / "claudecodecleanup",
                "snapshot-id",
                TEST_URL,
                capabilities,
                ["not-an-inventory-id"],
            )

        assert outside.read_text() == "must remain"

    def test_cleanup_deletion_plan_revalidates_every_inode_before_unlink(
        self,
        tmp_path,
    ):
        snap_dir = tmp_path / "test-output"
        extractor_dir = snap_dir / "readability"
        extractor_dir.mkdir(parents=True)
        first = extractor_dir / "first.txt"
        changed = extractor_dir / "changed.txt"
        first.write_text("duplicate")
        changed.write_text("duplicate")
        write_snapshot_ledger(snap_dir, "snapshot-id")
        append_snapshot_ledger_record(
            snap_dir,
            {
                "type": "ArchiveResult",
                "snapshot_id": "snapshot-id",
                "plugin": "readability",
            },
        )
        _inventory, capabilities = build_cleanup_inventory_with_capabilities(
            snap_dir,
            snap_dir / "claudecodecleanup",
        )
        selected_ids = [
            capability_id
            for capability_id, capability in capabilities.items()
            if capability["path"]
            in {"readability/first.txt", "readability/changed.txt"}
        ]
        changed.unlink()
        changed.write_text("replacement")

        with pytest.raises(ValueError, match="changed since inventory"):
            apply_cleanup_deletions(
                snap_dir,
                snap_dir / "claudecodecleanup",
                "snapshot-id",
                TEST_URL,
                capabilities,
                selected_ids,
            )

        assert first.read_text() == "duplicate"
        assert changed.read_text() == "replacement"

    def test_hook_skips_when_disabled(self):
        """Hook should skip when CLAUDECODECLEANUP_ENABLED=false."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODECLEANUP_ENABLED"] = "false"

            returncode, stdout, stderr = run_hook(
                CLEANUP_HOOK,
                TEST_URL,
                "test-snapshot",
                cwd=output_dir,
                env=env,
                timeout=30,
            )

            assert returncode == 0, f"Hook failed: {stderr}"
            result = parse_jsonl_output(stdout)
            assert result is not None, f"Expected JSONL output, got: {stdout}"
            assert result["status"] == "skipped"

    def test_hook_reads_snapshot_id_from_extra_context_when_cli_flag_missing(self):
        """Hook should not require --snapshot-id when EXTRA_CONTEXT provides it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODECLEANUP_ENABLED"] = "false"
            env["EXTRA_CONTEXT"] = json.dumps({"snapshot_id": "ctx-snapshot"})

            returncode, stdout, stderr = run_hook(
                CLEANUP_HOOK,
                TEST_URL,
                None,
                cwd=output_dir,
                env=env,
                timeout=30,
            )

            assert returncode == 0, f"Hook failed: {stderr}"
            assert "Missing option '--snapshot-id'" not in stderr
            result = parse_jsonl_output(stdout)
            assert result is not None, f"Expected JSONL output, got: {stdout}"
            assert result["status"] == "skipped"

    def test_hook_fails_without_api_key(self):
        """Hook should fail when no Claude Code credential is set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODECLEANUP_ENABLED"] = "true"
            env.pop("ANTHROPIC_API_KEY", None)
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

            returncode, stdout, stderr = run_hook(
                CLEANUP_HOOK,
                TEST_URL,
                "test-snapshot",
                cwd=output_dir,
                env=env,
                timeout=30,
            )

            assert returncode == 1
            result = parse_jsonl_output(stdout)
            assert result is not None, f"Expected JSONL output, got: {stdout}"
            assert result["status"] == "failed"
            assert "auth" in result["output_str"]


@pytest.mark.usefixtures("ensure_claude_code_prereqs")
class TestClaudeCodeCleanupIntegration:
    """Integration tests that run the full cleanup pipeline with real Claude Code.

    These tests require claude binary in PATH and ANTHROPIC_API_KEY or
    CLAUDE_CODE_OAUTH_TOKEN set.
    """

    def test_cleanup_produces_report(self, real_html_snapshot):
        """Cleanup hook should analyze snapshot and produce a cleanup report."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = create_snapshot_with_real_outputs(
                Path(tmpdir),
                "test-cleanup-integration",
                real_html_snapshot,
            )

            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CRAWL_DIR"] = str(Path(tmpdir) / "crawl")
            env["CLAUDECODECLEANUP_ENABLED"] = "true"
            env["CLAUDECODECLEANUP_MODEL"] = "haiku"
            env["CLAUDECODECLEANUP_MAX_TURNS"] = "25"
            env["CLAUDECODECLEANUP_TIMEOUT"] = "180"

            returncode, stdout, stderr = run_hook(
                CLEANUP_HOOK,
                TEST_URL,
                "test-cleanup-integration",
                cwd=output_dir,
                env=env,
                timeout=180,
            )

            result = parse_jsonl_output(stdout)
            assert result is not None, f"No ArchiveResult. stderr: {stderr[:500]}"
            assert result["status"] == "succeeded", (
                f"Cleanup should succeed. status={result['status']}, "
                f"output={result.get('output_str', '')}, stderr: {stderr[:500]}"
            )

            # Should produce cleanup_report.txt
            report_file = output_dir / "cleanup_report.txt"
            assert report_file.exists(), (
                f"Should create cleanup_report.txt. Dir: {list(output_dir.iterdir())}"
            )
            report_text = report_file.read_text()
            assert len(report_text) > 20, "Cleanup report should contain analysis"
            assert report_text == (output_dir / "response.txt").read_text(), (
                "The hook must persist Claude's final cleanup report exactly"
            )

            # hashes/ directory should NOT be deleted
            assert (snap_dir / "hashes").exists(), "hashes/ should be preserved"
            assert (snap_dir / "hashes" / "hashes.json").exists(), (
                "hashes.json should be preserved"
            )

    def test_cleanup_preserves_hashes(self, real_html_snapshot):
        """Cleanup should delete redundant outputs but never delete hashes/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = create_snapshot_with_real_outputs(
                Path(tmpdir),
                "test-preserve-hashes",
                real_html_snapshot,
            )

            output_dir = snap_dir / "claudecodecleanup"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CRAWL_DIR"] = str(Path(tmpdir) / "crawl")
            env["CLAUDECODECLEANUP_ENABLED"] = "true"
            env["CLAUDECODECLEANUP_MODEL"] = "haiku"
            env["CLAUDECODECLEANUP_MAX_TURNS"] = "25"
            env["CLAUDECODECLEANUP_TIMEOUT"] = "90"
            env["CLAUDECODECLEANUP_PROMPT"] = (
                "Select every deletable file id under htmltotext/ because that real extractor output is redundant. "
                "Do NOT select ids from any other extractor directory. "
                "Return a summary of what you deleted in your final response."
            )

            returncode, stdout, stderr = run_hook(
                CLEANUP_HOOK,
                TEST_URL,
                "test-preserve-hashes",
                cwd=output_dir,
                env=env,
                timeout=180,
            )

            result = parse_jsonl_output(stdout)
            assert result is not None, f"No ArchiveResult. stderr: {stderr[:500]}"
            assert result["status"] == "succeeded", f"Should succeed: {stderr[:500]}"

            assert not (snap_dir / "htmltotext" / "htmltotext.txt").exists(), (
                "the selected real extractor output should have been deleted"
            )
            assert (snap_dir / "screenshot" / "stdout.log").exists(), (
                "failed extractor process logs must remain protected"
            )

            # Verify hashes preserved (must survive even when deletion is enabled)
            assert (snap_dir / "hashes").exists(), "hashes/ must be preserved"
            assert (snap_dir / "hashes" / "hashes.json").exists(), (
                "hashes.json must be preserved"
            )

            # Verify cleanup report was written
            report_file = output_dir / "cleanup_report.txt"
            assert report_file.exists(), (
                f"Should create cleanup_report.txt. Dir: {list(output_dir.iterdir())}"
            )
            report_text = report_file.read_text()
            assert len(report_text) > 20, "Report should contain analysis"
            assert report_text == (output_dir / "response.txt").read_text(), (
                "The hook must persist Claude's final cleanup report exactly"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

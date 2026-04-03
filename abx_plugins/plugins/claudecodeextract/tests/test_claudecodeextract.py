"""
Tests for the claudecodeextract plugin.

Tests verify:
1. Hook script exists
2. Config schema is valid and declares claudecode dependency
3. Hook skips when disabled
4. Hook fails gracefully when API key is missing
5. Hook fails gracefully when claude binary is not found
6. Full extraction pipeline runs against real snapshot data (integration, requires ANTHROPIC_API_KEY)
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import (
    get_plugin_dir,
    get_hook_script,
    parse_jsonl_output,
    run_hook,
)


PLUGIN_DIR = get_plugin_dir(__file__)
_EXTRACT_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_claudecodeextract*")
if _EXTRACT_HOOK is None:
    raise FileNotFoundError(f"Extract hook not found in {PLUGIN_DIR}")
EXTRACT_HOOK = _EXTRACT_HOOK
TEST_URL = "https://example.com"


def create_fake_snapshot(snap_dir: Path) -> None:
    """Create a realistic snapshot directory with multiple extractor outputs."""
    # readability output
    readability_dir = snap_dir / "readability"
    readability_dir.mkdir(parents=True)
    (readability_dir / "content.html").write_text("""
<div>
    <h1>Example Domain</h1>
    <p>This domain is for use in illustrative examples in documents.</p>
    <p>You may use this domain in literature without prior coordination.</p>
</div>
    """)
    (readability_dir / "content.txt").write_text(
        "Example Domain\n\n"
        "This domain is for use in illustrative examples in documents.\n"
        "You may use this domain in literature without prior coordination.\n",
    )
    (readability_dir / "article.json").write_text(
        json.dumps(
            {
                "title": "Example Domain",
                "byline": None,
                "siteName": "example.com",
            },
        ),
    )

    # htmltotext output
    htmltotext_dir = snap_dir / "htmltotext"
    htmltotext_dir.mkdir()
    (htmltotext_dir / "content.txt").write_text(
        "Example Domain\n"
        "This domain is for use in illustrative examples in documents.\n",
    )

    # dom output
    dom_dir = snap_dir / "dom"
    dom_dir.mkdir()
    (dom_dir / "output.html").write_text("""
<!DOCTYPE html>
<html><head><title>Example Domain</title></head>
<body><h1>Example Domain</h1>
<p>This domain is for use in illustrative examples in documents.</p>
</body></html>
    """)


class TestClaudeCodeExtractPlugin:
    """Test the claudecodeextract plugin."""

    def test_hook_exists(self):
        """Hook script should exist."""
        assert EXTRACT_HOOK.exists(), f"Hook not found: {EXTRACT_HOOK}"

    def test_config_json_exists_and_valid(self):
        """config.json should exist and declare claudecode dependency."""
        config_path = PLUGIN_DIR / "config.json"
        assert config_path.exists(), "config.json not found"

        config = json.loads(config_path.read_text())
        assert config.get("$schema") == "http://json-schema.org/draft-07/schema#"
        assert "claudecode" in config.get("required_plugins", [])
        assert "CLAUDECODEEXTRACT_ENABLED" in config["properties"]
        assert "CLAUDECODEEXTRACT_PROMPT" in config["properties"]
        assert "CLAUDECODEEXTRACT_MODEL" in config["properties"]
        assert "CLAUDECODEEXTRACT_TIMEOUT" in config["properties"]
        assert "CLAUDECODEEXTRACT_MAX_TURNS" in config["properties"]

    def test_config_has_default_prompt(self):
        """Config should have a sensible default prompt."""
        config_path = PLUGIN_DIR / "config.json"
        config = json.loads(config_path.read_text())
        default_prompt = config["properties"]["CLAUDECODEEXTRACT_PROMPT"]["default"]
        assert len(default_prompt) > 50, "Default prompt should be meaningful"
        assert "markdown" in default_prompt.lower() or "Markdown" in default_prompt

    def test_templates_exist(self):
        """Template files should exist."""
        templates_dir = PLUGIN_DIR / "templates"
        assert (templates_dir / "icon.html").exists()
        assert (templates_dir / "card.html").exists()
        assert (templates_dir / "full.html").exists()

    def test_hook_skips_when_disabled(self):
        """Hook should skip when CLAUDECODEEXTRACT_ENABLED=false."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            output_dir = snap_dir / "claudecodeextract"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODEEXTRACT_ENABLED"] = "false"

            returncode, stdout, stderr = run_hook(
                EXTRACT_HOOK,
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
            output_dir = snap_dir / "claudecodeextract"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODEEXTRACT_ENABLED"] = "false"
            env["EXTRA_CONTEXT"] = json.dumps({"snapshot_id": "ctx-snapshot"})

            returncode, stdout, stderr = run_hook(
                EXTRACT_HOOK,
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
        """Hook should fail when ANTHROPIC_API_KEY is not set."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            output_dir = snap_dir / "claudecodeextract"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODEEXTRACT_ENABLED"] = "true"
            env.pop("ANTHROPIC_API_KEY", None)

            returncode, stdout, stderr = run_hook(
                EXTRACT_HOOK,
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
            assert "ANTHROPIC_API_KEY" in result["output_str"]

    def test_hook_fails_gracefully_with_missing_binary(self):
        """Hook should fail gracefully when claude binary is not found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            output_dir = snap_dir / "claudecodeextract"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CLAUDECODEEXTRACT_ENABLED"] = "true"
            env["ANTHROPIC_API_KEY"] = "sk-ant-test-key"
            env["CLAUDECODE_BINARY"] = "/nonexistent/claude"

            returncode, stdout, stderr = run_hook(
                EXTRACT_HOOK,
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
            assert "not found" in result["output_str"].lower(), (
                f"Error should mention missing binary: {result['output_str']}"
            )


@pytest.mark.usefixtures("ensure_claude_code_prereqs")
class TestClaudeCodeExtractIntegration:
    """Integration tests that run the full extract pipeline with real Claude Code.

    These tests require claude binary in PATH and ANTHROPIC_API_KEY set.
    """

    def test_extract_generates_markdown_from_snapshot(self):
        """Full extract hook should read snapshot outputs and generate markdown."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            create_fake_snapshot(snap_dir)

            output_dir = snap_dir / "claudecodeextract"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CRAWL_DIR"] = str(Path(tmpdir) / "crawl")
            env["CLAUDECODEEXTRACT_ENABLED"] = "true"
            env["CLAUDECODEEXTRACT_MODEL"] = "haiku"
            env["CLAUDECODEEXTRACT_TIMEOUT"] = "90"

            returncode, stdout, stderr = run_hook(
                EXTRACT_HOOK,
                TEST_URL,
                "test-extract-integration",
                cwd=output_dir,
                env=env,
                timeout=120,
            )

            result = parse_jsonl_output(stdout)
            assert result is not None, (
                f"No ArchiveResult in output. stderr: {stderr[:500]}"
            )
            assert result["status"] == "succeeded", (
                f"Extract should succeed. status={result['status']}, "
                f"output={result.get('output_str', '')}, stderr: {stderr[:500]}"
            )

            # Default prompt should generate content.md with markdown from snapshot
            content_md = output_dir / "content.md"
            assert content_md.exists(), (
                f"Default prompt should create content.md. "
                f"Dir contents: {list(output_dir.iterdir())}"
            )
            md_text = content_md.read_text()
            assert len(md_text) > 20, "content.md should contain meaningful markdown"
            assert "example" in md_text.lower(), (
                f"content.md should contain content from the snapshot: {md_text[:300]}"
            )

    def test_extract_with_custom_prompt(self):
        """Extract hook should respect custom CLAUDECODEEXTRACT_PROMPT."""
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snap"
            snap_dir.mkdir()
            create_fake_snapshot(snap_dir)

            output_dir = snap_dir / "claudecodeextract"
            output_dir.mkdir()

            env = os.environ.copy()
            env["SNAP_DIR"] = str(snap_dir)
            env["CRAWL_DIR"] = str(Path(tmpdir) / "crawl")
            env["CLAUDECODEEXTRACT_ENABLED"] = "true"
            env["CLAUDECODEEXTRACT_MODEL"] = "haiku"
            env["CLAUDECODEEXTRACT_TIMEOUT"] = "90"
            env["CLAUDECODEEXTRACT_PROMPT"] = (
                "Read the readability/article.json file and extract the title. "
                "Write a JSON file named extracted.json in your output directory "
                'containing {"title": "<the title you found>"}.'
            )

            returncode, stdout, stderr = run_hook(
                EXTRACT_HOOK,
                TEST_URL,
                "test-custom-prompt",
                cwd=output_dir,
                env=env,
                timeout=120,
            )

            result = parse_jsonl_output(stdout)
            assert result is not None, f"No ArchiveResult. stderr: {stderr[:500]}"
            assert result["status"] == "succeeded", (
                f"Custom prompt extraction should succeed: {stderr[:500]}"
            )

            # Verify the custom output file was created
            extracted_file = output_dir / "extracted.json"
            assert extracted_file.exists(), (
                f"Custom prompt should create extracted.json. "
                f"Dir: {list(output_dir.iterdir())}"
            )
            extracted = json.loads(extracted_file.read_text())
            assert "title" in extracted, f"Should have 'title' key: {extracted}"
            assert "example" in extracted["title"].lower(), (
                f"Title should contain 'example': {extracted['title']}"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

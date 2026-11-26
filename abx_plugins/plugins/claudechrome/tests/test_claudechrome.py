"""
Tests for the claudechrome plugin.

Tests verify:
1. Hook scripts exist (install, config, snapshot)
2. Config schema is valid and declares chrome dependency
3. Snapshot hook skips when disabled
4. Snapshot hook fails gracefully when API key is missing
5. Snapshot hook fails gracefully without Chrome session
6. Templates exist
7. Full integration: launches Chrome, runs Claude computer-use, produces output files
"""

import json
import subprocess
import tempfile


import pytest

from abx_plugins.plugins.base.testing import (
    get_hydrated_required_binaries,
    get_plugin_dir,
    get_hook_script,
    parse_jsonl_output,
    run_hook,
)

from abx_plugins.plugins.base.utils import load_config

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    get_test_env,
)


PLUGIN_DIR = get_plugin_dir(__file__)

_CONFIG_HOOK = get_hook_script(PLUGIN_DIR, "on_CrawlSetup__*_claudechrome_config*")
_SNAPSHOT_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_claudechrome*")

if _SNAPSHOT_HOOK is None:
    raise FileNotFoundError(f"Snapshot hook not found in {PLUGIN_DIR}")
SNAPSHOT_HOOK = _SNAPSHOT_HOOK
TEST_URL = "https://example.com"
CHROME_STARTUP_TIMEOUT_SECONDS = 45


@pytest.fixture
def claudechrome_test_url(chrome_test_urls):
    """Serve a deterministic test page with a 'Show More' button for Claude to click."""
    return chrome_test_urls["claudechrome_url"]


class TestClaudeChromePlugin:
    """Test the claudechrome plugin."""

    def test_hook_exists(self):
        """All hook scripts should exist."""
        assert _CONFIG_HOOK is not None and _CONFIG_HOOK.exists(), (
            "Config hook not found"
        )
        assert SNAPSHOT_HOOK.exists(), "Snapshot hook not found"

    def test_hook_runs_at_correct_priorities(self):
        """Hooks should run at the expected priorities."""
        assert _CONFIG_HOOK is not None
        assert "__96_" in _CONFIG_HOOK.name, (
            f"Config hook should be priority 96: {_CONFIG_HOOK.name}"
        )
        assert "__47_" in SNAPSHOT_HOOK.name, (
            f"Snapshot hook should be priority 47: {SNAPSHOT_HOOK.name}"
        )

    def test_snapshot_runs_after_infiniscroll_before_singlefile(self):
        """Snapshot hook priority 47 is after infiniscroll (45) and before singlefile (50)."""
        name = SNAPSHOT_HOOK.name
        priority = int(name.split("__")[1].split("_")[0])
        assert 45 < priority < 50, (
            f"Priority {priority} should be between infiniscroll (45) and singlefile (50)"
        )

    def test_config_json_exists_and_valid(self):
        """config.json should exist and declare chrome dependency."""
        config_path = PLUGIN_DIR / "config.json"
        assert config_path.exists(), "config.json not found"

        config = json.loads(config_path.read_text())
        assert config.get("$schema") == "http://json-schema.org/draft-07/schema#"
        assert "chrome" in config.get("required_plugins", [])
        assert "CLAUDECHROME_ENABLED" in config["properties"]
        assert "CLAUDECHROME_PROMPT" in config["properties"]
        assert "CLAUDECHROME_TIMEOUT" in config["properties"]
        assert "CLAUDECHROME_MODEL" in config["properties"]
        assert "ANTHROPIC_API_KEY" in config["properties"]

    def test_model_config_retains_explicit_aliases_and_legacy_id(self):
        path = PLUGIN_DIR / "config.json"
        assert (
            load_config(path, environ={}, hydrate_binaries=False).CLAUDECHROME_MODEL
            == "claude-sonnet-5-5"
        )
        for model in ("haiku", "opus", "claude-haiku-4-5-20251001"):
            config = load_config(
                path,
                environ={"CLAUDECHROME_MODEL": model},
                hydrate_binaries=False,
            )
            assert config.CLAUDECHROME_MODEL == model

    def test_computer_use_wire_protocol_and_legacy_pins(self):
        """Exercise the production JS adapter with the documented wire shapes."""
        checks = """
const assert = require("node:assert/strict");
const wire = require(process.argv[1]);
const viewport = { width: 1280, height: 720 };
for (const family of ["sonnet", "opus", "haiku"]) {
  const model = `claude-${family}-5-5`;
  assert.equal(wire.modelID(family), model);
  const protocol = wire.computerProtocol(model, viewport);
  assert.equal(protocol.beta, undefined);
  const [tool] = protocol.fields.tools;
  assert.equal(tool.type, "computer_toolset_20260801");
  for (const legacyField of ["name", "display_width_px", "display_height_px", "display_number"]) {
    assert.equal(legacyField in tool, false);
  }
  assert.equal(tool.configs.zoom.enabled, false);
  assert.equal(protocol.fields.tool_choice.disable_parallel_tool_use, true);
}
for (const [pins, version] of [
  [["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"], "20250124"],
  [["claude-sonnet-4-6", "claude-opus-4-6", "claude-opus-4-5-20251101"], "20251124"],
]) {
  for (const pin of pins) {
    assert.equal(wire.modelID(pin), pin);
    const protocol = wire.computerProtocol(pin, viewport);
    assert.equal(protocol.fields.tools[0].type, `computer_${version}`);
    assert.equal(protocol.fields.tools[0].display_width_px, viewport.width);
    assert.equal(protocol.beta, `computer-use-${version.slice(0, 4)}-${version.slice(4, 6)}-${version.slice(6)}`);
  }
}
const use = {
  type: "tool_use", id: "toolu_click", name: "left_click",
  toolset_name: "computer", input: { coordinate: [500, 300] },
};
const image = { width: 2560, height: 1440 };
assert.deepEqual(wire.computerAction(use, true, image, viewport), {
  action: "left_click", coordinate: [250, 150],
});
assert.equal(
  wire.computerAction({
    ...use, name: "key", input: { text: "Tab", repeat: 4 },
  }, true, image, viewport).repeat,
  4
);
assert.throws(() => wire.computerAction({
  ...use, input: { coordinate: [2560, 0] },
}, true, image, viewport), /outside/);
assert.throws(() => wire.computerAction({
  ...use, toolset_name: "browser",
}, true, image, viewport), /Unsupported/);
assert.deepEqual(wire.toolResult(use, true, null, "halted"), {
  type: "tool_result", tool_use_id: "toolu_click", toolset_name: "computer",
  is_error: true, content: "Not executed: an earlier computer action in this turn failed.",
});
const followup = { ...use, id: "toolu_followup", name: "screenshot", input: {} };
const assistantContent = [{ type: "thinking", thinking: "", signature: "sig" }, use, followup];
const messages = [{ role: "user", content: "Open details" }];
wire.appendComputerResults(messages, assistantContent, [
  wire.toolResult(use, true, "aW1hZ2U="),
  wire.toolResult(followup, true, null, "one-action"),
]);
assert.equal(messages[1].content, assistantContent);
assert.deepEqual(messages[2].content.map((result) => result.tool_use_id), ["toolu_click", "toolu_followup"]);
assert.equal(messages[2].content[0].content[0].source.data, "aW1hZ2U=");
assert.equal(messages[2].content[1].is_error, true);
assert.equal(
  wire.toolResult(use, true, null, "browser failure at /private/fixture").content,
  "The computer action could not be completed."
);
const legacy = {
  type: "tool_use", id: "toolu_legacy", name: "computer",
  input: { action: "left_click", coordinate: [500, 300] },
};
assert.deepEqual(wire.computerAction(legacy, false, null, viewport), legacy.input);
assert.equal("toolset_name" in wire.toolResult(legacy, false, "AA=="), false);
assert.deepEqual(wire.screenshotOptions(viewport, 1), {});
assert.equal(wire.screenshotOptions(viewport, 2).clip.scale, 1568 / 2560);
"""
        result = subprocess.run(
            ["node", "-e", checks, str(PLUGIN_DIR / "computer_use.js")],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr

    def test_config_has_default_prompt(self):
        """Config should have a sensible default prompt."""
        config_path = PLUGIN_DIR / "config.json"
        config = json.loads(config_path.read_text())
        default_prompt = config["properties"]["CLAUDECHROME_PROMPT"]["default"]
        assert len(default_prompt) > 30, "Default prompt should be meaningful"
        assert "expand" in default_prompt.lower() or "click" in default_prompt.lower()

    def test_templates_exist(self):
        """Template files should exist."""
        templates_dir = PLUGIN_DIR / "templates"
        assert (templates_dir / "icon.html").exists()
        assert (templates_dir / "card.html").exists()
        assert (templates_dir / "full.html").exists()

    def test_required_binaries_include_claudechrome(self):
        """required_binaries should declare the claudechrome extension asset."""
        env = get_test_env()
        required_binaries = get_hydrated_required_binaries(PLUGIN_DIR, env=env)
        claudechrome_binary = next(
            binary
            for binary in required_binaries
            if binary.get("name") == "claudechrome"
        )
        assert claudechrome_binary["binproviders"] == "env,chromewebstore"

    def test_config_hook_reports_skipped_when_disabled(self):
        """Config hook should report skipped when CLAUDECHROME_ENABLED=false."""
        assert _CONFIG_HOOK is not None
        env = get_test_env()
        env["CLAUDECHROME_ENABLED"] = "false"

        with tempfile.TemporaryDirectory() as tmpdir:
            env["CRAWL_DIR"] = tmpdir
            returncode, stdout, stderr = run_hook(
                _CONFIG_HOOK,
                TEST_URL,
                "test-config-disabled",
                cwd=tmpdir,
                env=env,
                timeout=30,
            )

            assert returncode == 10, f"Hook failed: {stderr}"
            assert stdout.strip() == "CLAUDECHROME_ENABLED=False"

    def test_snapshot_hook_skips_when_disabled(self):
        """Snapshot hook should skip when CLAUDECHROME_ENABLED=false."""
        env = get_test_env()
        env["CLAUDECHROME_ENABLED"] = "false"

        with tempfile.TemporaryDirectory() as tmpdir:
            env["SNAP_DIR"] = tmpdir
            returncode, stdout, stderr = run_hook(
                SNAPSHOT_HOOK,
                TEST_URL,
                "test-snapshot",
                cwd=tmpdir,
                env=env,
                timeout=30,
            )

            assert returncode == 0, f"Hook failed: {stderr}"
            result = parse_jsonl_output(stdout)
            assert result is not None, f"Expected JSONL output, got: {stdout}"
            assert result["status"] == "skipped"

    def test_snapshot_hook_fails_without_api_key(self):
        """Snapshot hook should fail when ANTHROPIC_API_KEY is not set."""
        env = get_test_env()
        env["CLAUDECHROME_ENABLED"] = "true"
        env.pop("ANTHROPIC_API_KEY", None)

        with tempfile.TemporaryDirectory() as tmpdir:
            env["SNAP_DIR"] = tmpdir
            returncode, stdout, stderr = run_hook(
                SNAPSHOT_HOOK,
                TEST_URL,
                "test-snapshot",
                cwd=tmpdir,
                env=env,
                timeout=30,
            )

            assert returncode == 1
            result = parse_jsonl_output(stdout)
            assert result is not None, f"Expected JSONL output, got: {stdout}"
            assert result["status"] == "failed"
            assert "ANTHROPIC_API_KEY" in result["output_str"]

    def test_snapshot_hook_fails_without_chrome_session(self):
        """Snapshot hook should fail gracefully when no Chrome session exists."""
        env = get_test_env()
        env["CLAUDECHROME_ENABLED"] = "true"
        env["ANTHROPIC_API_KEY"] = "sk-ant-test-key"

        with tempfile.TemporaryDirectory() as tmpdir:
            env["SNAP_DIR"] = tmpdir
            returncode, stdout, stderr = run_hook(
                SNAPSHOT_HOOK,
                TEST_URL,
                "test-no-chrome",
                cwd=tmpdir,
                env=env,
                timeout=30,
            )

            assert returncode != 0, "Should fail when no Chrome session exists"
            # Hook may crash before emitting JSONL (puppeteer not loaded) or
            # emit a failed ArchiveResult — either is acceptable
            result = parse_jsonl_output(stdout)
            if result is not None:
                assert result["status"] == "failed"
            else:
                err_lower = stderr.lower()
                assert any(
                    x in err_lower for x in ["chrome", "cdp", "puppeteer", "module"]
                ), f"Should mention chrome/CDP/puppeteer in error: {stderr}"


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_api_rejection_fails_and_preserves_capture(tmp_path, claudechrome_test_url):
    with chrome_session(
        tmp_path,
        crawl_id="test-claudechrome-rejection",
        snapshot_id="snap-claudechrome-rejection",
        test_url=claudechrome_test_url,
        timeout=CHROME_STARTUP_TIMEOUT_SECONDS,
    ) as (_launch, _pid, snapshot_chrome_dir, env):
        output = snapshot_chrome_dir.parent / "claudechrome"
        output.mkdir()
        env.update(
            CLAUDECHROME_ENABLED="true",
            ANTHROPIC_API_KEY="invalid-key-for-authentication-rejection",
            CLAUDECHROME_MAX_ACTIONS="1",
            CLAUDECHROME_TIMEOUT="30",
        )
        code, stdout, stderr = run_hook(
            SNAPSHOT_HOOK,
            claudechrome_test_url,
            "snap-claudechrome-rejection",
            cwd=output,
            env=env,
            timeout=60,
        )
        assert "authentication_error" in stderr, stderr
        assert code == 1, stdout + stderr
        result = parse_jsonl_output(stdout)
        assert result is not None
        assert result["status"] == "failed"
        assert "authentication_error" in result["output_str"]
        conversation = json.loads((output / "conversation.json").read_text())
        assert conversation["success"] is False
        assert any(
            message["role"] == "error" and "authentication_error" in message["text"]
            for message in conversation["conversation"]
        )
        assert "authentication_error" in (output / "conversation.txt").read_text()
        screenshot = (output / "screenshot_initial.png").read_bytes()
        assert screenshot.startswith(b"\x89PNG\r\n\x1a\n")
        assert int.from_bytes(screenshot[16:20], "big") > 1
        assert int.from_bytes(screenshot[20:24], "big") > 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

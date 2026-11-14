"""Real hook invocation, artifact boundaries, and strict tag validation."""

import importlib.util
import json
import os
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_records, run_hook
from abx_plugins.plugins.opencode.analysis import saved_text

PLUGIN_DIR = Path(__file__).resolve().parents[1]
HOOK = PLUGIN_DIR / "on_Snapshot__85_autotag.py"


@pytest.mark.parametrize(
    "enabled, expected",
    [("false", "skipped"), ("true", "noresults")],
)
def test_hook_does_not_generate_tags_without_saved_content(tmp_path, enabled, expected):
    env = {
        **os.environ,
        "SNAP_DIR": str(tmp_path),
        "AUTOTAG_ENABLED": enabled,
        "EXTRA_CONTEXT": json.dumps({"snapshot_id": "native-tag-test"}),
    }
    code, stdout, stderr = run_hook(
        HOOK,
        "https://news.ycombinator.com/",
        "native-tag-test",
        cwd=tmp_path,
        env=env,
        timeout=30,
    )
    assert code == 0, stderr
    records = parse_jsonl_records(stdout)
    assert records[-1]["status"] == expected
    assert records[-1]["snapshot_id"] == "native-tag-test"
    assert not any(record["type"] == "Tag" for record in records)
    assert not (tmp_path / "autotag/tags.jsonl").exists()


def test_tag_formatting_and_invalid_output():
    spec = importlib.util.spec_from_file_location("autotag_hook", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.format_tags(
        {"tags": ["Machine Learning", "Machine Learning", "Hardware"]},
        style="lower_hyphens",
    ) == ["machine-learning", "hardware"]
    with pytest.raises(ValueError, match="JSON object"):
        module.format_tags({"tags": [None]}, style="as_generated")


def test_tag_output_uses_jsonl_records():
    schema = json.loads((PLUGIN_DIR / "config.json").read_text())
    assert schema["default_output_path"] == "tags.jsonl"
    assert "application/x-ndjson" in schema["output_mimetypes"]


def test_custom_prompt_is_loaded_as_plain_plugin_config():
    from abx_plugins.plugins.base.utils import load_config

    prompt = "Prefer these tags when relevant: Hardware, Privacy, Home Automation."
    config = load_config(
        PLUGIN_DIR / "config.json",
        environ={"AUTOTAG_PROMPT": prompt},
        hydrate_binaries=False,
    )
    assert config.AUTOTAG_PROMPT == prompt


def test_saved_inputs_cannot_follow_external_symlinks(tmp_path):
    external = PLUGIN_DIR / "README.md"
    (tmp_path / "outside.md").symlink_to(external)
    assert saved_text(tmp_path, ["outside.md"]) == ("", "")
    with pytest.raises(ValueError, match="relative snapshot"):
        saved_text(tmp_path, ["../outside.md"])

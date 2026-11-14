"""Real summary hook behavior without provider credentials or saved inputs."""

import os
import json
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_records, run_hook

HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__86_autosummary.py"


def test_summary_output_uses_markdown():
    schema = json.loads((HOOK.parent / "config.json").read_text())
    assert schema["default_output_path"] == "summary.md"
    assert "text/markdown" in schema["output_mimetypes"]


@pytest.mark.parametrize(
    "enabled, expected",
    [("false", "skipped"), ("true", "noresults")],
)
def test_hook_does_not_fabricate_a_summary(tmp_path, enabled, expected):
    env = {**os.environ, "SNAP_DIR": str(tmp_path), "AUTOSUMMARY_ENABLED": enabled}
    code, stdout, stderr = run_hook(
        HOOK,
        "https://news.ycombinator.com/",
        "native-tag-test",
        cwd=tmp_path,
        env=env,
        timeout=30,
    )
    assert code == 0, stderr
    assert parse_jsonl_records(stdout)[-1]["status"] == expected
    assert not (tmp_path / "autosummary/summary.md").exists()

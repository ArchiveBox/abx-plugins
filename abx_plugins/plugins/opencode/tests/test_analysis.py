"""Native OpenCode configuration and real hooks never select an anonymous model."""

import json
import os
import sqlite3
from pathlib import Path

import pytest
import requests

from abx_plugins.plugins.base.testing import (
    install_required_binary_from_config,
    parse_jsonl_records,
    run_hook,
)
from abx_plugins.plugins.opencode import runtime
from abx_plugins.plugins.opencode.analysis import _configured_model

PLUGIN = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def analysis_environment(tmp_path_factory):
    env = os.environ.copy()
    env.setdefault("ABXPKG_LIB_DIR", str(tmp_path_factory.mktemp("opencode-lib")))
    env.pop("OPENCODE_CONFIG", None)
    env.pop("OPENCODE_CONFIG_DIR", None)
    for name in ("node", "git", "browser-harness", "stagehand", "opencode"):
        install_required_binary_from_config(PLUGIN, name, env=env)
    return env


@pytest.mark.parametrize("plugin", ["autotag", "autosummary"])
def test_saved_capture_without_selected_model_is_skipped(
    tmp_path,
    analysis_environment,
    plugin,
):
    response = requests.get("https://news.ycombinator.com/", timeout=30)
    response.raise_for_status()
    assert "Hacker News" in response.text
    capture = tmp_path / "dom/output.html"
    capture.parent.mkdir()
    capture.write_bytes(response.content)
    state = tmp_path / "opencode"
    env = {
        **analysis_environment,
        "SNAP_DIR": str(tmp_path),
        "DATA_DIR": str(tmp_path),
        "OPENCODE_STATE_DIR": str(state),
        "OPENCODE_WORKDIR": str(tmp_path),
        f"{plugin.upper()}_ENABLED": "true",
        f"{plugin.upper()}_MODEL": "",
    }
    hook = next((PLUGIN.parent / plugin).glob("on_Snapshot__*.py"))
    code, stdout, stderr = run_hook(
        hook,
        response.url,
        "unconfigured-analysis",
        cwd=tmp_path,
        env=env,
    )
    assert code == 0, stderr
    records = parse_jsonl_records(stdout)
    assert len(records) == 1, records
    assert records[0]["status"] == "skipped"
    assert records[0]["output_str"] == "No OpenCode model selected."
    assert capture.read_bytes() == response.content
    assert not (tmp_path / plugin).exists()
    # Native config inspection initializes its database, but must not start inference.
    with sqlite3.connect(state / "data/opencode/opencode.db") as db:
        assert db.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 0


def test_native_jsonc_default_and_recent_model_selection(
    tmp_path,
    analysis_environment,
):
    settings = runtime._settings(
        {**analysis_environment, "OPENCODE_STATE_DIR": str(tmp_path / "opencode")},
        tmp_path,
    )
    binary, binary_env = runtime._resolve_binary(settings["binary"], settings["config"])
    env = runtime.process_environment(settings, binary_env)
    config = settings["config_home"] / "opencode/opencode.jsonc"
    config.parent.mkdir(parents=True)
    config.write_text('// Native default\n{"model": "openai/gpt-5.4-mini"}\n')
    assert _configured_model(binary, settings, env, 30) == "openai/gpt-5.4-mini"
    recent = settings["state_home"] / "opencode/model.json"
    recent.parent.mkdir(parents=True, exist_ok=True)
    recent.write_text(
        json.dumps({"recent": [{"providerID": "openai", "modelID": "gpt-5.4"}]}),
    )
    assert _configured_model(binary, settings, env, 30) == "openai/gpt-5.4-mini"
    config.write_text("{}\n")
    assert _configured_model(binary, settings, env, 30) == "openai/gpt-5.4"
    recent.unlink()
    assert _configured_model(binary, settings, env, 30) == ""

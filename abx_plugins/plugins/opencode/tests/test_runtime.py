import json
import signal
import subprocess

import pytest


def test_stop_owned_process_falls_back_for_stopped_process_without_dedicated_group():
    from abx_plugins.plugins.opencode import runtime

    process = subprocess.Popen(["sleep", "60"])
    try:
        process.send_signal(signal.SIGSTOP)
        runtime._stop_owned_process(process)
        assert process.returncode == -signal.SIGTERM
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_opencode_state_dir_is_separate_from_workdir(tmp_path):
    from abx_plugins.plugins.opencode import runtime

    workdir = tmp_path / "workdir"
    state_dir = tmp_path / "state"
    settings = runtime._settings(
        {
            "OPENCODE_WORKDIR": str(workdir),
            "OPENCODE_STATE_DIR": str(state_dir),
        },
    )
    runtime._ensure_project_files(settings)

    assert settings["workdir"] == workdir
    assert settings["opencode_dir"] == state_dir
    assert settings["config_home"] == state_dir / "config"
    assert settings["data_home"] == state_dir / "data"
    assert settings["state_home"] == state_dir / "state"
    editable_skill = state_dir / "SKILL.md"
    loaded_skill = (
        state_dir / "config" / "opencode" / "skills" / "archivebox" / "SKILL.md"
    )
    assert editable_skill.exists()
    assert loaded_skill.is_symlink()
    assert loaded_skill.resolve() == editable_skill.resolve()
    assert (
        f"ArchiveBox collection directory: {settings['archivebox_data_dir']}"
        in editable_skill.read_text()
    )
    opencode_config = json.loads(
        (state_dir / "config" / "opencode" / "opencode.jsonc").read_text(),
    )
    assert opencode_config["model"] == "opencode/big-pickle"
    assert opencode_config["snapshot"] is False


@pytest.mark.parametrize(
    "existing_config",
    [
        '{"model": "anthropic/claude-sonnet-4-5"}\n',
        '{\n  // Keep the administrator-selected model.\n  "model": "anthropic/claude-sonnet-4-5",\n}\n',
        '{\n  // Schema-only files are still user-owned.\n  "$schema": "https://opencode.ai/config.json",\n}\n',
    ],
)
def test_opencode_preserves_existing_config(tmp_path, existing_config):
    from abx_plugins.plugins.opencode import runtime

    state_dir = tmp_path / "state"
    config_path = state_dir / "config" / "opencode" / "opencode.jsonc"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(existing_config)

    runtime._ensure_project_files(
        runtime._settings(
            {
                "OPENCODE_WORKDIR": str(tmp_path / "workdir"),
                "OPENCODE_STATE_DIR": str(state_dir),
            },
        ),
    )

    assert config_path.read_text() == existing_config


def test_opencode_defaults_to_the_archivebox_collection():
    from abx_plugins.plugins.opencode import runtime

    settings = runtime._settings({})

    assert settings["opencode_dir"] == settings["archivebox_data_dir"] / "opencode"
    assert settings["workdir"] == settings["archivebox_data_dir"]
    assert settings["timeout"] == 120


def test_opencode_rewrites_vite_preload_assets():
    from abx_plugins.plugins.opencode import runtime

    body = b'const BL="modulepreload",UL=function(t){return"/"+t};const icon="/assets/sprite.svg#anthropic"'
    rewritten = runtime._rewrite_text(
        body,
        {"origin": "http://127.0.0.1:4096"},
    ).decode()

    assert 'return"/"+t' not in rewritten
    assert 'return"/admin/agent/opencode/"+t' in rewritten
    assert '"/admin/agent/opencode/assets/sprite.svg#anthropic"' in rewritten

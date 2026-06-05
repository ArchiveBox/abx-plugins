import json
import os
from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import assert_isolated_snapshot_env
from abx_plugins.plugins.base.utils import (
    BASE_CONFIG_PATH,
    build_binproviders,
    build_config_model,
    load_config,
    resolve_plugin_configs,
)


CHROME_CONFIG = (
    Path(__file__).parents[1] / "abx_plugins" / "plugins" / "chrome" / "config.json"
)


def test_assert_isolated_snapshot_env_allows_separate_sibling_dirs(
    tmp_path: Path,
) -> None:
    env = {
        "HOME": str(tmp_path / "home"),
        "SNAP_DIR": str(tmp_path / "snap"),
        "LIB_DIR": str(tmp_path / "lib"),
        "PERSONAS_DIR": str(tmp_path / "personas"),
    }

    assert_isolated_snapshot_env(env)


def test_assert_isolated_snapshot_env_rejects_nested_home_and_snap(
    tmp_path: Path,
) -> None:
    with pytest.raises(AssertionError, match="HOME must not be nested under SNAP_DIR"):
        assert_isolated_snapshot_env(
            {
                "HOME": str(tmp_path / "snap" / "home"),
                "SNAP_DIR": str(tmp_path / "snap"),
            },
        )


def test_assert_isolated_snapshot_env_allows_ambient_home_ancestor_of_snap(
    tmp_path: Path,
) -> None:
    assert_isolated_snapshot_env(
        {
            "HOME": str(tmp_path),
            "SNAP_DIR": str(tmp_path / "snap"),
        },
    )


def test_load_config_preserves_resolved_runtime_dirs_over_schema_defaults(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    crawl_dir = tmp_path / "crawl"
    snap_dir = tmp_path / "snap"

    config = load_config(
        CHROME_CONFIG,
        global_config={
            "DATA_DIR": str(data_dir),
            "CRAWL_DIR": str(crawl_dir),
            "SNAP_DIR": str(snap_dir),
        },
        environ={},
        user_config={},
    )

    assert config.DATA_DIR == str(data_dir)
    assert config.CRAWL_DIR == str(crawl_dir)
    assert config.SNAP_DIR == str(snap_dir)


def test_resolve_plugin_configs_derives_chrome_extensions_dir_from_lib_dir(
    tmp_path: Path,
) -> None:
    lib_dir = tmp_path / "lib"

    resolved = resolve_plugin_configs(
        {
            "base": json.loads(BASE_CONFIG_PATH.read_text()),
            "chrome": json.loads(CHROME_CONFIG.read_text()),
        },
        global_config={"LIB_DIR": str(lib_dir)},
        user_config={},
        environ={},
    )

    assert resolved["chrome"]["CHROME_EXTENSIONS_DIR"] == str(
        lib_dir / "chromewebstore" / "extensions",
    )


def test_build_binproviders_scopes_env_provider_to_lib_dir(
    tmp_path: Path,
) -> None:
    lib_dir = tmp_path / "lib"
    ambient_bin = tmp_path / "ambient-bin"
    ambient_bin.mkdir()

    providers = build_binproviders(
        "env,pnpm",
        config={"LIB_DIR": str(lib_dir), "PATH": str(ambient_bin)},
        environ={"LIB_DIR": str(lib_dir), "PATH": str(ambient_bin)},
    )

    by_name = {provider.name: provider for provider in providers}
    by_name["env"].setup_PATH()
    by_name["pnpm"].setup_PATH()

    assert by_name["env"].install_root == lib_dir / "env"
    assert by_name["env"].PATH.split(os.pathsep)[:2] == [
        str(lib_dir / "env" / "bin"),
        str(ambient_bin),
    ]
    assert by_name["pnpm"].install_root == lib_dir / "pnpm"


def test_build_config_model_infers_typed_fields_from_schema() -> None:
    config_model = build_config_model(
        "BaseConfig",
        {
            "CHROME_ARGS": {
                "type": "array",
                "default": ["--headless"],
                "items": {"type": "string"},
            },
            "ABX_INSTALL_CACHE": {
                "type": "object",
                "default": {},
                "additionalProperties": {"type": "string"},
            },
        },
    )

    config = config_model.model_validate(
        {
            "CHROME_ARGS": ["--no-first-run"],
            "ABX_INSTALL_CACHE": {"wget": "2026-03-24T00:00:00+00:00"},
        },
    )

    payload = config.model_dump(mode="json")
    assert payload["CHROME_ARGS"] == ["--no-first-run"]
    assert payload["ABX_INSTALL_CACHE"] == {
        "wget": "2026-03-24T00:00:00+00:00",
    }


def test_load_config_resolves_aliases_to_canonical_fields() -> None:
    config = load_config(
        BASE_CONFIG_PATH.parent.parent / "favicon" / "config.json",
        environ={},
        user_config={"SAVE_FAVICON": "false"},
    )

    assert config.FAVICON_ENABLED is False
    assert "SAVE_FAVICON" not in config.model_dump(mode="json")


def test_load_config_hydrates_binary_fields_from_abxpkg_env_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    fake_bin_dir = tmp_path / "bin"
    fake_bin_dir.mkdir()
    fake_tool = fake_bin_dir / "fake-tool"
    fake_tool.write_text("#!/bin/sh\necho 'fake-tool 1.0.0'\n")
    fake_tool.chmod(0o755)
    config_path = plugin_dir / "config.json"
    config_path.write_text(
        """
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "FakeTool",
  "type": "object",
  "additionalProperties": false,
  "required_binaries": [
    {
      "name": "{FAKE_TOOL_BINARY}",
      "binproviders": "env",
      "min_version": null
    }
  ],
  "properties": {
    "FAKE_TOOL_BINARY": {
      "type": "string",
      "default": "fake-tool"
    }
  }
}
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("FAKE_TOOL_BINARY", "fake-tool")
    monkeypatch.setenv("PATH", str(fake_bin_dir))

    config = load_config(config_path, user_config={})

    resolved = Path(config.FAKE_TOOL_BINARY)
    assert resolved.name == "fake-tool"
    assert resolved.exists()

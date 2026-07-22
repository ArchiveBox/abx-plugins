import json
import os
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import assert_isolated_snapshot_env
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
        "ABXPKG_LIB_DIR": str(tmp_path / "lib"),
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


def test_plugin_config_fallbacks_only_propagate_explicit_values() -> None:
    schemas = {
        "base": {
            "properties": {
                "TIMEOUT": {"type": "integer", "default": 60},
            },
        },
        "claudecode": {
            "properties": {
                "CLAUDECODE_TIMEOUT": {
                    "type": "integer",
                    "default": 120,
                    "x-fallback": "TIMEOUT",
                },
            },
        },
        "claudecodecleanup": {
            "properties": {
                "CLAUDECODECLEANUP_TIMEOUT": {
                    "type": "integer",
                    "default": 180,
                    "x-fallback": "CLAUDECODE_TIMEOUT",
                },
            },
        },
    }

    defaults = resolve_plugin_configs(
        schemas,
        global_config={"TIMEOUT": 60},
        user_config={},
        environ={},
    )
    global_override = resolve_plugin_configs(
        schemas,
        global_config={"TIMEOUT": 90},
        user_config={"TIMEOUT": "90"},
        environ={},
    )
    plugin_override = resolve_plugin_configs(
        schemas,
        user_config={"CLAUDECODE_TIMEOUT": "150"},
        environ={},
    )

    assert defaults["base"]["TIMEOUT"] == 60
    assert defaults["claudecode"]["CLAUDECODE_TIMEOUT"] == 120
    assert defaults["claudecodecleanup"]["CLAUDECODECLEANUP_TIMEOUT"] == 180
    assert global_override["claudecode"]["CLAUDECODE_TIMEOUT"] == 90
    assert global_override["claudecodecleanup"]["CLAUDECODECLEANUP_TIMEOUT"] == 90
    assert plugin_override["claudecode"]["CLAUDECODE_TIMEOUT"] == 150
    assert plugin_override["claudecodecleanup"]["CLAUDECODECLEANUP_TIMEOUT"] == 150


def test_chromewebstore_provider_derives_extensions_dir_from_lib_dir(
    tmp_path: Path,
) -> None:
    lib_dir = tmp_path / "lib"

    resolved = resolve_plugin_configs(
        {
            "base": json.loads(BASE_CONFIG_PATH.read_text()),
            "chrome": json.loads(CHROME_CONFIG.read_text()),
        },
        global_config={"ABXPKG_LIB_DIR": str(lib_dir)},
        user_config={},
        environ={},
    )
    provider = build_binproviders(
        "chromewebstore",
        config=resolved["chrome"],
        environ={"ABXPKG_LIB_DIR": str(lib_dir)},
    )[0]

    assert provider.ENV["CHROMEWEBSTORE_EXTENSIONS_DIR"] == str(
        lib_dir / "chromewebstore" / "extensions",
    )


def test_chromewebstore_provider_honors_specific_install_root(
    tmp_path: Path,
) -> None:
    lib_dir = tmp_path / "lib"
    extensions_root = tmp_path / "isolated-chromewebstore"
    config = {
        "ABXPKG_LIB_DIR": str(lib_dir),
        "ABXPKG_CHROMEWEBSTORE_ROOT": str(extensions_root),
    }

    provider = build_binproviders(
        "chromewebstore",
        config=config,
        environ=config,
    )[0]

    assert provider.install_root == extensions_root
    assert provider.ENV["CHROMEWEBSTORE_EXTENSIONS_DIR"] == str(
        extensions_root / "extensions",
    )


def test_build_binproviders_scopes_env_provider_to_lib_dir(
    tmp_path: Path,
) -> None:
    lib_dir = tmp_path / "lib"
    ambient_bin = tmp_path / "ambient-bin"
    ambient_bin.mkdir()

    providers = build_binproviders(
        "env,pnpm",
        config={"ABXPKG_LIB_DIR": str(lib_dir), "PATH": str(ambient_bin)},
        environ={"ABXPKG_LIB_DIR": str(lib_dir), "PATH": str(ambient_bin)},
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


def test_load_config_hydrates_chrome_node_binary_from_real_env_provider(
    tmp_path: Path,
) -> None:
    lib_dir = tmp_path / "lib"
    personas_dir = tmp_path / "personas"
    environ = {
        **os.environ,
        "ABXPKG_LIB_DIR": str(lib_dir),
        "NODE_BINARY": "node",
        "PATH": os.environ.get("PATH", ""),
    }

    config = load_config(
        CHROME_CONFIG,
        global_config={
            "ABXPKG_LIB_DIR": str(lib_dir),
            "PERSONAS_DIR": str(personas_dir),
        },
        environ=environ,
        user_config={"NODE_BINARY": "node"},
    )

    resolved = Path(config.NODE_BINARY)
    assert resolved == lib_dir / "env" / "bin" / "node"
    assert resolved.is_symlink()
    assert resolved.exists()

    version = subprocess.run(
        [str(resolved), "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert version.returncode == 0, version.stderr

from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import assert_isolated_snapshot_env
from abx_plugins.plugins.base.utils import (
    BASE_CONFIG_PATH,
    build_config_model,
    load_config,
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

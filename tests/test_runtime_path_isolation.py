from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import assert_isolated_snapshot_env
from abx_plugins.plugins.base.utils import load_config


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

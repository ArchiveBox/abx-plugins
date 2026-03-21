from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import assert_isolated_snapshot_env


def test_assert_isolated_snapshot_env_allows_separate_sibling_dirs(tmp_path: Path) -> None:
    env = {
        "HOME": str(tmp_path / "home"),
        "SNAP_DIR": str(tmp_path / "snap"),
        "LIB_DIR": str(tmp_path / "lib"),
        "PERSONAS_DIR": str(tmp_path / "personas"),
    }

    assert_isolated_snapshot_env(env)


def test_assert_isolated_snapshot_env_rejects_nested_home_and_snap(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="HOME must not contain SNAP_DIR"):
        assert_isolated_snapshot_env(
            {
                "HOME": str(tmp_path),
                "SNAP_DIR": str(tmp_path / "snap"),
            },
        )

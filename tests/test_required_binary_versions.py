from pathlib import Path

import pytest
from abxpkg import BinaryLoadError

from abx_plugins.plugins.base.utils import load_required_binary


@pytest.mark.parametrize("absolute_path", [False, True])
def test_required_binary_rejects_newer_installed_version(absolute_path: bool) -> None:
    record = {"name": "uv", "binproviders": "env"}
    installed = load_required_binary(record)
    assert installed.loaded_abspath
    assert Path(installed.loaded_abspath).is_file()
    assert installed.loaded_version
    if absolute_path:
        record["name"] = str(installed.loaded_abspath)

    # A real installed executable must not satisfy an incompatible ceiling,
    # including when a runner has already projected its absolute path.
    with pytest.raises(BinaryLoadError):
        load_required_binary({**record, "max_version": "0.0.1"})

    exact = str(installed.loaded_version)
    selected = load_required_binary(
        {**record, "min_version": exact, "max_version": exact},
    )
    assert selected.loaded_abspath == installed.loaded_abspath
    assert selected.loaded_version == installed.loaded_version

"""SingleFile integration fixtures; dependency resolution stays with abxpkg."""

import json
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import (
    get_plugin_dir,
    install_required_binary_from_config,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_extension_install_env,
)

PLUGIN_DIR = get_plugin_dir(__file__)


@pytest.fixture(scope="session")
def singlefile_install_state(tmp_path_factory):
    """Resolve the real extension once and reuse its provider-owned installation."""
    install_root = tmp_path_factory.mktemp("singlefile-ext")
    env_install, extensions_dir = chrome_extension_install_env(install_root)

    loaded = install_required_binary_from_config(
        PLUGIN_DIR,
        "singlefile",
        env=env_install,
    )
    assert loaded.loaded_abspath is not None, (
        "abxpkg did not resolve SingleFile extension"
    )
    assert loaded.loaded_abspath.parent == extensions_dir

    cache_candidates = (
        extensions_dir.parent / "singlefile.extension.json",
        extensions_dir / "singlefile.extension.json",
    )
    cache_file = next((path for path in cache_candidates if path.exists()), None)
    assert cache_file is not None, (
        "Extension cache file not created in any expected location: "
        + ", ".join(str(path) for path in cache_candidates)
    )

    payload = json.loads(cache_file.read_text())
    unpacked_path = Path(payload.get("unpacked_path", ""))
    assert unpacked_path.exists(), f"Unpacked extension path missing: {unpacked_path}"
    assert (unpacked_path / "manifest.json").exists(), (
        f"Extension manifest missing: {unpacked_path / 'manifest.json'}"
    )

    return {
        "install_root": install_root,
        "abxpkg_lib_dir": Path(env_install["ABXPKG_LIB_DIR"]),
        "extensions_dir": extensions_dir,
        "cache_file": cache_file,
        "unpacked_path": unpacked_path,
    }

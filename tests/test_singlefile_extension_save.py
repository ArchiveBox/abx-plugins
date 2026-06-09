from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SINGLEFILE_HELPER = (
    REPO_ROOT
    / "abx_plugins"
    / "plugins"
    / "singlefile"
    / "singlefile_extension_save.js"
)


def test_singlefile_helper_source_installs_node_resolution_before_chrome_utils() -> (
    None
):
    """Parser-only guard: NODE_MODULES_DIR resolution must precede chrome_utils loading."""
    source = SINGLEFILE_HELPER.read_text(encoding="utf-8")

    ensure_call = "ensureNodeModuleResolution(module);"
    chrome_utils_load = 'require("../chrome/chrome_utils.js")'

    assert ensure_call in source
    assert chrome_utils_load in source
    assert source.index(ensure_call) < source.index(chrome_utils_load)

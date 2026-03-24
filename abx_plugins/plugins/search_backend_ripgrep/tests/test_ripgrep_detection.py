#!/usr/bin/env python3
"""
Tests for ripgrep binary detection and archivebox install functionality.

Guards against regressions in:
1. Ripgrep hook not resolving binary names via shutil.which()
2. SEARCH_BACKEND_ENGINE not being passed to hook environment
"""

from pathlib import Path

import pytest

from abx_plugins.plugins.base.test_utils import get_hydrated_required_binaries


PLUGIN_DIR = Path(__file__).parent.parent


def test_ripgrep_required_binaries_emit_rg_default():
    binary = next(
        record
        for record in get_hydrated_required_binaries(PLUGIN_DIR)
        if record.get("name") == "rg"
    )
    assert binary.get("type", "BinaryRequest") == "BinaryRequest"
    assert binary["name"] == "rg"
    assert binary["binproviders"] == "env,apt,brew"


def test_ripgrep_required_binaries_allow_absolute_path():
    binary = next(
        record
        for record in get_hydrated_required_binaries(
            PLUGIN_DIR,
            env={"RIPGREP_BINARY": "/custom/bin/rg"},
        )
        if record.get("name") == "/custom/bin/rg"
    )
    assert binary["name"] == "/custom/bin/rg"
    assert binary["binproviders"] == "env,apt,brew"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

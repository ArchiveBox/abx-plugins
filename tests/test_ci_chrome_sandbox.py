# ci-runner: hosted-linux
# Chrome's setuid sandbox is a Linux executable, used by the hosted CI jobs.
"""Resolve the sandbox from each provider's real installed browser."""

import json
import os
import runpy
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.utils import load_required_binary


@pytest.mark.parametrize("provider", ["playwright", "puppeteer"])
def test_sandbox_matches_installed_browser(tmp_path, provider):
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "abx_plugins/plugins/chrome/config.json").read_text())
    record = next(
        item
        for item in config["required_binaries"]
        if item["name"] == "{CHROME_BINARY}"
    )
    lib = str(tmp_path / "lib")
    installed = load_required_binary(
        {**record, "name": "chromium", "binproviders": provider},
        config={"ABXPKG_LIB_DIR": lib},
        environ={**os.environ, "ABXPKG_LIB_DIR": lib},
        install=True,
    )
    assert installed.loaded_binprovider.name == provider
    browser = Path(installed.loaded_abspath)
    resolve = runpy.run_path(str(root / ".github/ci_chrome_sandbox.py"))[
        "sandbox_for_browser"
    ]
    helper = resolve(browser)
    assert helper.read_bytes().startswith(b"\x7fELF")
    assert helper.parent.joinpath("chrome").is_file()
    result = subprocess.run(
        [str(helper), "--get-api"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) > 0

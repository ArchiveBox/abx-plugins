from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import install_binary_with_abxpkg
from abx_plugins.plugins.base.utils import has_staticfile_output


REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_UTILS_JS = REPO_ROOT / "abx_plugins" / "plugins" / "base" / "utils.js"

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


def _js_has_staticfile_output(staticfile_dir: Path) -> bool:
    node = install_binary_with_abxpkg("node", binproviders="env,apt,brew")
    assert node.loaded_abspath is not None
    result = subprocess.run(
        [
            str(node.loaded_abspath),
            "-e",
            (
                "const { hasStaticFileOutput } = require("
                f"{json.dumps(str(BASE_UTILS_JS))}"
                ");"
                "console.log(hasStaticFileOutput(process.argv[1]) ? 'true' : 'false');"
            ),
            str(staticfile_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip() == "true"


def test_staticfile_output_helpers_accept_real_static_artifact(
    tmp_path: Path,
    real_staticfile_output,
    local_staticfile_urls,
) -> None:
    snapshot_dir = real_staticfile_output(
        tmp_path,
        local_staticfile_urls["json"],
        "static-artifact",
    )
    staticfile_dir = snapshot_dir / "staticfile"

    assert has_staticfile_output(str(staticfile_dir)) is True
    assert _js_has_staticfile_output(staticfile_dir) is True


def test_staticfile_output_helpers_reject_real_html_noresults(
    tmp_path: Path,
    real_staticfile_output,
    local_staticfile_urls,
) -> None:
    snapshot_dir = real_staticfile_output(
        tmp_path,
        local_staticfile_urls["html"],
        "html-noresults",
    )
    staticfile_dir = snapshot_dir / "staticfile"

    assert has_staticfile_output(str(staticfile_dir)) is False
    assert _js_has_staticfile_output(staticfile_dir) is False

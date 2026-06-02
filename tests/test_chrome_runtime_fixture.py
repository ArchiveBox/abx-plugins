from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    require_chrome_runtime_impl,
)
from abxpkg import Binary, EnvProvider


def test_require_chrome_runtime_loads_node_and_npm():
    """Fixture should force actual binary resolution, not just construct providers."""
    require_chrome_runtime_impl()

    for name in ("node", "npm"):
        binary = Binary(name=name, binproviders=[EnvProvider()]).load()
        assert binary.abspath
        assert Path(str(binary.abspath)).exists()


def test_require_chrome_runtime_resolves_in_subprocess(
    tmp_path: Path,
):
    """The subprocess path should use the same provider-aware runtime resolution."""

    env = os.environ.copy()
    env["ABXPKG_LIB_DIR"] = str(tmp_path / "abxpkg-lib")
    env["ABXPKG_ENV_ROOT"] = str(tmp_path / "abxpkg-env")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from abx_plugins.plugins.chrome.tests.chrome_test_helpers "
                "import require_chrome_runtime_impl; "
                "require_chrome_runtime_impl()"
            ),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr

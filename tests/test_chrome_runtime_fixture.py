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


def test_require_chrome_runtime_fails_when_binary_resolution_fails(
    tmp_path: Path,
):
    """Fixture should fail fast when a required runtime binary cannot be loaded."""

    env = os.environ.copy()
    env["PATH"] = str(tmp_path)
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

    assert result.returncode != 0
    assert "Chrome integration prerequisites unavailable:" in result.stderr

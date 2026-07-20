from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_test_env,
    require_chrome_runtime_impl,
)


def test_require_chrome_runtime_loads_provider_managed_chrome_runtime():
    """Fixture should force actual provider-managed runtime resolution."""
    require_chrome_runtime_impl()

    env = get_test_env()
    node_modules_dir = Path(env["NODE_MODULES_DIR"])
    assert node_modules_dir.exists()

    for module_name in ("puppeteer", "abxbus"):
        result = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                "console.log(require.resolve(process.argv[1]))",
                module_name,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        resolved_path = Path(result.stdout.strip())
        assert resolved_path.exists(), result.stdout
        assert str(Path(env["ABXPKG_LIB_DIR"])) in str(resolved_path)

    assert str(node_modules_dir) in env["NODE_PATH"]

    chrome_binary = Path(os.environ["CHROME_BINARY"])
    assert chrome_binary.exists()
    version = subprocess.run(
        [str(chrome_binary), "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert version.returncode == 0, version.stderr
    assert "Chrom" in f"{version.stdout}\n{version.stderr}"


def test_require_chrome_runtime_resolves_in_subprocess(
    tmp_path: Path,
):
    """The subprocess path should use the same provider-aware runtime resolution."""

    env = os.environ.copy()
    env["ABXPKG_LIB_DIR"] = str(tmp_path / "lib")
    env["ABXPKG_ENV_ROOT"] = str(tmp_path / "env")
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
        timeout=300,
    )

    assert result.returncode == 0, result.stderr

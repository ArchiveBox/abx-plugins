import json
import os
import platform
import subprocess
import tempfile
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parent.parent
BINARY_HOOK = PLUGIN_DIR / "on_Binary__12_brew_install.py"
HOOK_TIMEOUT = 600 if platform.system().lower() == "linux" else 120


def test_brew_hook_respects_brew_only_and_maps_gnu_time():
    prefix_result = subprocess.run(
        ["brew", "--prefix"],
        capture_output=True,
        text=True,
        check=True,
    )
    expected_gtime = (
        Path(prefix_result.stdout.strip()) / "opt" / "gnu-time" / "bin" / "gtime"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        env = os.environ.copy()
        env["HOMEBREW_NO_AUTO_UPDATE"] = "1"

        result = subprocess.run(
            [
                str(BINARY_HOOK),
                "--machine-id=test-machine",
                "--binary-id=test-binary",
                "--plugin-name=test-suite",
                "--hook-name=test_brew_install",
                "--name=gtime",
                "--binproviders=brew",
                '--overrides={"brew":{"install_args":["gnu-time"]}}',
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
            timeout=HOOK_TIMEOUT,
        )

    assert result.returncode == 0, result.stderr

    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    assert records, result.stdout
    assert records[0]["type"] == "Binary"
    assert expected_gtime.is_file(), (
        f"Expected Homebrew gtime binary at {expected_gtime}"
    )
    assert records[0]["abspath"] == str(expected_gtime)
    assert records[0]["binprovider"] == "brew"

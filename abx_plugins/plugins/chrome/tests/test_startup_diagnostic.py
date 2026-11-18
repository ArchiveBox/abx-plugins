"""A real unavailable CDP endpoint retains a bounded, useful startup failure."""

import json
import socket
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_LAUNCH_HOOK,
    setup_test_env,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


def test_unavailable_cdp_reports_safe_error_before_cleanup(tmp_path):
    env = setup_test_env(tmp_path)
    chrome_dir = Path(env["CRAWL_DIR"]) / "chrome"
    chrome_dir.mkdir(parents=True, exist_ok=True)
    # Reserve an actual local port without listening: no other process can claim
    # it while the real WebSocket connection fails without a peer.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        port = unavailable.getsockname()[1]
        env.update(
            {
                "CHROME_CDP_URL": f"ws://127.0.0.1:{port}/private-fixture-marker",
                "CHROME_IS_LOCAL": "false",
            },
        )
        result = subprocess.run(
            [str(CHROME_LAUNCH_HOOK), "--crawl-id=diagnostic-fixture"],
            cwd=chrome_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert result.returncode == 1
    assert not result.stdout.strip()
    prefix = "ERROR: chrome startup "
    lines = [line for line in result.stderr.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1, result.stderr
    diagnostic = json.loads(lines[0].removeprefix(prefix))
    assert diagnostic.pop("code") in {"ECONNREFUSED", "ETIMEDOUT"}
    assert diagnostic == {
        "phase": "cdp-ready",
        "errorClass": "ErrorEvent",
        "syscall": "connect",
    }
    assert len(lines[0]) < 200
    assert "private-fixture-marker" not in lines[0]
    assert "127.0.0.1" not in lines[0]
    assert str(port) not in lines[0]
    assert not (chrome_dir / "browser.json").exists()
    assert not (chrome_dir / ".launch.lock").exists()

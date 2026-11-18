"""A real unavailable CDP endpoint retains a bounded, useful startup failure."""

import json
import socket
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_LAUNCH_HOOK,
    get_test_env,
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


@pytest.mark.parametrize(
    "case",
    ["missing_cdp_url", "missing_auth_file", "empty_auth_json", "invalid_auth_json"],
)
def test_startup_validation_diagnostic_has_safe_reason(tmp_path, case):
    plugin = Path(__file__).resolve().parents[1]
    auth_file = tmp_path / "private-auth-marker.json"
    if case == "empty_auth_json":
        auth_file.write_text("")
    elif case == "invalid_auth_json":
        auth_file.write_text("private-cookie-content-marker")
    script = f"""
const chrome = require({json.dumps(str(plugin / "chrome_utils.js"))});
const {{startupErrorDiagnostic}} = require({json.dumps(str(plugin / "startup_diagnostic.js"))});
(async () => {{
  try {{
    if ({json.dumps(case)} === 'missing_cdp_url') {{
      await chrome.ensureChromeSession({{CHROME_IS_LOCAL: false, CHROME_CDP_URL: '', outputDir: {json.dumps(str(tmp_path / "chrome"))}}});
    }} else {{
      // Invalid files must fail before a browser is needed or touched.
      await chrome.importCookiesFromFile(null, {json.dumps(str(auth_file))});
    }}
    process.exitCode = 2;
  }} catch (error) {{
    console.log(JSON.stringify(startupErrorDiagnostic(error)));
  }}
  try {{ JSON.parse('private-upstream-marker'); }} catch (error) {{
    console.log(JSON.stringify(startupErrorDiagnostic(error)));
  }}
}})();
"""
    env = get_test_env()
    result = subprocess.run(
        [env["NODE_BINARY"], "-e", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    diagnostic, untrusted = (json.loads(line) for line in result.stdout.splitlines())
    assert diagnostic == {
        "errorClass": "Error",
        "code": "unknown",
        "syscall": "unknown",
        "reason": case,
        "message": {
            "missing_cdp_url": "CHROME_IS_LOCAL=false requires CHROME_CDP_URL or an upstream published chrome session",
            "missing_auth_file": "Cookies file not found",
            "empty_auth_json": "Invalid JSON cookie export: file is empty",
            "invalid_auth_json": "Invalid JSON cookie export: expected a JSON object containing a cookies array",
        }[case],
    }
    assert untrusted == {
        "errorClass": "SyntaxError",
        "code": "unknown",
        "syscall": "unknown",
    }
    assert "private-" not in result.stdout
    assert str(tmp_path) not in result.stdout

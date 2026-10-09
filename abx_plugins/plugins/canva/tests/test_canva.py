"""Exercise public provider export restrictions with a real browser session."""

import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = (
    "https://www.canva.com/design/DAFzz5bvS5k/Fd3WlIpCPDxW9j8dQ_z7Bg/view?mode=preview"
)
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_canva.js"


@pytest.mark.parametrize(
    ("url", "status", "reason"),
    [
        (URL, "noresults", "Persona must be logged in to canva.com"),
        (
            "https://www.canva.com/design/DAGudZAYlEE/VzrUrqpV2RhkVHCg43lvKQ/view",
            "noresults",
            "No export in public view",
        ),
    ],
)
def test_public_export_restriction(
    tmp_path,
    ensure_chrome_test_prereqs,
    url,
    status,
    reason,
):
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=90,
        env_overrides={"AUTH_STORAGE_FILE": "", "CHROME_HEADLESS": "false"},
    ) as (_, _, chrome, env):
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == status, result.stdout
        assert record["output_str"] == reason, result.stdout
        assert result.stderr.strip() == reason
        assert not (chrome.parent / "canva" / "downloads.json").exists()

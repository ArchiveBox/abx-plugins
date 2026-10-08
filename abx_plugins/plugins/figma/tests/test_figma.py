"""Exercise public provider export restrictions with a real browser session."""

import subprocess
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://www.figma.com/design/0YpAEiii3cM0l3xidTbWPk/Style-Guide-Starter--Copy---Copy-?node-id=0-1&p=f"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_figma.js"


def test_public_export_requires_login_is_skipped(tmp_path, ensure_chrome_test_prereqs):
    with chrome_session(
        tmp_path,
        test_url=URL,
        timeout=90,
        env_overrides={
            "AUTH_STORAGE_FILE": "",
            "CHROME_HEADLESS": "false",
            "PERSONAS_DIR": str(tmp_path / "personas"),
            "ACTIVE_PERSONA": "Default",
            "CHROME_USER_DATA_DIR": str(
                tmp_path / "personas" / "Default" / "chrome_profile",
            ),
        },
    ) as (_, _, chrome, env):
        result = subprocess.run(
            [str(HOOK), f"--url={URL}"],
            cwd=chrome.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == "skipped", result.stdout
        assert record["output_str"] == "Persona must be logged in to figma.com"
        assert result.stderr.strip() == record["output_str"]
        assert not (chrome.parent / "figma" / "downloads.json").exists()

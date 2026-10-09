"""Exercise public provider export restrictions with a real browser session."""

import subprocess
from pathlib import Path

from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

URL = "https://miro.com/app/board/uXjVK4c-_uU=/"
HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_miro.js"


def test_public_export_restriction(tmp_path, ensure_chrome_test_prereqs):
    with chrome_session(
        tmp_path,
        test_url=URL,
        timeout=90,
        env_overrides={"AUTH_STORAGE_FILE": ""},
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
        assert record and record["status"] == "noresults", result.stdout
        assert record["output_str"] == "Persona must be logged in to miro.com"
        assert result.stderr.strip() == record["output_str"]
        assert not (chrome.parent / "miro" / "downloads.json").exists()

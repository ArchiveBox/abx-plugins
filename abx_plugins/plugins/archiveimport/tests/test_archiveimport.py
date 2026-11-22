"""Exercise the real executable without credentials or provider traffic."""

import json
import os
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__34_archiveimport.py"


def run_hook(tmp_path, url, **settings):
    env = dict(os.environ, SNAP_DIR=str(tmp_path), **settings)
    return subprocess.run(
        [str(HOOK), "--url", url],
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=180,
    )


def test_missing_perma_key_and_disabled(tmp_path):
    result = run_hook(
        tmp_path,
        "https://perma.cc/W6PY-UZ99",
        ARCHIVEIMPORT_PERMA_API_KEY="",
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "noresults"
    result = run_hook(
        tmp_path,
        "https://archive.org/details/hello-world.",
        ARCHIVEIMPORT_ENABLED="false",
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "skipped"

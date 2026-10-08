"""Parse the genuine subscription URL published on Calendify's event page."""

import json
import os
import subprocess
from pathlib import Path


def test_published_webcal_subscription(tmp_path):
    # https://calendify.com/session/K7NgQ1qGgL8 publishes this exact URL.
    subscription = "webcal://calendify.com/session/K7NgQ1qGgL8/ical"
    original = tmp_path / "subscription.txt"
    original.write_text(subscription + "\n")
    hook = Path(__file__).resolve().parents[1] / "on_Snapshot__71_parse_txt_urls.py"
    result = subprocess.run(
        [str(hook), f"--url={original.as_uri()}"],
        cwd=tmp_path,
        env={**os.environ, "SNAP_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    snapshots = [r for r in records if r["type"] == "Snapshot"]
    assert len(snapshots) == 1
    assert snapshots[0]["url"] == "https://calendify.com/session/K7NgQ1qGgL8/ical"
    assert snapshots[0]["depth"] == 1
    saved = tmp_path / "parse_txt_urls/urls.jsonl"
    assert [json.loads(line)["url"] for line in saved.read_text().splitlines()] == [
        snapshots[0]["url"],
    ]
    assert [r["status"] for r in records if r["type"] == "ArchiveResult"] == [
        "succeeded",
    ]

"""Exercise public provider export restrictions with a real browser session."""

import json
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
            URL.split("?", 1)[0],
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
        # Control readiness is logged before waiting so a killed hook still
        # identifies its last phase. The prerequisite itself remains exact.
        diagnostics = result.stderr.strip().splitlines()
        # Navigation/readiness observations arrive asynchronously, before the
        # final control/prerequisite messages. They contain no raw page text.
        observations = [
            line
            for line in diagnostics
            if line.startswith(
                (
                    "Canva export readiness: ",
                    "Canva export document response: ",
                    "Canva source document response: ",
                ),
            )
        ]
        for line in observations:
            state = json.loads(line.split(": ", 1)[1])
            if "controls" in state:
                assert set(state) == {
                    "frame",
                    "readyState",
                    "blocked",
                    "titleState",
                    "controls",
                }
                assert state["frame"] in {"main", "child"}
                assert state["titleState"] in {
                    "empty",
                    "canva",
                    "challenge",
                    "document",
                }
                assert all(
                    set(control)
                    == {"label", "role", "tag", "disabled", "visible", "exposed"}
                    for control in state["controls"]
                )
            else:
                assert state == {"status": 200, "challenge": False}
        diagnostics = [
            line
            for line in diagnostics
            if line not in observations
            and line
            not in {
                "Canva export DOM loaded",
                "Canva export load complete",
                "Canva source DOM loaded",
                "Canva source load complete",
            }
        ]
        if url == URL:
            assert observations
            prefix = "Canva export navigation: "
            assert diagnostics[0].startswith(prefix), result.stderr
            assert json.loads(diagnostics.pop(0)[len(prefix) :]) == {
                "status": 200,
                "path": "/design/DAFzz5bvS5k/Fd3WlIpCPDxW9j8dQ_z7Bg/view",
                "title": "FAFSA Night Flyer",
            }
        expected_diagnostics = (
            ["Waiting for Canva export control: button View template"]
            if url == URL
            else []
        )
        assert diagnostics == [*expected_diagnostics, reason]
        assert not (chrome.parent / "canva" / "downloads.json").exists()


def test_export_failure_keeps_native_callsite(tmp_path, ensure_chrome_test_prereqs):
    result = subprocess.run(
        [str(HOOK)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    record = parse_jsonl_output(result.stdout)
    assert record and record["status"] == "failed", result.stdout
    assert record["output_str"] == "Missing --url"
    assert "Error: Missing --url" in result.stderr
    assert "at main (" in result.stderr and HOOK.name in result.stderr

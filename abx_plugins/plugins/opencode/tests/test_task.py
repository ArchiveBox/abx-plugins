"""Real OpenCode task completion across model turns."""

import json
import os
import subprocess
from pathlib import Path

from abx_plugins.plugins.base.testing import install_required_binary_from_config


def test_task_continues_until_done(tmp_path):
    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY is required"
    plugin = Path(__file__).parents[1]
    env = {
        **os.environ,
        "ABXPKG_LIB_DIR": os.environ.get("ABXPKG_LIB_DIR", str(tmp_path / "lib")),
    }
    node = install_required_binary_from_config(plugin, "node", env=env)
    opencode = install_required_binary_from_config(plugin, "opencode", env=env)
    env["OPENCODE_BINARY"] = str(opencode.abspath)
    output = tmp_path / "output"
    output.mkdir()
    result = subprocess.run(
        [str(node.abspath), str(plugin / "task.js")],
        input=json.dumps(
            {
                "prompt": "This is a two-turn confirmation task. On your first turn, reply exactly READY and end the turn without calling any tools; the task is not yet complete. Wait for the next user message. On your second turn, the confirmation is complete: call the done tool.",
                "model": "openai/gpt-6.1-sol",
                "timeoutMs": 120000,
                "outputDir": str(output),
            },
        ),
        env=env,
        text=True,
        capture_output=True,
        timeout=135,
    )
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert record["done"] is True
    assert record["turns"] == 2, record
    events = [
        json.loads(line) for line in (output / "agent.log").read_text().splitlines()
    ]
    assert len({event["sessionID"] for event in events}) == 1
    assert any(
        event["type"] == "tool_use"
        and event["part"]["tool"] == "done"
        and event["part"]["state"]["status"] == "completed"
        for event in events
    )

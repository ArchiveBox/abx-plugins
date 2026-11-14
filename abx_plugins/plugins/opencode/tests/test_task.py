"""Real OpenCode task completion across model turns."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import time

import psutil
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
    request = json.dumps(
        {
            "prompt": "This is a two-turn confirmation task. On your first turn, reply exactly READY and end the turn without calling any tools; the task is not yet complete. Wait for the next user message. On your second turn, the confirmation is complete: call the done tool.",
            "model": "openai/gpt-6.1-sol",
            "timeoutMs": 120000,
            "outputDir": str(output),
        },
    )
    peak_rss = 0
    peak_processes = []
    with (
        subprocess.Popen(
            [str(node.abspath), str(plugin / "task.js")],
            env=env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        completed = executor.submit(process.communicate, request, timeout=135)
        while not completed.done():
            try:
                parent = psutil.Process(process.pid)
                used = parent.memory_info().rss
                processes = [(parent.pid, parent.name(), used)]
                for child in parent.children(recursive=True):
                    try:
                        rss = child.memory_info().rss
                        used += rss
                        processes.append((child.pid, child.name(), rss))
                    except psutil.NoSuchProcess:
                        pass
                if used > peak_rss:
                    peak_rss = used
                    peak_processes = processes
            except psutil.NoSuchProcess:
                pass
            time.sleep(0.025)
        stdout, stderr = completed.result()
    assert process.returncode == 0, stderr
    # The real CLI previously exceeded 1 GiB before its first tool call.
    (output / "memory.json").write_text(
        json.dumps({"peak_rss": peak_rss, "processes": peak_processes}),
    )
    assert 0 < peak_rss < 1024**3, (
        f"Task process tree peaked at {peak_rss} bytes: {peak_processes}"
    )
    record = json.loads(stdout)
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

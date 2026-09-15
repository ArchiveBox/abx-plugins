import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import time

from abx_plugins.plugins.search_backend_sonic.daemon import (
    get_sonic_supervisord_worker,
    is_port_listening,
)
from abx_plugins.plugins.search_backend_sonic.search import search


def test_real_sonic_indexes_files_and_cli_id_not_reflection_context(tmp_path: Path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = {
        "DATA_DIR": str(tmp_path),
        "SNAP_DIR": str(tmp_path),
        "SEARCH_BACKEND_SONIC_ENABLED": "true",
        "SEARCH_BACKEND_SONIC_HOST_NAME": "127.0.0.1",
        "SEARCH_BACKEND_SONIC_PORT": str(port),
        "SEARCH_BACKEND_SONIC_PASSWORD": "test-input-contract",
        "SEARCH_BACKEND_SONIC_COLLECTION": "archivebox",
        "SEARCH_BACKEND_SONIC_BUCKET": "snapshots",
    }
    worker = get_sonic_supervisord_worker(config)
    assert worker is not None
    title = (
        "archived " * 20000 + "metatitleuniqueneedle $(touch injected) `touch injected`"
    )
    manifest = (
        json.dumps(
            {
                "type": "Snapshot",
                "id": "explicit-sonic-id",
                "url": "https://example.com",
                "title": title,
                "tags": "metataguniqueneedle",
            },
        )
        + "\n"
    )
    (tmp_path / "index.jsonl").write_text(manifest)
    env = {
        **os.environ,
        **config,
        "EXTRA_CONTEXT": json.dumps(
            {
                "snapshot_id": "reflection-only",
                "snapshot_title": "forbiddencontextneedle",
                "snapshot_tags": "forbiddencontexttag",
                "trace_id": ["keep", 3],
            },
        ),
    }
    hook = Path(__file__).parents[1] / "on_Snapshot__91_index_sonic.py"
    with (tmp_path / "sonic-test.log").open("w") as log:
        daemon = subprocess.Popen(
            shlex.split(worker["command"]),
            cwd=worker["directory"],
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 10
            while not is_port_listening("127.0.0.1", port):
                assert daemon.poll() is None, (tmp_path / "sonic-test.log").read_text()
                assert time.monotonic() < deadline, "Sonic did not become ready"
                time.sleep(0.05)
            result = subprocess.run(
                [
                    str(hook),
                    "--url=https://example.com",
                    "--snapshot-id=explicit-sonic-id",
                ],
                cwd=tmp_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
            record = json.loads(result.stdout.splitlines()[-1])
            assert record["status"] == "succeeded"
            assert record["snapshot_id"] == "reflection-only"
            assert record["trace_id"] == ["keep", 3]
            assert search("metatitleuniqueneedle", environ=env) == ["explicit-sonic-id"]
            assert search("metataguniqueneedle", environ=env) == ["explicit-sonic-id"]
            assert search("forbiddencontextneedle", environ=env) == []
            assert search("forbiddencontexttag", environ=env) == []
            assert (tmp_path / "index.jsonl").read_text() == manifest
            assert not list(tmp_path.rglob("injected"))
        finally:
            daemon.terminate()
            daemon.wait(timeout=10)

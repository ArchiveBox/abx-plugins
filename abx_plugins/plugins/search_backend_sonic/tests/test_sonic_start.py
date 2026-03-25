import json
import os
import socket
import subprocess
from pathlib import Path


HOOK = Path(__file__).parent.parent / "on_CrawlSetup__55_sonic_start.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_hook(tmp_path: Path, **env_overrides: str) -> subprocess.CompletedProcess[str]:
    crawl_dir = tmp_path / "crawl"
    crawl_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "ABX_RUNTIME": "archivebox",
            "CRAWL_DIR": str(crawl_dir),
            "DATA_DIR": str(tmp_path / "data"),
            "SEARCH_BACKEND_ENGINE": "sonic",
            "USE_INDEXING_BACKEND": "true",
            "SONIC_BINARY": "/usr/bin/sonic",
            "SEARCH_BACKEND_SONIC_HOST_NAME": "127.0.0.1",
            "SEARCH_BACKEND_SONIC_PORT": str(_free_port()),
        },
    )
    env.update(env_overrides)
    return subprocess.run(
        [str(HOOK), "--url=https://example.com"],
        cwd=str(crawl_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_sonic_start_emits_worker_request_and_host_port_summary(tmp_path: Path) -> None:
    result = _run_hook(tmp_path)

    assert result.returncode == 0, result.stderr
    stdout_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert len(stdout_lines) == 2, stdout_lines

    record = json.loads(stdout_lines[0])
    assert record["type"] == "ProcessEvent"
    assert record["hook_name"] == "worker_sonic"
    assert record["process_type"] == "worker"
    assert record["worker_type"] == "sonic"
    assert record["url"].startswith("tcp://127.0.0.1:")
    assert stdout_lines[1] == record["url"].removeprefix("tcp://")


def test_sonic_start_skips_outside_archivebox(tmp_path: Path) -> None:
    result = _run_hook(tmp_path, ABX_RUNTIME="abx-dl")

    assert result.returncode == 10
    assert result.stdout.strip() == "ABX_RUNTIME=abx-dl"


def test_sonic_start_skips_when_backend_not_selected(tmp_path: Path) -> None:
    result = _run_hook(tmp_path, SEARCH_BACKEND_ENGINE="sqlite")

    assert result.returncode == 10
    assert result.stdout.strip() == "SEARCH_BACKEND_ENGINE=sqlite"


def test_sonic_start_skips_when_indexing_disabled(tmp_path: Path) -> None:
    result = _run_hook(tmp_path, USE_INDEXING_BACKEND="false")

    assert result.returncode == 10
    assert result.stdout.strip() == "USE_INDEXING_BACKEND=False"

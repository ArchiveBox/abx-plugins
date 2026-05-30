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


def test_sonic_start_emits_daemon_start_event_and_host_port_summary(
    tmp_path: Path,
) -> None:
    result = _run_hook(tmp_path)

    assert result.returncode == 0, result.stderr
    stdout_lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert len(stdout_lines) == 2, stdout_lines

    record = json.loads(stdout_lines[0])
    assert record["type"] == "SonicDaemonStartEvent"
    assert record["worker_name"] == "worker_sonic"
    assert record["url"].startswith("tcp://127.0.0.1:")
    assert record["config_path"].endswith("/sonic/config.cfg")
    assert record["output_dir"].endswith("/sonic")
    assert stdout_lines[1] == record["url"].removeprefix("tcp://")


def test_sonic_supervisord_worker_is_owned_by_plugin(tmp_path: Path) -> None:
    from abx_plugins.plugins.search_backend_sonic.daemon import (
        get_sonic_supervisord_worker,
    )

    config = {
        "DATA_DIR": str(tmp_path / "data"),
        "SEARCH_BACKEND_ENGINE": "sonic",
        "SONIC_BINARY": "/usr/bin/sonic",
        "SEARCH_BACKEND_SONIC_HOST_NAME": "127.0.0.1",
        "SEARCH_BACKEND_SONIC_PORT": _free_port(),
        "SEARCH_BACKEND_SONIC_PASSWORD": "SecretPassword",
    }

    worker = get_sonic_supervisord_worker(config)

    assert worker is not None
    assert worker["name"] == "worker_sonic"
    assert worker["command"].startswith("/usr/bin/sonic -c ")
    assert worker["command"].endswith(
        f"-c {tmp_path / 'data' / 'sonic' / 'config.cfg'}",
    )
    assert worker["directory"] == str(tmp_path / "data" / "sonic")
    assert f'SONIC_DIR="{tmp_path / "data" / "sonic"}"' in worker["environment"]
    assert f'DATA_DIR="{tmp_path / "data"}"' in worker["environment"]
    assert worker["autorestart"] == "true"
    assert (tmp_path / "data" / "sonic" / "config.cfg").exists()
    assert (
        f'path = "{tmp_path / "data" / "sonic" / "store" / "kv"}/"'
        in (tmp_path / "data" / "sonic" / "config.cfg").read_text()
    )


def test_sonic_daemon_config_normalizes_localhost_bind_host(tmp_path: Path) -> None:
    from abx_plugins.plugins.search_backend_sonic.daemon import (
        prepare_sonic_daemon,
    )

    port = _free_port()
    config = {
        "DATA_DIR": str(tmp_path / "data"),
        "SEARCH_BACKEND_ENGINE": "sonic",
        "SEARCH_BACKEND_SONIC_HOST_NAME": "localhost",
        "SEARCH_BACKEND_SONIC_PORT": port,
        "SEARCH_BACKEND_SONIC_PASSWORD": "SecretPassword",
    }

    daemon_event = prepare_sonic_daemon(config)

    assert daemon_event.url == f"tcp://localhost:{port}"
    assert (
        f'inet = "127.0.0.1:{port}"'
        in Path(
            daemon_event.config_path,
        ).read_text()
    )


def test_sonic_start_skips_outside_archivebox(tmp_path: Path) -> None:
    result = _run_hook(tmp_path, ABX_RUNTIME="abx-dl")

    assert result.returncode == 10
    assert result.stdout.strip() == "ABX_RUNTIME=abx-dl"


def test_sonic_start_skips_when_disabled_and_engine_not_sonic(tmp_path: Path) -> None:
    result = _run_hook(
        tmp_path,
        SEARCH_BACKEND_ENGINE="ripgrep",
        SEARCH_BACKEND_SONIC_ENABLED="false",
    )

    assert result.returncode == 10
    assert result.stdout.strip() == "SEARCH_BACKEND_SONIC_ENABLED=False"


def test_sonic_start_still_runs_when_engine_selects_other_backend(
    tmp_path: Path,
) -> None:
    """Default SEARCH_BACKEND_SONIC_ENABLED=true keeps sonic indexing even if engine=sqlite."""
    result = _run_hook(tmp_path, SEARCH_BACKEND_ENGINE="sqlite")

    assert result.returncode == 0, result.stderr
    assert "127.0.0.1:" in result.stdout


def test_sonic_start_runs_when_engine_is_sonic_even_if_enabled_false(
    tmp_path: Path,
) -> None:
    """Engine=sonic forces sonic to run regardless of SEARCH_BACKEND_SONIC_ENABLED."""
    result = _run_hook(
        tmp_path,
        SEARCH_BACKEND_ENGINE="sonic",
        SEARCH_BACKEND_SONIC_ENABLED="false",
    )

    assert result.returncode == 0, result.stderr
    assert "127.0.0.1:" in result.stdout

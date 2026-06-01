import socket
from pathlib import Path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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

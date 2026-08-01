import json
import socket
import shlex
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


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
        "SEARCH_BACKEND_SONIC_ENABLED": True,
        "SONIC_BINARY": "sonic",
        "SEARCH_BACKEND_SONIC_HOST_NAME": "127.0.0.1",
        "SEARCH_BACKEND_SONIC_PORT": _free_port(),
        "SEARCH_BACKEND_SONIC_PASSWORD": "SecretPassword",
    }

    worker = get_sonic_supervisord_worker(config)

    assert worker is not None
    command = shlex.split(worker["command"])
    sonic_binary = Path(command[0])
    assert sonic_binary.is_absolute()
    assert sonic_binary.is_file()
    assert worker["name"] == "worker_sonic"
    assert command == [
        str(sonic_binary),
        "-c",
        str(tmp_path / "data" / "sonic" / "config.cfg"),
    ]
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
        "SEARCH_BACKEND_SONIC_ENABLED": True,
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


def test_sonic_required_binary_avoids_build_chain_on_linux_x86_64() -> None:
    config = json.loads((PLUGIN_DIR / "config.json").read_text(encoding="utf-8"))

    [sonic_binary] = config["required_binaries"]
    provider_names = sonic_binary["binproviders"].split(",")
    bash_override = sonic_binary["overrides"]["bash"]
    apt_override = sonic_binary["overrides"]["apt"]

    assert config["properties"]["SEARCH_BACKEND_SONIC_ENABLED"]["default"] is True
    assert provider_names == ["env", "brew", "apt", "bash", "cargo"]
    assert provider_names.index("apt") < provider_names.index("bash")
    assert provider_names.index("apt") < provider_names.index("cargo")
    assert bash_override["install_args"] == ["sonic@1.7.4"]
    assert (
        "81b1d017992ffc9957dc27f7f6c78fd2cf1a4e09c89295dc8b15d15ded01b8ae"
        in bash_override["install"]
    )
    assert apt_override["install_args"] == ["sonic"]
    assert apt_override["apt_gpg_keys"] == {
        "https://packagecloud.io/valeriansaliou/sonic/gpgkey": "valeriansaliou_sonic.asc",
    }
    assert apt_override["apt_sources"] == {
        "valeriansaliou_sonic.list": "deb [signed-by=/etc/apt/keyrings/valeriansaliou_sonic.asc] https://packagecloud.io/valeriansaliou/sonic/debian/ bookworm main",
    }
    assert apt_override["apt_system_groups"] == {"sonic": {}}
    assert apt_override["apt_system_users"] == {
        "sonic": {
            "gid": "sonic",
            "home": "/var/lib/sonic",
            "shell": "/usr/sbin/nologin",
            "create_home": False,
        },
    }
    assert sonic_binary["overrides"]["brew"]["install_args"] == ["sonic"]
    assert sonic_binary["overrides"]["cargo"]["install_args"] == [
        "sonic-server",
        "--version",
        "1.7.4",
    ]

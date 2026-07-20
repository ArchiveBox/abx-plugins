from __future__ import annotations

import json
import os
import shlex
import socket
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, TypedDict
from collections.abc import Mapping

from abx_plugins.plugins.base.utils import (
    load_required_binary_from_config,
    write_text_atomic,
)


SONIC_WORKER_NAME = "worker_sonic"
SONIC_DAEMON_EVENT_TYPE = "SonicDaemonStartEvent"
CONFIG_PATH = Path(__file__).with_name("config.json")


class SonicSupervisorWorker(TypedDict):
    name: str
    command: str
    directory: str
    environment: str
    autostart: str
    autorestart: str
    stopasgroup: str
    killasgroup: str
    stopwaitsecs: str
    stdout_logfile: str
    redirect_stderr: str


class SonicDaemonStartRecord(TypedDict):
    type: Literal["SonicDaemonStartEvent"]
    worker_name: str
    url: str
    host: str
    port: int
    config_path: str
    output_dir: str


@dataclass(frozen=True)
class SonicDaemonStartEvent:
    worker_name: str
    url: str
    host: str
    port: int
    config_path: str
    output_dir: str

    def to_record(self) -> SonicDaemonStartRecord:
        return {
            "type": "SonicDaemonStartEvent",
            "worker_name": self.worker_name,
            "url": self.url,
            "host": self.host,
            "port": self.port,
            "config_path": self.config_path,
            "output_dir": self.output_dir,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_record())

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> SonicDaemonStartEvent:
        if record.get("type") != SONIC_DAEMON_EVENT_TYPE:
            raise ValueError(
                f"Expected {SONIC_DAEMON_EVENT_TYPE}, got {record.get('type')}",
            )
        return cls(
            worker_name=str(record["worker_name"]),
            url=str(record["url"]),
            host=str(record["host"]),
            port=int(record["port"]),
            config_path=str(record["config_path"]),
            output_dir=str(record["output_dir"]),
        )


def config_value(config: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    if isinstance(config, Mapping):
        return config.get(key, default)
    return getattr(config, key, default)


@lru_cache(maxsize=1)
def _sonic_config_properties() -> dict[str, Any]:
    data = json.loads(CONFIG_PATH.read_text())
    properties = data.get("properties") if isinstance(data, dict) else {}
    return dict(properties) if isinstance(properties, dict) else {}


def _coerce_env_value(value: str, prop: Mapping[str, Any]) -> Any:
    prop_type = prop.get("type")
    if prop_type == "boolean":
        return value.strip().lower() not in {"0", "false", "no", "off"}
    if prop_type == "integer":
        try:
            return int(value)
        except ValueError:
            return prop.get("default", 0)
    return value


def load_sonic_config(environ: Mapping[str, str] | None = None) -> Any:
    """Load the Sonic hot-path config without pydantic or binary hydration."""
    env = os.environ if environ is None else environ
    values: dict[str, Any] = {}
    for key, prop in _sonic_config_properties().items():
        aliases = prop.get("x-aliases") if isinstance(prop, Mapping) else []
        env_keys = [key, *(aliases if isinstance(aliases, list) else [])]
        raw_value = next((env[name] for name in env_keys if name in env), None)
        if raw_value is None:
            values[key] = prop.get("default") if isinstance(prop, Mapping) else ""
        else:
            values[key] = _coerce_env_value(raw_value, prop)
    for key in ("ABX_RUNTIME", "CRAWL_DIR", "DATA_DIR", "EXTRA_CONTEXT", "SNAP_DIR"):
        values[key] = env.get(key, "")
    return SimpleNamespace(**values)


def supervisord_environment(**values: Any) -> str:
    return ",".join(
        f"{key}={json.dumps(str(value))}"
        for key, value in values.items()
        if value is not None
    )


def is_sonic_backend_enabled(config: Mapping[str, Any] | Any) -> bool:
    return str(
        config_value(config, "SEARCH_BACKEND_SONIC_ENABLED", True),
    ).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def is_port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5) as sock:
            sock.settimeout(0.5)
            reader = sock.makefile("r", encoding="utf-8")
            writer = sock.makefile("w", encoding="utf-8")
            if not reader.readline().startswith("CONNECTED"):
                return False
            writer.write("QUIT\n")
            writer.flush()
            try:
                reader.readline()
            except OSError:
                pass
            return True
    except OSError:
        return False


def get_sonic_dir(
    config: Mapping[str, Any] | Any,
    *,
    crawl_dir: Path | None = None,
) -> Path:
    configured_dir = config_value(config, "SONIC_DIR")
    if configured_dir:
        return Path(str(configured_dir)).expanduser().resolve()
    data_dir = config_value(config, "DATA_DIR")
    if data_dir:
        return (Path(str(data_dir)).expanduser().resolve() / "sonic").resolve()
    if crawl_dir is not None:
        return (crawl_dir / "sonic").resolve()
    return Path("sonic").resolve()


def sonic_daemon_bind_host(config: Mapping[str, Any] | Any) -> str:
    host = str(config_value(config, "SEARCH_BACKEND_SONIC_HOST_NAME") or "127.0.0.1")
    if host.strip().lower() == "localhost":
        return "127.0.0.1"
    return host


def build_config_text(config: Mapping[str, Any] | Any, sonic_dir: Path) -> str:
    kv_dir = (sonic_dir / "store" / "kv").resolve()
    fst_dir = (sonic_dir / "store" / "fst").resolve()
    daemon_host = sonic_daemon_bind_host(config)
    return "\n".join(
        [
            "[server]",
            'log_level = "error"',
            "",
            "[channel]",
            f'inet = "{daemon_host}:{config_value(config, "SEARCH_BACKEND_SONIC_PORT")}"',
            "tcp_timeout = 300",
            f'auth_password = "{config_value(config, "SEARCH_BACKEND_SONIC_PASSWORD")}"',
            "",
            "[channel.search]",
            "query_limit_default = 10",
            "query_limit_maximum = 100",
            "query_alternates_try = 4",
            "suggest_limit_default = 5",
            "suggest_limit_maximum = 20",
            "list_limit_default = 100",
            "list_limit_maximum = 500",
            "",
            "[store]",
            "",
            "[store.kv]",
            f'path = "{kv_dir.as_posix()}/"',
            "retain_word_objects = 1000",
            "",
            "[store.kv.pool]",
            "inactive_after = 1800",
            "",
            "[store.kv.database]",
            "flush_after = 900",
            "compress = true",
            "parallelism = 2",
            "max_files = 100",
            "max_compactions = 1",
            "max_flushes = 1",
            "write_buffer = 16384",
            "write_ahead_log = true",
            "",
            "[store.fst]",
            f'path = "{fst_dir.as_posix()}/"',
            "",
            "[store.fst.pool]",
            "inactive_after = 300",
            "",
            "[store.fst.graph]",
            "consolidate_after = 180",
            "max_size = 2048",
            "max_words = 250000",
            "",
        ],
    )


def prepare_sonic_daemon(
    config: Mapping[str, Any] | Any,
    *,
    crawl_dir: Path | None = None,
) -> SonicDaemonStartEvent:
    sonic_dir = get_sonic_dir(config, crawl_dir=crawl_dir)
    (sonic_dir / "store" / "kv").mkdir(parents=True, exist_ok=True)
    (sonic_dir / "store" / "fst").mkdir(parents=True, exist_ok=True)
    config_path = sonic_dir / "config.cfg"
    write_text_atomic(config_path, build_config_text(config, sonic_dir))

    host = str(config_value(config, "SEARCH_BACKEND_SONIC_HOST_NAME"))
    port = int(config_value(config, "SEARCH_BACKEND_SONIC_PORT"))
    return SonicDaemonStartEvent(
        worker_name=SONIC_WORKER_NAME,
        url=f"tcp://{host}:{port}",
        host=host,
        port=port,
        config_path=str(config_path),
        output_dir=str(sonic_dir),
    )


def get_sonic_supervisord_worker(
    config: Mapping[str, Any] | Any,
) -> SonicSupervisorWorker | None:
    if not is_sonic_backend_enabled(config):
        return None

    daemon_event = prepare_sonic_daemon(config)
    config_payload = dict(config) if isinstance(config, Mapping) else vars(config)
    requested_binary = str(config_value(config, "SONIC_BINARY", "sonic"))
    loaded_binary = load_required_binary_from_config(
        requested_binary,
        CONFIG_PATH,
        global_config=config_payload,
        install=True,
    )
    if not loaded_binary.loaded_abspath:
        raise RuntimeError("abxpkg did not resolve SONIC_BINARY")
    sonic_binary = str(loaded_binary.loaded_abspath)
    data_dir = config_value(config, "DATA_DIR")
    return {
        "name": daemon_event.worker_name,
        "command": shlex.join([sonic_binary, "-c", daemon_event.config_path]),
        "directory": daemon_event.output_dir,
        "environment": supervisord_environment(
            SONIC_DIR=daemon_event.output_dir,
            DATA_DIR=data_dir,
        ),
        "autostart": "false",
        "autorestart": "true",
        "stopasgroup": "true",
        "killasgroup": "true",
        "stopwaitsecs": "10",
        "stdout_logfile": "logs/worker_sonic.log",
        "redirect_stderr": "true",
    }

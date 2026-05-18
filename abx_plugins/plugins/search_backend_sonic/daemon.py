from __future__ import annotations

import json
import shlex
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict
from collections.abc import Mapping

from abx_plugins.plugins.base.utils import write_text_atomic


SONIC_WORKER_NAME = "worker_sonic"
SONIC_DAEMON_EVENT_TYPE = "SonicDaemonStartEvent"


class SonicSupervisorWorker(TypedDict):
    name: str
    command: str
    directory: str
    environment: str
    autostart: str
    autorestart: str
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


def supervisord_environment(**values: Any) -> str:
    return ",".join(
        f"{key}={json.dumps(str(value))}"
        for key, value in values.items()
        if value is not None
    )


def is_sonic_backend_enabled(config: Mapping[str, Any] | Any) -> bool:
    return config_value(config, "SEARCH_BACKEND_ENGINE") == "sonic" and bool(
        config_value(config, "USE_INDEXING_BACKEND", True),
    )


def is_port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
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


def build_config_text(config: Mapping[str, Any] | Any, sonic_dir: Path) -> str:
    kv_dir = (sonic_dir / "store" / "kv").resolve()
    fst_dir = (sonic_dir / "store" / "fst").resolve()
    return "\n".join(
        [
            "[server]",
            'log_level = "error"',
            "",
            "[channel]",
            f'inet = "{config_value(config, "SEARCH_BACKEND_SONIC_HOST_NAME")}:{config_value(config, "SEARCH_BACKEND_SONIC_PORT")}"',
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
    sonic_binary = str(config_value(config, "SONIC_BINARY", "sonic"))
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
        "stdout_logfile": "logs/worker_sonic.log",
        "redirect_stderr": "true",
    }

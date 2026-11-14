"""Optional ArchiveBox persona browser service."""

import os
import json
import sys
from pathlib import Path
import shlex
from functools import partial


def prepare_browser_worker(persona, host):
    binding = host.browser_binding(persona_name=persona, create=True)
    config = dict(binding["config"])
    config["AUTH_STORAGE_FILE"] = config.get("AUTH_STORAGE_FILE") or ""
    config["COOKIES_FILE"] = config.get("COOKIES_FILE") or ""
    environment = {
        key: value if isinstance(value, str) else json.dumps(value)
        for key, value in config.items()
        if value is not None
    }
    command = [
        sys.executable,
        str(Path(__file__).with_name("browser.py")),
        "open",
        "--persona-dir",
        binding["persona_dir"],
        "--persona-name",
        binding["persona_name"],
    ]
    return {
        "name": "worker_vnc_browser",
        "command": shlex.join(command),
        "environment": ",".join(
            f"{key}={json.dumps(value)}" for key, value in environment.items()
        ),
        "autostart": "false",
        "autorestart": "false",
        "stopasgroup": "true",
        "killasgroup": "true",
        "stopwaitsecs": "30",
        "stdout_logfile": "logs/worker_vnc_browser.log",
        "redirect_stderr": "true",
    }


def get_services(host, config):
    values = config if isinstance(config, dict) else vars(config)
    if not os.environ.get("DISPLAY") or not (
        values.get("IN_DOCKER") or os.environ.get("ARCHIVEBOX_VNC_PERSONA")
    ):
        return ()
    persona = os.environ.get("ARCHIVEBOX_VNC_PERSONA") or values.get(
        "DEFAULT_PERSONA",
        "Default",
    )
    return (
        {
            "name": "worker_vnc_browser",
            "label": "browser",
            "contexts": ("server",),
            "prepare": partial(prepare_browser_worker, persona, host),
        },
    )

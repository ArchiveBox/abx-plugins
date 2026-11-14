"""Prepared Stagehand module environment for browser clients."""

import os
import subprocess
from pathlib import Path


def browser_environment(binary):
    module = subprocess.run(
        [
            os.environ.get("NODE_BINARY") or "node",
            str(Path(__file__).with_name("prepare.js")),
            str(binary.loaded_abspath),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {"ARCHIVEBOX_STAGEHAND_MODULE": module}


def agent_environment(endpoint, binaries):
    """Connection settings for the registered Stagehand agent tool."""
    return {**browser_environment(binaries["stagehand"]), "BU_CDP_WS": endpoint}

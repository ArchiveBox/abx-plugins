"""Agent setup and gallery entry imported by ArchiveBox's screenshot command."""

import socket
import subprocess


def configure() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    subprocess.run(
        ["archivebox", "config", "--set", f"OPENCODE_PORT={port}"],
        check=True,
    )


def gallery_entry(admin_base_url: str) -> str:
    return (
        f"AI agent|{admin_base_url}/admin/agent/|/admin/agent/|"
        "https://github.com/ArchiveBox/abx-plugins/blob/main/"
        "abx_plugins/plugins/opencode/archivebox/views.py|"
        "wait-text:ArchiveBox AI Agent"
    )

"""OpenCode-specific preparation and verification for preinstalled images."""

import os
import shutil
import subprocess
from pathlib import Path


def installed_binary() -> Path:
    return (
        Path(os.environ["ABXPKG_LIB_DIR"])
        / "pnpm/packages/opencode/node_modules/.bin/opencode"
    )


def prune_incompatible_image_files() -> None:
    """Remove pnpm's unused musl variants from the Debian/glibc image."""
    modules = installed_binary().parent.parent
    for path in modules.rglob("opencode-linux-*-musl"):
        if path.is_symlink():
            path.unlink()
    for path in (modules / ".pnpm").glob("opencode-linux-*-musl@*"):
        if path.is_dir():
            shutil.rmtree(path)


def verify_installed() -> None:
    """Check the shipped executable before an installer can repair it."""
    subprocess.run([str(installed_binary()), "--version"], check=True)


def verify_archivebox_install() -> None:
    verify_installed()
    subprocess.run(["archivebox", "install", "opencode"], check=True)
    verify_installed()

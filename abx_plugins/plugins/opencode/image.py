"""OpenCode-specific preparation and verification for preinstalled images."""

import os
import filecmp
import shutil
import subprocess
from pathlib import Path


def installed_binary() -> Path:
    return (
        Path(os.environ["ABXPKG_LIB_DIR"])
        / "pnpm/packages/opencode/node_modules/.bin/opencode"
    )


def prune_incompatible_image_files() -> None:
    """Remove unused musl variants and duplicate native payloads from images."""
    modules = installed_binary().parent.parent
    for path in modules.rglob("opencode-linux-*-musl"):
        if path.is_symlink():
            path.unlink()
    for path in (modules / ".pnpm").glob("opencode-linux-*-musl@*"):
        if path.is_dir():
            shutil.rmtree(path)
    # The upstream postinstall copies its selected executable when linking
    # across the package-store mount fails. Keep both installed paths, but make
    # their image size independent of that mount's filesystem/hardlink layout.
    for launcher in modules.glob(
        ".pnpm/opencode-ai@*/node_modules/opencode-ai/bin/opencode.exe",
    ):
        for native in modules.glob(
            ".pnpm/opencode-*/node_modules/opencode-*/bin/opencode",
        ):
            if not native.samefile(launcher) and filecmp.cmp(
                native,
                launcher,
                shallow=False,
            ):
                native.unlink()
                native.hardlink_to(launcher)


def verify_installed() -> None:
    """Check shipped agent tools before an installer can repair them."""
    subprocess.run([str(installed_binary()), "--version"], check=True)
    subprocess.run(["jq", "-en", '{"installed": true} | .installed'], check=True)
    subprocess.run(["file", "--version"], check=True)


def verify_archivebox_install() -> None:
    verify_installed()
    subprocess.run(["archivebox", "install", "opencode"], check=True)
    verify_installed()

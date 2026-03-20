#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "rich-click",
#   "abx-pkg",
# ]
# ///
#
# Install a binary using npm package manager and configure PATH and NODE_MODULES_DIR environment variables.
#
# Usage:
#     ./on_Binary__10_npm_install.py --machine-id=<uuid> --binary-id=<uuid> --name=<name> [...] > events.jsonl

import json
import os
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))
from base.utils import emit_binary_record, emit_machine_record, enforce_lib_permissions

import rich_click as click
from abx_pkg import Binary, EnvProvider, NpmProvider


def _resolve_node_modules_dir(binary_abspath: str | Path, npm_prefix: Path) -> Path:
    """Infer the node_modules directory that actually owns the resolved binary."""
    binary_path = Path(binary_abspath)

    # Typical npm CLI binaries live in <prefix>/node_modules/.bin/<name>.
    if binary_path.parent.name == ".bin" and binary_path.parent.parent.name == "node_modules":
        return binary_path.parent.parent

    return npm_prefix / "node_modules"


@click.command()
@click.option("--machine-id", required=True, help="Machine UUID")
@click.option("--binary-id", required=True, help="Dependency UUID")
@click.option("--plugin-name", required=True, help="Requesting plugin name")
@click.option("--hook-name", required=True, help="Requesting hook name")
@click.option("--name", required=True, help="Binary name to install")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--min-version", default="", help="Minimum acceptable version")
@click.option("--custom-cmd", default=None, help="Custom install command")
@click.option("--overrides", default=None, help="JSON-encoded overrides dict")
def main(
    binary_id: str,
    machine_id: str,
    plugin_name: str,
    hook_name: str,
    name: str,
    binproviders: str,
    min_version: str,
    custom_cmd: str | None,
    overrides: str | None,
):
    """Install binary using npm."""

    if binproviders != "*" and "npm" not in binproviders.split(","):
        click.echo(f"npm provider not allowed for {name}", err=True)
        sys.exit(0)

    # Get LIB_DIR from environment (optional)
    lib_dir = os.environ.get("LIB_DIR", "").strip()
    if not lib_dir:
        lib_dir = str(Path.home() / ".config" / "abx" / "lib")

    # Structure: lib/arm64-darwin/npm (npm will create node_modules inside this)
    npm_prefix = Path(lib_dir) / "npm"
    npm_prefix.mkdir(parents=True, exist_ok=True)

    # Use abx-pkg NpmProvider to install binary with custom prefix
    provider = NpmProvider(npm_prefix=npm_prefix)
    if not provider.INSTALLER_BIN:
        click.echo("npm not available on this system", err=True)
        sys.exit(1)

    click.echo(f"Installing {name} via npm to {npm_prefix}...", err=True)

    try:
        # Parse overrides if provided
        overrides_dict = None
        if overrides:
            try:
                overrides_dict = json.loads(overrides)
                click.echo(
                    f"Using custom install overrides: {overrides_dict}", err=True
                )
            except json.JSONDecodeError:
                click.echo(
                    f"Warning: Failed to parse overrides JSON: {overrides}", err=True
                )

        binary = Binary(
            name=name,
            min_version=min_version or None,
            binproviders=[EnvProvider(), provider],
            overrides=overrides_dict or {},
        ).load_or_install()
    except Exception as e:
        click.echo(f"npm install failed: {e}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found after npm install", err=True)
        sys.exit(1)

    machine_id = machine_id.strip() or os.environ.get("MACHINE_ID", "").strip()

    # Output Binary JSONL record to stdout
    emit_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider="npm",
        machine_id=machine_id,
        binary_id=binary_id,
        plugin_name=plugin_name,
        hook_name=hook_name,
    )

    # Emit PATH update for npm bin dirs (node_modules/.bin preferred)
    npm_bin_dirs = [
        str(npm_prefix / "node_modules" / ".bin"),
        str(npm_prefix / "bin"),
    ]
    current_path = os.environ.get("PATH", "")
    path_dirs = current_path.split(":") if current_path else []
    new_path = current_path

    for npm_bin_dir in npm_bin_dirs:
        if npm_bin_dir and npm_bin_dir not in path_dirs:
            new_path = f"{npm_bin_dir}:{new_path}" if new_path else npm_bin_dir
            path_dirs.insert(0, npm_bin_dir)

    emit_machine_record(
        {
            "PATH": new_path,
        }
    )

    # Emit JS module resolution env vars for downstream node-based hooks.
    node_modules_dir = str(_resolve_node_modules_dir(binary.abspath, npm_prefix))
    emit_machine_record(
        {
            "NODE_MODULES_DIR": node_modules_dir,
            "NODE_MODULE_DIR": node_modules_dir,
            "NODE_PATH": node_modules_dir,
        }
    )

    # Log human-readable info to stderr
    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    # Lock down lib/ so snapshot hooks can read/execute but not write
    enforce_lib_permissions()

    sys.exit(0)


if __name__ == "__main__":
    main()

#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "rich-click",
#   "abx-pkg",
#   "abx-plugins",
# ]
# ///
#
# Install a binary using a custom shell command defined in overrides.
# This provider runs arbitrary shell commands to install binaries that don't fit
# into standard package managers, outputting a Binary JSONL record to stdout.
#
# Usage:
#     ./on_BinaryRequest__14_custom.py [...] > events.jsonl

import subprocess
import sys

from abx_plugins.plugins.base.utils import emit_installed_binary_record

import rich_click as click
from pydantic import ConfigDict, TypeAdapter

from abx_pkg import Binary, BinaryOverrides, EnvProvider


OverridesDict = TypeAdapter(
    BinaryOverrides,
    config=ConfigDict(arbitrary_types_allowed=True),
)


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--name", required=True, help="Binary name to install")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--min-version", default="", help="Minimum acceptable version")
@click.option("--overrides", required=True, help="JSON-encoded overrides dict")
def main(
    name: str,
    binproviders: str,
    min_version: str,
    overrides: str,
):
    """Install binary using custom bash command."""

    if binproviders != "*" and "custom" not in binproviders.split(","):
        click.echo(f"custom provider not allowed for {name}", err=True)
        sys.exit(0)

    custom_overrides = OverridesDict.validate_json(overrides)["custom"]
    if "install" not in custom_overrides:
        click.echo("Custom provider requires overrides.custom.install", err=True)
        sys.exit(1)
    install_command = str(custom_overrides["install"])

    click.echo(f"Installing {name} via custom command: {install_command}", err=True)

    try:
        result = subprocess.run(
            install_command,
            shell=True,
            timeout=600,  # 10 minute timeout for custom installs
        )
        if result.returncode != 0:
            click.echo(f"Custom install failed (exit={result.returncode})", err=True)
            sys.exit(1)
    except subprocess.TimeoutExpired:
        click.echo("Custom install timed out", err=True)
        sys.exit(1)

    # Use abx-pkg to load the binary and get its info
    provider = EnvProvider()
    try:
        binary = Binary(name=name, binproviders=[provider]).load()
    except Exception:
        try:
            binary = Binary(
                name=name,
                binproviders=[provider],
                overrides={"env": {"version": "0.0.1"}},
            ).load()
        except Exception as e:
            click.echo(f"{name} not found after custom install: {e}", err=True)
            sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found after custom install", err=True)
        sys.exit(1)

    # Output Binary JSONL record to stdout
    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider="custom",
    )

    # Log human-readable info to stderr
    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    sys.exit(0)


if __name__ == "__main__":
    main()

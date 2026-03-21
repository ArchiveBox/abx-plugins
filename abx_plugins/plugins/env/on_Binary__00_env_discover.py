#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "rich-click",
#   "abx-pkg",
#   "abx-plugins",
# ]
# [tool.uv.sources]
# abx-plugins = { path = "../../..", editable = true }
# ///
#
# Check if a binary is available in the system PATH and output its Binary record.
# This simple provider discovers binaries that are already installed without installing anything.
#
# Usage:
#     ./on_Binary__00_env_discover.py --binary-id=<uuid> --machine-id=<uuid> --name=<name> > events.jsonl

import os
import sys

from abx_plugins.plugins.base.utils import emit_binary_record

import rich_click as click
from abx_pkg import Binary, EnvProvider, SemVer


@click.command()
@click.option("--machine-id", required=True, help="Machine UUID")
@click.option("--binary-id", required=True, help="Dependency UUID")
@click.option("--plugin-name", required=True, help="Requesting plugin name")
@click.option("--hook-name", required=True, help="Requesting hook name")
@click.option("--name", required=True, help="Binary name to find")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--min-version", default="", help="Minimum acceptable version")
@click.option("--overrides", default=None, help="JSON-encoded overrides dict (unused)")
def main(
    binary_id: str,
    machine_id: str,
    plugin_name: str,
    hook_name: str,
    name: str,
    binproviders: str,
    min_version: str,
    overrides: str | None,
):
    """Check if binary is available in PATH and record it."""

    # Check if env provider is allowed
    if binproviders != "*" and "env" not in binproviders.split(","):
        click.echo(f"env provider not allowed for {name}", err=True)
        sys.exit(0)  # Not an error, just skip

    # Use abx-pkg EnvProvider to find binary
    provider = EnvProvider()
    try:
        binary = Binary(
            name=name,
            min_version=SemVer(min_version) if min_version else None,
            binproviders=[provider],
        ).load()
    except Exception as e:
        click.echo(f"{name} not found in PATH: {e}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found in PATH", err=True)
        sys.exit(1)

    machine_id = machine_id.strip() or os.environ.get("MACHINE_ID", "").strip()

    # Output Binary JSONL record to stdout
    emit_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider="env",
        machine_id=machine_id,
        binary_id=binary_id,
        plugin_name=plugin_name,
        hook_name=hook_name,
    )

    # Log human-readable info to stderr
    click.echo(f"Found {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    sys.exit(0)


if __name__ == "__main__":
    main()

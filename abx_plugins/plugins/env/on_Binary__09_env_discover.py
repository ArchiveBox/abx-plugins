#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "rich-click",
#   "abx-pkg",
# ]
# ///
#
# Check if a binary is available in the system PATH and output its Binary record.
# This simple provider discovers binaries that are already installed without installing anything.
#
# Usage:
#     ./on_Binary__09_env_discover.py --binary-id=<uuid> --machine-id=<uuid> --name=<name> > events.jsonl

import json
import os
import sys

import rich_click as click
from abx_pkg import Binary, EnvProvider


@click.command()
@click.option("--machine-id", required=True, help="Machine UUID")
@click.option("--binary-id", required=True, help="Dependency UUID")
@click.option("--name", required=True, help="Binary name to find")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--overrides", default=None, help="JSON-encoded overrides dict (unused)")
def main(
    binary_id: str, machine_id: str, name: str, binproviders: str, overrides: str | None
):
    """Check if binary is available in PATH and record it."""

    # Check if env provider is allowed
    if binproviders != "*" and "env" not in binproviders.split(","):
        click.echo(f"env provider not allowed for {name}", err=True)
        sys.exit(0)  # Not an error, just skip

    # Use abx-pkg EnvProvider to find binary
    provider = EnvProvider()
    try:
        binary = Binary(name=name, binproviders=[provider]).load()
    except Exception as e:
        click.echo(f"{name} not found in PATH: {e}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found in PATH", err=True)
        sys.exit(1)

    machine_id = machine_id.strip() or os.environ.get("MACHINE_ID", "").strip()

    # Output Binary JSONL record to stdout
    record = {
        "type": "Binary",
        "name": name,
        "abspath": str(binary.abspath),
        "version": str(binary.version) if binary.version else "",
        "sha256": binary.sha256 or "",
        "binprovider": "env",
        "machine_id": machine_id,
        "binary_id": binary_id,
    }
    print(json.dumps(record))

    # Log human-readable info to stderr
    click.echo(f"Found {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    sys.exit(0)


if __name__ == "__main__":
    main()

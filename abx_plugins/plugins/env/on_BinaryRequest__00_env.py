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
# Check if a binary is available in the system PATH and output an Binary record.
# This simple provider discovers binaries that are already installed without installing anything.
#
# Usage:
#     ./on_BinaryRequest__00_env.py --name=<name> > events.jsonl

import sys

from abx_plugins.plugins.base.utils import emit_installed_binary_record

import rich_click as click
from abx_pkg import Binary, EnvProvider, SemVer


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--name", required=True, help="Binary name to find")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--min-version", default="", help="Minimum acceptable version")
@click.option("--overrides", default=None, help="JSON-encoded overrides dict (unused)")
def main(
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

    # Output Binary JSONL record to stdout
    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider="env",
    )

    # Log human-readable info to stderr
    click.echo(f"Found {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    sys.exit(0)


if __name__ == "__main__":
    main()

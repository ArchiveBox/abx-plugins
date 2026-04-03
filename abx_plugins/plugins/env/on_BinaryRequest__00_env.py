#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "rich-click",
#   "abx-pkg>=1.9.27",
#   "abx-plugins>=1.10.27",
# ]
# ///
#
# Check if a binary is available in the system PATH and output an Binary record.
# This simple provider discovers binaries that are already installed without installing anything.
#
# Usage:
#     ./on_BinaryRequest__00_env.py --name=<name> [--min-release-age=0] > events.jsonl

import json
import sys

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
    parse_extra_hook_args,
)

import rich_click as click
from abx_pkg import Binary, EnvProvider


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
        context = click.get_current_context(silent=True)
        extra_kwargs = parse_extra_hook_args(context.args if context else [])
        binary = Binary.model_validate(
            {
                **extra_kwargs,
                "name": name,
                "binproviders": [provider],
                "min_version": min_version or extra_kwargs.get("min_version") or None,
                "overrides": json.loads(overrides) if overrides else {},
            },
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

#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pydantic-settings",
#   "jambo",
#   "rich-click",
#   "abxpkg>=1.10.4",
#   "abx-plugins>=1.10.27",
# ]
# ///
#
# Install a binary using apt package manager. Outputs a Binary JSONL record to stdout after installation.
#
# Usage:
#     ./on_BinaryRequest__13_apt.py [...] > events.jsonl

import json
import subprocess
import sys
from collections.abc import Mapping

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
    parse_extra_hook_args,
)

import rich_click as click

from abxpkg import AptProvider, Binary


def _apt_install_args(name: str, provider_overrides: Mapping) -> list[str]:
    install_args = provider_overrides.get("install_args") or []
    if isinstance(install_args, str):
        install_args = [install_args]
    if not isinstance(install_args, list):
        return [name]
    packages = [
        str(arg)
        for arg in install_args
        if str(arg).strip() and not str(arg).startswith("-")
    ]
    return packages or [name]


def _apt_has_candidate(package_name: str) -> bool | None:
    try:
        result = subprocess.run(
            ["apt-cache", "policy", package_name],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Candidate:"):
            return stripped != "Candidate: (none)"
    return None


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--name", required=True, help="Binary name to install")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--min-version", default="", help="Minimum acceptable version")
@click.option("--overrides", default=None, help="JSON-encoded overrides dict")
def main(
    name: str,
    binproviders: str,
    min_version: str,
    overrides: str | None,
):
    """Install binary using apt package manager."""

    # Check if apt provider is allowed
    if binproviders != "*" and "apt" not in binproviders.split(","):
        click.echo(f"apt provider not allowed for {name}", err=True)
        sys.exit(0)  # Not an error, just skip

    # Use abxpkg AptProvider to install binary
    provider = AptProvider()
    try:
        provider.INSTALLER_BINARY()
    except Exception:
        click.echo(
            "AptProvider.INSTALLER_BIN is not available on this host",
            err=True,
        )
        sys.exit(0)

    click.echo(f"Resolving {name} via apt (load or install)...", err=True)

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
        )
        provider_overrides = binary.overrides.get("apt", {})
        if provider_overrides:
            click.echo(
                f"Using apt install overrides: {provider_overrides}",
                err=True,
            )
        for package_name in _apt_install_args(name, provider_overrides):
            has_candidate = _apt_has_candidate(package_name)
            if has_candidate is False:
                click.echo(f"apt package not found: {package_name}", err=True)
                sys.exit(1)

        binary = binary.install()
    except Exception as e:
        click.echo(f"apt install failed: {e}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo(f"{name} not found after apt install", err=True)
        sys.exit(1)

    resolved_provider = binary.loaded_binprovider
    resolved_provider_name = resolved_provider.name if resolved_provider else ""

    # Output Binary JSONL record to stdout
    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider=resolved_provider_name,
        binary=binary,
    )

    # Log human-readable info to stderr
    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    sys.exit(0)


if __name__ == "__main__":
    main()

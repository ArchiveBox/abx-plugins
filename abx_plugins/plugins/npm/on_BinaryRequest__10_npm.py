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
# Install a binary using npm package manager.
#
# Usage:
#     ./on_BinaryRequest__10_npm.py --name=<name> [...] > events.jsonl

import json
import os
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
    enforce_lib_permissions,
    load_config,
    parse_extra_hook_args,
)

import rich_click as click
from abx_pkg import Binary, NpmProvider


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
    """Install binary using npm."""
    config = load_config()

    if binproviders != "*" and "npm" not in binproviders.split(","):
        click.echo(f"npm provider not allowed for {name}", err=True)
        sys.exit(0)

    # Get LIB_DIR from environment (optional)
    lib_dir = (config.LIB_DIR or "").strip()
    if not lib_dir:
        lib_dir = str(Path.home() / ".config" / "abx" / "lib")

    # Structure: lib/arm64-darwin/npm (npm will create node_modules inside this)
    npm_prefix = Path(lib_dir) / "npm"
    npm_prefix.mkdir(parents=True, exist_ok=True)

    # Use abx-pkg NpmProvider to install binary with custom prefix
    provider = NpmProvider(npm_prefix=npm_prefix)
    if not provider.INSTALLER_BIN_ABSPATH:
        click.echo("npm not available on this system", err=True)
        sys.exit(0)

    click.echo(f"Installing {name} via npm to {npm_prefix}...", err=True)

    prior_skip_download: str | None = None
    prior_skip_chromium_download: str | None = None
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
        if binary.overrides:
            click.echo(
                f"Using custom install overrides: {binary.overrides}",
                err=True,
            )

        prior_skip_download = os.environ.get("PUPPETEER_SKIP_DOWNLOAD")
        prior_skip_chromium_download = os.environ.get(
            "PUPPETEER_SKIP_CHROMIUM_DOWNLOAD",
        )
        if name == "puppeteer":
            os.environ["PUPPETEER_SKIP_DOWNLOAD"] = "true"
            os.environ["PUPPETEER_SKIP_CHROMIUM_DOWNLOAD"] = "true"

        binary = binary.load_or_install()
    except Exception as e:
        click.echo(f"npm install failed: {e}", err=True)
        sys.exit(1)
    finally:
        if name == "puppeteer":
            if prior_skip_download is None:
                os.environ.pop("PUPPETEER_SKIP_DOWNLOAD", None)
            else:
                os.environ["PUPPETEER_SKIP_DOWNLOAD"] = prior_skip_download
            if prior_skip_chromium_download is None:
                os.environ.pop("PUPPETEER_SKIP_CHROMIUM_DOWNLOAD", None)
            else:
                os.environ["PUPPETEER_SKIP_CHROMIUM_DOWNLOAD"] = (
                    prior_skip_chromium_download
                )

    if not binary.abspath:
        click.echo(f"{name} not found after npm install", err=True)
        sys.exit(1)

    # Output Binary JSONL record to stdout
    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider="npm",
    )

    # Log human-readable info to stderr
    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    # Lock down lib/ so snapshot hooks can read/execute but not write
    enforce_lib_permissions()

    sys.exit(0)


if __name__ == "__main__":
    main()

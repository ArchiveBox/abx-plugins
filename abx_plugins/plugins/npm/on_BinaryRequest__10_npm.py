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
# Install a binary using npm package manager.
#
# Usage:
#     ./on_BinaryRequest__10_npm.py --name=<name> [...] > events.jsonl

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
    enforce_lib_permissions,
    load_config,
    parse_extra_hook_args,
)

import rich_click as click
from abxpkg import Binary, NpmProvider


def _npm_package_name(install_arg: str) -> str | None:
    """Return the node_modules package path implied by one npm install arg."""
    if not install_arg or install_arg.startswith(("-", ".", "/")):
        return None
    if ":" in install_arg.split("/")[0]:
        return None
    if install_arg.startswith("@"):
        parts = install_arg.split("/")
        if len(parts) < 2:
            return None
        scope = parts[0]
        package = parts[1].split("@", 1)[0]
        return f"{scope}/{package}" if package else None
    return install_arg.split("@", 1)[0]


def _missing_requested_packages(npm_prefix: Path, install_args: list[str]) -> list[str]:
    """Find requested npm packages that are absent from the managed prefix."""
    missing: list[str] = []
    for install_arg in install_args:
        package_name = _npm_package_name(str(install_arg))
        if not package_name:
            continue
        if not (npm_prefix / "node_modules" / package_name / "package.json").exists():
            missing.append(package_name)
    return missing


def _static_install_args(value: object) -> list[str]:
    if value is None or isinstance(value, Callable):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, tuple | list):
        return [str(item) for item in value]
    return []


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

    # Use abxpkg NpmProvider to install binary with custom prefix
    provider = NpmProvider(install_root=npm_prefix)
    try:
        provider.INSTALLER_BINARY()
    except Exception:
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

        npm_overrides = binary.overrides.get("npm", {})
        install_args = _static_install_args(npm_overrides.get("install_args"))
        missing_packages = _missing_requested_packages(npm_prefix, install_args)
        if missing_packages:
            click.echo(
                f"Missing requested npm packages {', '.join(missing_packages)}; forcing npm install",
                err=True,
            )

        binary = binary.install(no_cache=bool(missing_packages))
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
        binary=binary,
    )

    # Log human-readable info to stderr
    click.echo(f"Installed {name} at {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)

    # Lock down lib/ so snapshot hooks can read/execute but not write
    enforce_lib_permissions()

    sys.exit(0)


if __name__ == "__main__":
    main()

#!/usr/bin/env -S uv run --active --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pydantic-settings",
#     "jambo",
#     "rich-click",
#     "abxpkg>=1.10.4",
#     "abx-plugins>=1.10.27",
# ]
# ///
"""
Install Chromium via the Puppeteer CLI.

Usage: on_BinaryRequest__12_puppeteer.py --name=<name>
Output: Binary JSONL record to stdout after installation
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import rich_click as click
from abxpkg import AptProvider, Binary, EnvProvider, PuppeteerProvider

from abx_plugins.plugins.base.utils import (
    emit_installed_binary_record,
    load_config,
    parse_extra_hook_args,
)


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--name", required=True, help="Binary name to install")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--overrides", default=None, help="JSON-encoded overrides dict")
def main(
    name: str,
    binproviders: str,
    overrides: str | None,
) -> None:
    config = load_config()

    if binproviders != "*" and "puppeteer" not in binproviders.split(","):
        sys.exit(0)

    if name not in ("chrome", "chromium"):
        sys.exit(0)

    existing_chrome_binary = (config.CHROME_BINARY or "").strip()
    if existing_chrome_binary and _is_explicit_path(existing_chrome_binary):
        existing_binary = _load_binary_from_path(existing_chrome_binary, name=name)
        if existing_binary and existing_binary.abspath:
            _emit_browser_binary_record(binary=existing_binary, name=name)
            sys.exit(0)

    lib_dir = (config.LIB_DIR or "").strip()
    if not lib_dir:
        lib_dir = str(Path.home() / ".config" / "abx" / "lib")
    os.environ.setdefault("ABXPKG_LIB_DIR", str(Path(lib_dir).expanduser().resolve()))
    provider = PuppeteerProvider()

    raw_overrides = json.loads(overrides) if overrides else {}
    if not isinstance(raw_overrides, dict):
        click.echo("puppeteer overrides must decode to an object", err=True)
        sys.exit(1)

    provider_overrides = raw_overrides.get("puppeteer")
    default_install_args = ["chromium@latest"]
    if provider_overrides is None:
        raw_overrides = {
            **raw_overrides,
            "puppeteer": {
                "install_args": _install_args_for_current_user(default_install_args),
            },
        }
    elif (
        isinstance(provider_overrides, dict)
        and "install_args" not in provider_overrides
    ):
        raw_overrides = {
            **raw_overrides,
            "puppeteer": {
                **provider_overrides,
                "install_args": _install_args_for_current_user(default_install_args),
            },
        }
    elif isinstance(provider_overrides, dict):
        install_args = provider_overrides.get("install_args")
        if isinstance(install_args, list):
            raw_overrides = {
                **raw_overrides,
                "puppeteer": {
                    **provider_overrides,
                    "install_args": _install_args_for_current_user(install_args),
                },
            }

    _install_browser_system_deps_if_needed()

    context = click.get_current_context(silent=True)
    extra_kwargs = parse_extra_hook_args(context.args if context else [])

    try:
        binary = Binary.model_validate(
            {
                **extra_kwargs,
                "name": name,
                "binproviders": [provider],
                "overrides": raw_overrides,
            },
        ).install()
    except Exception as e:
        error_output = str(e)
        click.echo(f"puppeteer install failed: {error_output}", err=True)
        sys.exit(1)

    if not binary.abspath:
        click.echo("ERROR: failed to locate browser after install", err=True)
        sys.exit(1)

    _emit_browser_binary_record(
        binary=binary,
        name=name,
    )

    sys.exit(0)


def _emit_browser_binary_record(
    binary: Binary,
    name: str,
) -> None:
    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version) if binary.version else "",
        sha256=binary.sha256 or "",
        binprovider="puppeteer",
        binary=binary,
    )


def _load_binary_from_path(path: str, name: str) -> Binary | None:
    raw_path = str(path or "").strip()
    if not raw_path or not _is_explicit_path(raw_path):
        return None
    path_obj = Path(raw_path).expanduser()
    overrides = {"env": {"abspath": str(path_obj)}}
    try:
        binary = Binary.model_validate(
            {
                "name": path_obj.name,
                "binproviders": [EnvProvider()],
                "overrides": overrides,
            },
        ).load()
    except Exception:
        return None
    if binary and binary.abspath and _is_supported_chromium_binary(binary.abspath):
        return binary
    return None


def _is_supported_chromium_binary(path: str | Path) -> bool:
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    version = f"{proc.stdout}\n{proc.stderr}".strip()
    return bool(version)


def _install_args_for_current_user(install_args: list[str]) -> list[str]:
    args = [str(arg) for arg in install_args]
    if _should_use_abx_apt_browser_deps():
        return args
    if os.geteuid() != 0 or "--install-deps" in args:
        return args

    browser_arg = next((arg for arg in args if not arg.startswith("-")), "")
    if browser_arg.split("@", 1)[0] not in {
        "chrome",
        "chromium",
        "chrome-headless-shell",
    }:
        return args

    return [*args, "--install-deps"]


def _install_browser_system_deps_if_needed() -> None:
    if not _should_use_abx_apt_browser_deps():
        return

    packages = _browser_system_packages()
    if not packages:
        return

    AptProvider().default_install_handler(
        "chromium-system-deps",
        install_args=packages,
        timeout=int(
            os.environ.get("PUPPETEER_TIMEOUT") or os.environ.get("TIMEOUT") or 900,
        ),
    )


def _should_use_abx_apt_browser_deps() -> bool:
    return (
        os.geteuid() == 0
        and sys.platform.startswith("linux")
        and str(os.environ.get("IN_DOCKER", "")).lower() in {"1", "true", "yes"}
    )


def _browser_system_packages() -> list[str]:
    packages = [
        "fonts-liberation",
        "fonts-noto-color-emoji",
        "libasound2",
        "libatk-bridge2.0-0",
        "libatk1.0-0",
        "libatspi2.0-0",
        "libcairo2",
        "libcups2",
        "libdbus-1-3",
        "libdrm2",
        "libexpat1",
        "libgbm1",
        "libglib2.0-0",
        "libgtk-3-0",
        "libnspr4",
        "libnss3",
        "libpango-1.0-0",
        "libvulkan1",
        "libx11-6",
        "libx11-xcb1",
        "libxcb1",
        "libxcomposite1",
        "libxdamage1",
        "libxext6",
        "libxfixes3",
        "libxkbcommon0",
        "libxrandr2",
        "libxshmfence1",
        "wget",
        "xdg-utils",
    ]
    if _os_release_id() == "ubuntu" and _os_release_version_id().startswith("24."):
        packages = [
            "libasound2t64" if package == "libasound2" else package
            for package in packages
        ]
    return packages


def _os_release_id() -> str:
    return _os_release_value("ID")


def _os_release_version_id() -> str:
    return _os_release_value("VERSION_ID")


def _os_release_value(key: str) -> str:
    path = Path("/etc/os-release")
    if not path.exists():
        return ""
    prefix = f"{key}="
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip().strip('"').strip("'").lower()
    return ""


def _is_explicit_path(value: str) -> bool:
    raw_value = str(value or "").strip()
    return (
        raw_value.startswith(("~", ".", "/")) or "/" in raw_value or "\\" in raw_value
    )


if __name__ == "__main__":
    main()

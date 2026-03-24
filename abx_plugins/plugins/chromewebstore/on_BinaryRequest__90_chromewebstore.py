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
"""Install Chrome Web Store extensions as binary-like artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import rich_click as click
from abx_pkg import Binary, EnvProvider, SemVer

from abx_plugins.plugins.base.utils import emit_installed_binary_record, load_config


CHROME_UTILS_PATH = (
    Path(__file__).resolve().parent.parent / "chrome" / "chrome_utils.js"
)


def _extensions_dir() -> Path:
    config = load_config()
    if config.CHROME_EXTENSIONS_DIR:
        return Path(config.CHROME_EXTENSIONS_DIR).expanduser().resolve()
    return (
        Path(config.PERSONAS_DIR).expanduser()
        / config.ACTIVE_PERSONA
        / "chrome_extensions"
    ).resolve()


def _parse_overrides(raw_overrides: str | None) -> dict[str, Any]:
    if not raw_overrides:
        return {}
    try:
        parsed = json.loads(raw_overrides)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _hash_file(path: Path) -> str:
    hash_sha256 = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hash_sha256.update(chunk)
    return hash_sha256.hexdigest()


class ChromeWebstoreProvider(EnvProvider):
    name: str = "chromewebstore"
    INSTALLER_BIN: str = "node"
    overrides: dict[str, dict[str, str]] = {
        "*": {
            "abspath": "self.chromewebstore_abspath_handler",
            "version": "self.chromewebstore_version_handler",
            "install_args": "self.chromewebstore_install_args_handler",
            "install": "self.chromewebstore_install_handler",
            "update": "self.chromewebstore_install_handler",
            "uninstall": "self.uninstall_noop",
        },
    }

    def chromewebstore_install_args_handler(
        self, bin_name: str, **context
    ) -> list[str]:
        return [bin_name, bin_name]

    def _cache_path(self, bin_name: str) -> Path:
        return _extensions_dir() / f"{bin_name}.extension.json"

    def _cached_extension(self, bin_name: str) -> dict[str, Any]:
        cache_path = self._cache_path(bin_name)
        if not cache_path.exists():
            return {}
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return cached if isinstance(cached, dict) else {}

    def _extension_name(self, bin_name: str, install_args: list[str]) -> str:
        if len(install_args) > 1:
            raw_name = str(install_args[1])
            if raw_name.startswith("--name="):
                return raw_name.split("=", 1)[1] or bin_name
            return raw_name
        return bin_name

    def _extension_spec(self, bin_name: str) -> tuple[str, str, Path, Path, Path]:
        cached = self._cached_extension(bin_name)
        install_args = list(self.get_install_args(bin_name, quiet=True))
        webstore_id = str(
            cached.get("webstore_id") or (install_args[0] if install_args else bin_name)
        )
        extension_name = str(
            cached.get("name") or self._extension_name(bin_name, install_args)
        )
        extensions_dir = _extensions_dir()
        unpacked_path = Path(
            cached.get("unpacked_path")
            or (extensions_dir / f"{webstore_id}__{extension_name}")
        )
        crx_path = Path(
            cached.get("crx_path")
            or (extensions_dir / f"{webstore_id}__{extension_name}.crx")
        )
        manifest_path = unpacked_path / "manifest.json"
        return webstore_id, extension_name, unpacked_path, crx_path, manifest_path

    def chromewebstore_abspath_handler(self, bin_name: str, **context) -> str | None:
        _, _, _, _, manifest_path = self._extension_spec(bin_name)
        if manifest_path.exists():
            return str(manifest_path)
        return None

    def chromewebstore_version_handler(
        self,
        bin_name: str,
        abspath: str | Path | None = None,
        **context,
    ) -> str | None:
        _, _, _, _, manifest_path = self._extension_spec(bin_name)
        if not manifest_path.exists():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return str(manifest.get("version") or "")

    def get_sha256(
        self, bin_name: str, abspath: str | Path | None = None, nocache: bool = False
    ) -> str | None:
        _, _, _, crx_path, manifest_path = self._extension_spec(bin_name)
        if crx_path.exists():
            return _hash_file(crx_path)
        if manifest_path.exists():
            return _hash_file(manifest_path)
        return None

    def chromewebstore_install_handler(
        self,
        bin_name: str,
        install_args: list[str] | tuple[str, ...] | None = None,
        **context,
    ) -> str:
        install_args = list(install_args or self.get_install_args(bin_name))
        webstore_id = str(install_args[0] if install_args else bin_name)
        extension_name = self._extension_name(bin_name, install_args)
        if self.DRY_RUN:
            return f"DRY_RUN would install extension {extension_name} ({webstore_id})"

        node_binary = self.INSTALLER_BIN_ABSPATH
        if not node_binary:
            raise FileNotFoundError(
                "node is required to install Chrome Web Store extensions"
            )

        proc = subprocess.run(
            [
                str(node_binary),
                str(CHROME_UTILS_PATH),
                "installExtensionWithCache",
                webstore_id,
                extension_name,
            ],
            capture_output=True,
            text=True,
            timeout=self._install_timeout,
            env=os.environ.copy(),
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stdout + "\n" + proc.stderr).strip())
        return f"Installed extension {extension_name} ({webstore_id})"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--name", required=True, help="Chrome Web Store extension ID")
@click.option("--binproviders", default="*", help="Allowed providers (comma-separated)")
@click.option("--min-version", default="", help="Minimum acceptable version")
@click.option("--overrides", default=None, help="JSON-encoded overrides dict")
def main(
    name: str,
    binproviders: str,
    min_version: str,
    overrides: str | None,
) -> None:
    if binproviders != "*" and "chromewebstore" not in [
        provider.strip() for provider in binproviders.split(",")
    ]:
        sys.exit(0)

    parsed_overrides = _parse_overrides(overrides)
    provider_overrides = parsed_overrides.get("chromewebstore", {})

    provider = ChromeWebstoreProvider()
    binary = Binary(
        name=name,
        min_version=SemVer(min_version) if min_version else None,
        binproviders=[provider],
        overrides={"chromewebstore": provider_overrides},
    ).load_or_install()

    if not binary.abspath:
        click.echo(f"{name} not resolved as Chrome Web Store extension", err=True)
        sys.exit(1)

    emit_installed_binary_record(
        name=name,
        abspath=str(binary.abspath),
        version=str(binary.version or ""),
        sha256=str(binary.sha256 or ""),
        binprovider="chromewebstore",
    )

    click.echo(f"Resolved extension {name} -> {binary.abspath}", err=True)
    click.echo(f"  version: {binary.version}", err=True)
    sys.exit(0)


if __name__ == "__main__":
    main()

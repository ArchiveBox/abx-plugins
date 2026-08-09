from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from abx_plugins.plugins.base.testing import install_required_binary_from_config


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPO_ROOT / "abx_plugins" / "plugins"
REQUIRED_METADATA_FIELDS = (
    "title",
    "description",
    "required_plugins",
    "required_binaries",
    "output_mimetypes",
)


def _iter_plugin_dirs() -> list[Path]:
    return sorted(
        path
        for path in PLUGINS_ROOT.iterdir()
        if path.is_dir() and path.name != "__pycache__"
    )


def _is_non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def test_every_plugin_has_config_json_with_required_metadata() -> None:
    failures: list[str] = []

    for plugin_dir in _iter_plugin_dirs():
        config_path = plugin_dir / "config.json"
        plugin_name = plugin_dir.name

        if not config_path.exists():
            failures.append(f"{plugin_name}: missing config.json")
            continue

        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as err:
            failures.append(f"{plugin_name}: invalid JSON in config.json ({err})")
            continue

        for field in REQUIRED_METADATA_FIELDS:
            if field not in config:
                failures.append(f"{plugin_name}: missing top-level field {field!r}")

        title = config.get("title")
        if not _is_non_empty_string(title):
            failures.append(f"{plugin_name}: 'title' must be a non-empty string")

        description = config.get("description")
        if not _is_non_empty_string(description):
            failures.append(f"{plugin_name}: 'description' must be a non-empty string")

        for field in ("required_plugins", "output_mimetypes"):
            value = config.get(field)
            if not isinstance(value, list):
                failures.append(f"{plugin_name}: {field!r} must be a list")
                continue
            if any(not _is_non_empty_string(item) for item in value):
                failures.append(
                    f"{plugin_name}: {field!r} must contain only non-empty strings",
                )

        required_binaries = config.get("required_binaries")
        if not isinstance(required_binaries, list):
            failures.append(f"{plugin_name}: 'required_binaries' must be a list")
        else:
            for index, item in enumerate(required_binaries):
                label = f"{plugin_name}: required_binaries[{index}]"
                if not isinstance(item, dict):
                    failures.append(f"{label} must be an object")
                    continue
                item_dict = cast(dict[str, Any], item)
                required_keys = {"name", "binproviders", "min_version"}
                missing_keys = required_keys - item_dict.keys()
                if missing_keys:
                    failures.append(f"{label} missing keys: {sorted(missing_keys)!r}")
                if not _is_non_empty_string(item_dict.get("name")):
                    failures.append(f"{label}.name must be a non-empty string")
                if not _is_non_empty_string(item_dict.get("binproviders")):
                    failures.append(f"{label}.binproviders must be a non-empty string")
                min_version = item_dict.get("min_version")
                if min_version is not None and not _is_non_empty_string(min_version):
                    failures.append(
                        f"{label}.min_version must be null or a non-empty string",
                    )
                if "overrides" in item_dict and not isinstance(
                    item_dict["overrides"],
                    dict,
                ):
                    failures.append(f"{label}.overrides must be an object when present")

        required_plugins = config.get("required_plugins", [])
        if isinstance(required_plugins, list):
            for dependency in required_plugins:
                if dependency == plugin_name:
                    failures.append(
                        f"{plugin_name}: 'required_plugins' must not include itself",
                    )
                elif not (PLUGINS_ROOT / dependency).is_dir():
                    failures.append(
                        f"{plugin_name}: 'required_plugins' references unknown plugin {dependency!r}",
                    )

    assert not failures, "Plugin config metadata validation failed:\n" + "\n".join(
        failures,
    )


def test_required_binary_configs_use_uv_and_pnpm_not_pip_or_npm() -> None:
    failures: list[str] = []

    for plugin_dir in _iter_plugin_dirs():
        config_path = plugin_dir / "config.json"
        if not config_path.exists():
            continue
        config = cast(
            dict[str, Any],
            json.loads(config_path.read_text(encoding="utf-8")),
        )
        required_binaries = config.get("required_binaries")
        if not isinstance(required_binaries, list):
            continue
        for index, item in enumerate(required_binaries):
            if not isinstance(item, dict):
                continue
            item = cast(dict[str, Any], item)
            label = f"{plugin_dir.name}: required_binaries[{index}]"
            binproviders = {
                provider.strip()
                for provider in str(item.get("binproviders") or "").split(",")
                if provider.strip()
            }
            if "pip" in binproviders:
                failures.append(f"{label}.binproviders must use uv instead of pip")
            if "npm" in binproviders:
                failures.append(f"{label}.binproviders must use pnpm instead of npm")
            raw_overrides = item.get("overrides")
            overrides = (
                cast(dict[str, Any], raw_overrides)
                if isinstance(raw_overrides, dict)
                else {}
            )
            if "pip" in overrides:
                failures.append(f"{label}.overrides must use uv instead of pip")
            if "npm" in overrides:
                failures.append(f"{label}.overrides must use pnpm instead of npm")
            if any(
                isinstance(value, dict) and "module_name" in value
                for value in overrides.values()
            ):
                failures.append(f"{label}.overrides must not declare module_name")
            if "pnpm" in binproviders:
                pnpm_overrides = overrides.get("pnpm")
                if not isinstance(pnpm_overrides, dict) or not pnpm_overrides.get(
                    "install_root",
                ):
                    failures.append(
                        f"{label}.overrides.pnpm must declare an isolated install_root",
                    )

    assert not failures, "Plugin provider policy validation failed:\n" + "\n".join(
        failures,
    )


def test_uv_required_cli_binaries_enable_postinstall_scripts() -> None:
    failures: list[str] = []

    for plugin_dir in _iter_plugin_dirs():
        config_path = plugin_dir / "config.json"
        if not config_path.exists():
            continue
        config = cast(
            dict[str, Any],
            json.loads(config_path.read_text(encoding="utf-8")),
        )
        required_binaries = config.get("required_binaries")
        if not isinstance(required_binaries, list):
            continue
        for index, item in enumerate(required_binaries):
            if not isinstance(item, dict):
                continue
            item = cast(dict[str, Any], item)
            binproviders = {
                provider.strip()
                for provider in str(item.get("binproviders") or "").split(",")
                if provider.strip()
            }
            if "uv" not in binproviders:
                continue
            binary_name = _hydrated_binary_name(str(item["name"]), config)
            overrides = item.get("overrides")
            uv_overrides = (
                cast(dict[str, Any], overrides).get("uv")
                if isinstance(overrides, dict)
                else None
            )
            if (
                not isinstance(uv_overrides, dict)
                or uv_overrides.get("postinstall_scripts") is not True
            ):
                failures.append(
                    f"{plugin_dir.name}: required_binaries[{index}] {binary_name!r} uses uv and must set overrides.uv.postinstall_scripts=true",
                )

    assert not failures, (
        "uv-managed CLI required_binaries must install console entrypoint scripts:\n"
        + "\n".join(failures)
    )


def test_required_binary_configs_prefer_compatible_host_binaries() -> None:
    failures: list[str] = []

    for plugin_dir in _iter_plugin_dirs():
        config_path = plugin_dir / "config.json"
        if not config_path.exists():
            continue
        config = cast(
            dict[str, Any],
            json.loads(config_path.read_text(encoding="utf-8")),
        )
        required_binaries = config.get("required_binaries")
        if not isinstance(required_binaries, list):
            continue
        for index, item in enumerate(required_binaries):
            if not isinstance(item, dict):
                continue
            providers = [
                provider.strip()
                for provider in str(item.get("binproviders") or "").split(",")
                if provider.strip()
            ]
            overrides = item.get("overrides")
            uv_overrides = overrides.get("uv") if isinstance(overrides, dict) else None
            uv_install_args = (
                uv_overrides.get("install_args")
                if isinstance(uv_overrides, dict)
                else None
            )
            managed_uv = (
                providers[:1] == ["uv"]
                and isinstance(uv_overrides, dict)
                and bool(uv_overrides.get("install_root"))
                and isinstance(uv_install_args, list)
                and bool(uv_install_args)
                and all("==" in str(package) for package in uv_install_args)
            )
            if (not providers or providers[0] != "env") and not managed_uv:
                failures.append(
                    f"{plugin_dir.name}: required_binaries[{index}] must try env first or use an isolated uv root with exact pins",
                )
            if "apt" in providers:
                apt_index = providers.index("apt")
                for preferred_provider in (
                    "node",
                    "brew",
                    "nix",
                    "uv",
                    "pnpm",
                    "puppeteer",
                ):
                    if (
                        preferred_provider in providers
                        and providers.index(preferred_provider) > apt_index
                    ):
                        failures.append(
                            f"{plugin_dir.name}: required_binaries[{index}] must try {preferred_provider} before apt",
                        )

    assert not failures, (
        "Plugin host binary preference validation failed:\n"
        + "\n".join(
            failures,
        )
    )


def _required_binary_names(config: dict[str, Any]) -> set[str]:
    required_binaries = config.get("required_binaries")
    if not isinstance(required_binaries, list):
        return set()
    return {
        str(item.get("name"))
        for item in required_binaries
        if isinstance(item, dict) and _is_non_empty_string(item.get("name"))
    }


def test_plugins_do_not_duplicate_required_plugin_binaries() -> None:
    configs: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    for plugin_dir in _iter_plugin_dirs():
        config_path = plugin_dir / "config.json"
        if not config_path.exists():
            continue
        configs[plugin_dir.name] = cast(
            dict[str, Any],
            json.loads(config_path.read_text(encoding="utf-8")),
        )

    for plugin_name, config in configs.items():
        required_plugins = config.get("required_plugins")
        if not isinstance(required_plugins, list):
            continue

        own_binary_names = _required_binary_names(config)
        for dependency_name in required_plugins:
            dependency_config = configs.get(str(dependency_name))
            if not dependency_config:
                continue
            duplicated = own_binary_names & _required_binary_names(dependency_config)
            for binary_name in sorted(duplicated):
                failures.append(
                    f"{plugin_name}: required_binaries duplicates {binary_name!r} from required plugin {dependency_name!r}",
                )

    assert not failures, (
        "Plugin configs must inherit upstream plugin binaries through required_plugins:\n"
        + "\n".join(failures)
    )


def _hydrated_binary_name(name: str, config: dict[str, Any]) -> str:
    if not (name.startswith("{") and name.endswith("}")):
        return name
    key = name[1:-1]
    prop = (config.get("properties") or {}).get(key)
    default = prop.get("default") if isinstance(prop, dict) else None
    return default if isinstance(default, str) and default else name


def test_pnpm_required_binaries_resolve_through_plugin_config() -> None:
    failures: list[str] = []

    for plugin_dir in _iter_plugin_dirs():
        config_path = plugin_dir / "config.json"
        if not config_path.exists():
            continue
        config = cast(
            dict[str, Any],
            json.loads(config_path.read_text(encoding="utf-8")),
        )
        required_binaries = config.get("required_binaries")
        if not isinstance(required_binaries, list):
            continue
        for index, item in enumerate(required_binaries):
            if not isinstance(item, dict):
                continue
            item = cast(dict[str, Any], item)
            binproviders = {
                provider.strip()
                for provider in str(item.get("binproviders") or "").split(",")
                if provider.strip()
            }
            if "pnpm" not in binproviders:
                continue
            binary_name = _hydrated_binary_name(str(item["name"]), config)
            overrides = item.get("overrides")
            pnpm_overrides = (
                overrides.get("pnpm") if isinstance(overrides, dict) else None
            )
            if "min_release_age" in item and (
                not isinstance(pnpm_overrides, dict)
                or pnpm_overrides.get("min_release_age") != item["min_release_age"]
            ):
                failures.append(
                    f"{plugin_dir.name}: required_binaries[{index}] {binary_name!r} must preserve min_release_age in overrides.pnpm",
                )
                continue
            try:
                loaded = install_required_binary_from_config(plugin_dir, binary_name)
            except Exception as err:
                failures.append(
                    f"{plugin_dir.name}: required_binaries[{index}] {binary_name!r} failed to resolve via config: {type(err).__name__}: {err}",
                )
                continue
            abspath = loaded.abspath
            if not abspath:
                failures.append(
                    f"{plugin_dir.name}: required_binaries[{index}] {binary_name!r} resolved without an abspath",
                )
                continue
            if not Path(abspath).exists():
                failures.append(
                    f"{plugin_dir.name}: required_binaries[{index}] {binary_name!r} abspath does not exist: {abspath}",
                )

    assert not failures, "pnpm required binary config resolution failed:\n" + "\n".join(
        failures,
    )

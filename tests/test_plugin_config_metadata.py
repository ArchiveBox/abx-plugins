from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast


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
                if "overrides" in item_dict and not isinstance(item_dict["overrides"], dict):
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

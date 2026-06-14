"""Shared utilities for abx plugins.

Provides common helpers used across multiple plugins:
- Config loading from `config.json` using `jambo` with `x-aliases` and `x-fallback`
- JSONL record emission (archive results, binary requests, installed binaries)
- Atomic file writing (`write_text_atomic`, `write_file_atomic`)
- HTML source discovery (`find_html_source`)
- Sibling plugin output checking (`has_staticfile_output`)

Import directly via the package path::

    from abx_plugins.plugins.base.utils import load_config, get_config
"""

from __future__ import annotations

import json
import os
import stat
import sys
from urllib.parse import unquote, urlparse
from collections.abc import Mapping, MutableMapping
from functools import lru_cache
from pathlib import Path
from typing import Any, TextIO, cast


# ---------------------------------------------------------------------------
# Shared config resolution
# ---------------------------------------------------------------------------

BASE_CONFIG_PATH = Path(__file__).with_name("config.json")
PLUGINS_DIR = BASE_CONFIG_PATH.parent.parent
PROCESS_EXIT_SKIPPED = 10
INTERNAL_INPUT_URL = "archivebox://internal"


def normalize_config_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [normalize_config_value(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_config_value(val) for key, val in value.items()}
    return value


def apply_exec_env(
    exec_env: Mapping[str, str],
    env: MutableMapping[str, str],
) -> None:
    """Apply one execution-time env layer to ``env`` in place.

    Value semantics:
    - ``"value"`` overwrites the existing value
    - ``":value"`` appends to the existing value
    - ``"value:"`` prepends to the existing value
    """

    for key, value in exec_env.items():
        if value.startswith(":"):
            existing = env.get(key, "")
            env[key] = f"{existing}{value}" if existing else value[1:]
        elif value.endswith(":"):
            existing = env.get(key, "")
            env[key] = f"{value}{existing}" if existing else value[:-1]
        else:
            env[key] = value


def _parse_config_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parse_extra_hook_args(args: list[str]) -> dict[str, Any]:
    """Parse unknown Click hook args, accepting both JSON and plain strings."""
    parsed: dict[str, Any] = {}
    idx = 0
    while idx < len(args):
        arg = str(args[idx])
        idx += 1
        if not arg.startswith("--"):
            continue

        if "=" in arg:
            raw_key, raw_value = arg[2:].split("=", 1)
        else:
            raw_key = arg[2:]
            if idx < len(args) and not str(args[idx]).startswith("--"):
                raw_value = str(args[idx])
                idx += 1
            else:
                raw_value = "true"

        parsed[raw_key.replace("-", "_")] = _parse_config_value(raw_value)
    return parsed


def _schema_types(prop: Mapping[str, Any]) -> set[str]:
    raw_type = prop["type"] if "type" in prop else None
    if isinstance(raw_type, str):
        return {raw_type}
    if isinstance(raw_type, list):
        return {str(item) for item in raw_type}
    return set()


def _resolve_config_path(
    config_path: Path | str | None,
    *,
    stack_depth: int = 1,
) -> Path:
    if config_path is None:
        frame = sys._getframe(stack_depth)
        base_utils_path = Path(__file__).resolve()
        while frame:
            caller_file = Path(frame.f_code.co_filename).resolve()
            candidate = caller_file.parent / "config.json"
            if caller_file != base_utils_path and candidate.exists():
                return candidate.resolve()
            frame = frame.f_back
        raise FileNotFoundError("No plugin config.json found for load_config() caller")
    return Path(config_path).resolve()


@lru_cache(maxsize=None)
def _load_schema(path: Path) -> dict[str, Any]:
    data = json.loads(path.resolve().read_text())
    return data if isinstance(data, dict) else {}


def _argv_url() -> str:
    argv = sys.argv[1:]
    for index, arg in enumerate(argv):
        if arg == "--url" and index + 1 < len(argv):
            return str(argv[index + 1]).strip()
        if arg.startswith("--url="):
            return arg.split("=", 1)[1].strip()
    return ""


def _is_snapshot_hook() -> bool:
    return Path(sys.argv[0] or "").name.startswith("on_Snapshot__")


def _maybe_skip_unsupported_snapshot_url(schema: Mapping[str, Any]) -> None:
    url = _argv_url()
    if not (_is_snapshot_hook() and url):
        return
    if url.startswith(("http://", "https://", "file://")):
        return
    # ArchiveBox represents pasted/stdin import content as one synthetic
    # snapshot URL. Only plugins that explicitly declare they consume that
    # internal input should run; every other snapshot hook should cheaply
    # no-result before starting browsers/downloaders or touching the network.
    if url == INTERNAL_INPUT_URL and bool(schema.get("x-accepts-internal-input")):
        return

    record = {
        "type": "ArchiveResult",
        "status": "noresults",
        "output_str": f"unsupported input URL: {url}",
    }
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()
    raise SystemExit(0)


@lru_cache(maxsize=None)
def _collect_required_schema_path_strs(config_path: Path) -> tuple[str, ...]:
    seen: set[Path] = set()
    paths: list[Path] = []

    def walk(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        schema = _load_schema(resolved)
        required_plugins = schema.get("required_plugins") or []
        for required_plugin in (
            required_plugins if isinstance(required_plugins, list) else []
        ):
            required_path = (
                PLUGINS_DIR / str(required_plugin) / "config.json"
            ).resolve()
            if required_path.exists():
                walk(required_path)
        paths.append(resolved)

    walk(config_path.resolve())
    return tuple(str(path) for path in paths)


def _collect_required_schema_paths(config_path: Path) -> list[Path]:
    return [
        Path(path) for path in _collect_required_schema_path_strs(config_path.resolve())
    ]


@lru_cache(maxsize=None)
def _collect_required_binary_records_cached(
    config_path: Path,
) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    paths = [BASE_CONFIG_PATH.resolve(), *_collect_required_schema_paths(config_path)]
    for path in paths:
        records.extend(_schema_required_binaries(_load_schema(path)))
    return tuple(records)


def _collect_required_binary_records(config_path: Path) -> list[dict[str, Any]]:
    return [
        dict(record)
        for record in _collect_required_binary_records_cached(config_path.resolve())
    ]


@lru_cache(maxsize=None)
def _build_merged_properties(config_path_str: str) -> tuple[str, dict[str, Any]]:
    config_path = Path(config_path_str)
    root_schema = _load_schema(config_path)
    properties: dict[str, Any] = {}
    paths = [BASE_CONFIG_PATH.resolve(), *_collect_required_schema_paths(config_path)]
    for path in paths:
        properties.update(_schema_properties(_load_schema(path)))
    return str(root_schema.get("title") or "PluginConfig"), properties


def _lookup_raw_value(
    keys: list[str],
    *,
    environ: Mapping[str, str],
    user_config: Mapping[str, str] | None = None,
) -> tuple[Any, bool, bool]:
    for key in keys:
        if key in environ:
            return environ[key], True, False
    if user_config:
        for key in keys:
            if key in user_config:
                return user_config[key], True, True
    return None, False, False


def _coerce_raw_value(
    raw_value: Any,
    prop: Mapping[str, Any],
    *,
    persisted: bool,
) -> Any:
    if not isinstance(raw_value, str):
        return raw_value
    if persisted:
        parsed_value = _parse_config_value(raw_value)
        schema_types = _schema_types(prop)
        if not schema_types:
            return parsed_value
        if parsed_value is None:
            return parsed_value if "null" in schema_types else raw_value
        if isinstance(parsed_value, bool):
            return parsed_value if "boolean" in schema_types else raw_value
        if isinstance(parsed_value, int):
            return parsed_value if {"integer", "number"} & schema_types else raw_value
        if isinstance(parsed_value, float):
            return parsed_value if "number" in schema_types else raw_value
        if isinstance(parsed_value, str):
            return parsed_value if "string" in schema_types else raw_value
        if isinstance(parsed_value, list):
            return parsed_value if "array" in schema_types else raw_value
        if isinstance(parsed_value, dict):
            return parsed_value if "object" in schema_types else raw_value
        return raw_value
    if _schema_types(prop) & {"array", "object"}:
        return _parse_config_value(raw_value)
    return raw_value


def resolve_alias(
    key: str,
    plugin_schemas: dict[str, dict[str, Any]] | None = None,
) -> str:
    if plugin_schemas is None:
        return key

    for schema in plugin_schemas.values():
        properties = _schema_properties(schema)
        if key in properties:
            return key
        for canonical_key, prop in properties.items():
            aliases = prop["x-aliases"] if "x-aliases" in prop else []
            if key in aliases:
                return canonical_key
    return key


def _resolve_schema_payload(
    properties: dict[str, Any],
    *,
    resolved_config: dict[str, Any] | None = None,
    explicit_config_keys: set[str] | None = None,
    user_config: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    resolved = dict(resolved_config or {})
    explicit_config_keys = explicit_config_keys or set()
    payload: dict[str, Any] = {}

    for _ in range(max(len(properties), 1) + 1):
        changed = False
        for key, prop in properties.items():
            alias_values = prop["x-aliases"] if "x-aliases" in prop else []
            aliases = [key, *(str(alias) for alias in alias_values)]
            raw_value, found, persisted = _lookup_raw_value(
                aliases,
                environ=environ,
                user_config=user_config,
            )
            if found:
                resolved_value = _coerce_raw_value(raw_value, prop, persisted=persisted)
                if isinstance(resolved_value, str) and "{" in resolved_value:
                    resolved_value = _hydrate_value(resolved_value, resolved)
                if payload.get(key) != resolved_value:
                    payload[key] = resolved_value
                    resolved[key] = resolved_value
                    changed = True
                continue

            fallback_key = prop["x-fallback"] if "x-fallback" in prop else None
            if fallback_key:
                fallback_raw_value, fallback_found, fallback_persisted = (
                    _lookup_raw_value(
                        [str(fallback_key)],
                        environ=environ,
                        user_config=user_config,
                    )
                )
                if fallback_found:
                    fallback_value = _coerce_raw_value(
                        fallback_raw_value,
                        prop,
                        persisted=fallback_persisted,
                    )
                    if isinstance(fallback_value, str) and "{" in fallback_value:
                        fallback_value = _hydrate_value(fallback_value, resolved)
                    if payload.get(key) != fallback_value:
                        payload[key] = fallback_value
                        resolved[key] = fallback_value
                        changed = True
                    continue
                if fallback_key in resolved:
                    fallback_value = resolved[fallback_key]
                    if payload.get(key) != fallback_value:
                        payload[key] = fallback_value
                        resolved[key] = fallback_value
                        changed = True
                    continue

            if key in resolved:
                resolved_value = resolved[key]
                default_value = prop.get("default")
                if isinstance(resolved_value, str) and "{" in resolved_value:
                    resolved_value = _hydrate_value(resolved_value, resolved)
                    resolved[key] = resolved_value
                elif (
                    key not in explicit_config_keys
                    and isinstance(default_value, str)
                    and "{" in default_value
                ):
                    resolved_value = _hydrate_value(default_value, resolved)
                    resolved[key] = resolved_value
                elif "default" in prop and resolved_value == prop["default"]:
                    resolved_value = _hydrate_value(prop["default"], resolved)
                if payload.get(key) != resolved_value:
                    payload[key] = resolved_value
                    changed = True
                continue

            if "default" in prop:
                default_value = _hydrate_value(prop["default"], resolved)
                if payload.get(key) == default_value:
                    continue
                payload[key] = default_value
                resolved[key] = default_value
                changed = True
        if not changed:
            break

    return payload


def _hydrate_value(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        try:
            return value.format(**context)
        except Exception:
            return value
    if isinstance(value, list):
        return [_hydrate_value(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _hydrate_value(item, context) for key, item in value.items()}
    return value


def _placeholder_config_key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    key = stripped[1:-1].strip()
    return key if key.endswith("_BINARY") else None


def _provider_names(binproviders: Any) -> list[str]:
    if isinstance(binproviders, str):
        names = [part.strip() for part in binproviders.split(",")]
    elif isinstance(binproviders, list):
        names = [str(part).strip() for part in binproviders]
    else:
        names = ["env"]
    return [name for name in names if name] or ["env"]


_ABXPKG_OVERRIDE_KEYS = {
    "PATH",
    "INSTALLER_BIN",
    "euid",
    "install_root",
    "bin_dir",
    "dry_run",
    "postinstall_scripts",
    "min_release_age",
    "install_timeout",
    "version_timeout",
    "abspath",
    "version",
    "install_args",
    "packages",
    "install",
    "update",
    "uninstall",
    "docs_url",
    "search",
}


def abxpkg_native_overrides(overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only provider override keys that are native abxpkg concepts."""
    if not isinstance(overrides, Mapping):
        return {}

    native: dict[str, Any] = {}
    for provider_name, provider_overrides in overrides.items():
        if isinstance(provider_overrides, list):
            native[str(provider_name)] = {"install_args": provider_overrides}
        elif isinstance(provider_overrides, Mapping):
            allowed = {
                str(key): value
                for key, value in provider_overrides.items()
                if str(key) in _ABXPKG_OVERRIDE_KEYS
            }
            if allowed:
                native[str(provider_name)] = allowed
    return native


def _abxpkg_provider_kwargs(
    provider_name: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    lib_dir_value = str(payload.get("ABXPKG_LIB_DIR") or "").strip()
    lib_dir = Path(lib_dir_value).expanduser() if lib_dir_value else None
    if provider_name == "env":
        kwargs: dict[str, Any] = {
            "PATH": str(payload.get("PATH") or os.environ.get("PATH", "")),
        }
        if lib_dir is not None:
            kwargs["install_root"] = lib_dir / "env"
        return kwargs
    if lib_dir is not None and provider_name != "env":
        return {"install_root": lib_dir / provider_name}
    return {}


def build_binproviders(
    binproviders: Any = "env",
    *,
    config: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[Any]:
    """Build abxpkg providers using the same hydrated config context as hooks."""
    from abxpkg import DEFAULT_PROVIDER_NAMES, PROVIDER_CLASS_BY_NAME

    payload: dict[str, Any] = dict(os.environ if environ is None else environ)
    if config:
        payload.update(
            {key: normalize_config_value(value) for key, value in config.items()},
        )

    provider_names = _provider_names(binproviders)
    if provider_names == ["*"]:
        provider_names = list(DEFAULT_PROVIDER_NAMES)

    return [
        PROVIDER_CLASS_BY_NAME[provider_name](
            **_abxpkg_provider_kwargs(provider_name, payload),
        )
        for provider_name in provider_names
    ]


def hydrate_required_binary(
    record: Mapping[str, Any],
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Hydrate one required_binaries record from a resolved plugin config payload."""
    return cast(dict[str, Any], _hydrate_value(dict(record), config))


def load_required_binary(
    record: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    install: bool = False,
) -> Any:
    """Load or install one required_binaries record with abxpkg."""
    from abxpkg import Binary, SemVer

    payload: dict[str, Any] = dict(os.environ if environ is None else environ)
    if config:
        payload.update(
            {key: normalize_config_value(value) for key, value in config.items()},
        )

    hydrated_record = hydrate_required_binary(record, payload)
    name = str(hydrated_record.get("name") or "").strip()
    if not name:
        raise ValueError("required_binaries record is missing a name")

    min_version = hydrated_record.get("min_version")
    binary = Binary(
        name=name,
        binproviders=build_binproviders(
            hydrated_record.get("binproviders") or "env",
            config=payload,
            environ=environ,
        ),
        min_version=SemVer(min_version) if min_version else None,
        min_release_age=hydrated_record.get("min_release_age"),
        postinstall_scripts=hydrated_record.get("postinstall_scripts"),
        overrides=abxpkg_native_overrides(hydrated_record.get("overrides")),
    )
    return binary.install() if install else binary.load()


def _load_required_binary_path(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> str | None:
    try:
        loaded = load_required_binary(record, config=payload)
    except Exception:
        return None
    if loaded.loaded_abspath:
        return str(loaded.loaded_abspath)
    return None


def _schema_properties(schema: Mapping[str, Any]) -> dict[str, Any]:
    properties = schema["properties"] if "properties" in schema else schema
    return dict(properties) if isinstance(properties, Mapping) else {}


def _schema_required_binaries(schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_records = schema["required_binaries"] if "required_binaries" in schema else []
    if not isinstance(raw_records, list):
        return []
    return [dict(record) for record in raw_records if isinstance(record, Mapping)]


def _hydrate_config_payload(
    payload: dict[str, Any],
    *,
    user_config: Mapping[str, str] | None,
    environ: Mapping[str, str] | None,
    required_binaries: list[dict[str, Any]] | None = None,
) -> None:
    for record in required_binaries or []:
        key = _placeholder_config_key(record.get("name"))
        if key is None or key not in payload:
            continue
        env = os.environ if environ is None else environ
        if key in env and Path(str(env[key])).expanduser().exists():
            continue
        loaded_path = _load_required_binary_path(record, payload)
        if loaded_path:
            payload[key] = loaded_path


@lru_cache(maxsize=None)
def _schema_model(schema_json: str):
    from jambo import SchemaConverter
    from pydantic import ConfigDict

    model = SchemaConverter.build(json.loads(schema_json))
    model.model_config = ConfigDict(
        validate_assignment=True,
        use_enum_values=True,
        validate_default=True,
    )
    model.model_rebuild(force=True)
    return model


def _open_object_annotation(prop: Mapping[str, Any]) -> type[Any] | None:
    if prop.get("type") != "object":
        return None
    if prop.get("properties"):
        return None
    additional_properties = prop.get("additionalProperties")
    if not isinstance(additional_properties, Mapping):
        return None
    item_model = build_config_model("OpenObjectValue", {"value": additional_properties})
    item_annotation = item_model.model_fields["value"].annotation
    if item_annotation is None:
        return dict[str, Any]
    return dict[str, item_annotation]


def _open_object_default(default_value: Any) -> Any:
    from pydantic_core import PydanticUndefined

    if default_value is None or default_value is PydanticUndefined:
        return {}
    return default_value


def _patch_open_object_fields(
    model,
    properties: Mapping[str, Any],
):
    from pydantic import Field, create_model
    from pydantic.fields import FieldInfo

    fields: dict[str, tuple[Any, FieldInfo]] = {}
    changed = False
    for key, field in model.model_fields.items():
        prop = properties.get(key)
        annotation = (
            _open_object_annotation(prop) if isinstance(prop, Mapping) else None
        )
        if annotation is None:
            fields[key] = (field.annotation, field)
            continue
        patched_field = cast(
            FieldInfo,
            Field(
                default_factory=lambda default=_open_object_default(field.default): (
                    dict(default)
                ),
                description=field.description,
                title=field.title,
            ),
        )
        fields[key] = (annotation, patched_field)
        changed = True
    if not changed:
        return model
    return cast(
        Any,
        create_model(
            model.__name__,
            __config__=model.model_config,
            __module__=model.__module__,
            **cast(dict[str, Any], fields),
        ),
    )


def build_config_model(
    title: str,
    properties: Mapping[str, Any],
):
    """Build the typed pydantic config model for JSONSchema properties."""
    model = _schema_model(
        json.dumps(
            {
                "title": title,
                "type": "object",
                "properties": dict(properties),
            },
            sort_keys=True,
        ),
    )
    return _patch_open_object_fields(model, properties)


def resolve_plugin_configs(
    plugin_schemas: dict[str, dict[str, Any]],
    *,
    global_config: dict[str, Any] | None = None,
    user_config: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    resolved_sections: dict[str, dict[str, Any]] = {}
    resolved_values = {
        key: normalize_config_value(value)
        for key, value in (global_config or {}).items()
    }
    explicit_config_keys = set(resolved_values)
    resolved_payloads: dict[str, dict[str, Any]] = {}

    for _ in range(max(len(plugin_schemas), 1) + 1):
        changed = False
        for plugin_name, schema in sorted(plugin_schemas.items()):
            properties = _schema_properties(schema)
            payload = _resolve_schema_payload(
                properties,
                resolved_config=resolved_values,
                explicit_config_keys=explicit_config_keys,
                user_config=user_config,
                environ=environ,
            )
            if (
                plugin_name in resolved_sections
                and resolved_payloads.get(plugin_name) == payload
            ):
                plugin_config = resolved_sections[plugin_name]
            else:
                model = build_config_model(plugin_name, properties)
                plugin_config = normalize_config_value(
                    model.model_validate(payload).model_dump(mode="json"),
                )
                resolved_payloads[plugin_name] = payload
            if resolved_sections.get(plugin_name) != plugin_config:
                resolved_sections[plugin_name] = plugin_config
                changed = True
            for key, value in plugin_config.items():
                if resolved_values.get(key) != value:
                    resolved_values[key] = value
                    changed = True
        if not changed:
            break

    return resolved_sections


def resolve_plugin_config(
    plugin_name: str,
    schema: dict[str, Any],
    *,
    global_config: dict[str, Any] | None = None,
    user_config: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    all_plugin_schemas: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    plugin_schemas = all_plugin_schemas or {plugin_name: schema}
    return resolve_plugin_configs(
        plugin_schemas,
        global_config=global_config,
        user_config=user_config,
        environ=environ,
    )[plugin_name]


def _resolve_config_payload(
    config_path: Path | str | None,
    *,
    stack_depth: int,
    global_config: Mapping[str, Any] | None,
    user_config: Mapping[str, str] | None,
    environ: Mapping[str, str] | None,
) -> tuple[Path, str, dict[str, Any], dict[str, Any]]:
    resolved_path = _resolve_config_path(config_path, stack_depth=stack_depth + 1)
    title, properties = _build_merged_properties(str(resolved_path))
    payload = _resolve_schema_payload(
        properties,
        resolved_config=dict(global_config or {}),
        explicit_config_keys=set(global_config or {}),
        user_config=user_config,
        environ=environ,
    )
    return resolved_path, title, properties, payload


def get_hydrated_required_binaries(
    config_path: Path | str | None = None,
    *,
    global_config: Mapping[str, Any] | None = None,
    user_config: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return required_binaries hydrated from the same config path as load_config()."""
    resolved_path, _, _, payload = _resolve_config_payload(
        config_path,
        stack_depth=2,
        global_config=global_config,
        user_config=user_config,
        environ=environ,
    )
    return [
        hydrate_required_binary(record, payload)
        for record in _collect_required_binary_records(resolved_path)
    ]


def _find_hydrated_required_binary(
    records: list[dict[str, Any]],
    payload: Mapping[str, Any],
    name: str,
    resolved_path: Path,
) -> dict[str, Any]:
    for record in records:
        hydrated_record = hydrate_required_binary(record, payload)
        if hydrated_record.get("name") == name:
            return hydrated_record
    raise KeyError(f"{resolved_path} required_binaries is missing {name!r}")


def get_hydrated_required_binary(
    name: str,
    config_path: Path | str | None = None,
    *,
    global_config: Mapping[str, Any] | None = None,
    user_config: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return one hydrated required_binaries record by resolved binary name."""
    resolved_path, _, _, payload = _resolve_config_payload(
        config_path,
        stack_depth=2,
        global_config=global_config,
        user_config=user_config,
        environ=environ,
    )
    return _find_hydrated_required_binary(
        _collect_required_binary_records(resolved_path),
        payload,
        name,
        resolved_path,
    )


def load_required_binary_from_config(
    name: str,
    config_path: Path | str | None = None,
    *,
    global_config: Mapping[str, Any] | None = None,
    user_config: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    install: bool = False,
) -> Any:
    """Load or install a named required_binaries entry from plugin config.json."""
    resolved_path, _, _, payload = _resolve_config_payload(
        config_path,
        stack_depth=2,
        global_config=global_config,
        user_config=user_config,
        environ=environ,
    )
    record = _find_hydrated_required_binary(
        _collect_required_binary_records(resolved_path),
        payload,
        name,
        resolved_path,
    )
    return load_required_binary(
        record,
        config=payload,
        environ=environ,
        install=install,
    )


def load_config(
    config_path: Path | str | None = None,
    *,
    global_config: Mapping[str, Any] | None = None,
    user_config: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    hydrate_binaries: bool = True,
) -> Any:
    """Load typed plugin config using `jambo` plus `x-aliases` and `x-fallback`.

    The resolved config always includes shared `base/config.json` properties plus any
    `required_plugins` config it depends on. Values are resolved in this order:

    1. process environment
    2. explicit `user_config`
    3. `x-fallback`
    4. schema defaults
    """
    resolved_path, title, properties, payload = _resolve_config_payload(
        config_path,
        stack_depth=2,
        global_config=global_config,
        user_config=user_config,
        environ=environ,
    )
    _maybe_skip_unsupported_snapshot_url(_load_schema(resolved_path))
    _hydrate_config_payload(
        payload,
        user_config=user_config,
        environ=environ,
        required_binaries=_collect_required_binary_records(resolved_path)
        if hydrate_binaries
        else None,
    )
    model = build_config_model(title, properties)
    return model.model_validate(payload)


def get_config(
    config_path: Path | str | None = None,
    *,
    global_config: Mapping[str, Any] | None = None,
    user_config: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    hydrate_binaries: bool = True,
) -> Any:
    """Alias for `load_config()` that preserves direct-caller config lookup."""
    if config_path is None:
        config_path = _resolve_config_path(None, stack_depth=2)
    return load_config(
        config_path,
        global_config=global_config,
        user_config=user_config,
        environ=environ,
        hydrate_binaries=hydrate_binaries,
    )


def iter_staticfile_text_inputs(snap_dir: Path | str | None = None) -> tuple[Path, ...]:
    """Return source text artifacts shared through the staticfile output dir."""
    base = Path(snap_dir or os.environ.get("SNAP_DIR") or ".").expanduser().resolve()
    staticfile_dir = base / "staticfile"
    if not staticfile_dir.is_dir():
        return ()
    return tuple(
        path
        for path in sorted(staticfile_dir.glob("*.txt"))
        if path.is_file() and path.stat().st_size > 0
    )


def read_file_url_text(url: str) -> str | None:
    """Read direct file:// hook input for standalone abx-plugins/abx-dl use.

    ArchiveBox validates and blocks user-supplied file URLs before they become
    crawl work. The plugin package remains a lower-level downloader/parser
    toolkit, so direct hook invocations are still allowed to parse local files.
    """
    if not url.startswith("file://"):
        return None
    parsed = urlparse(url)
    if parsed.netloc not in ("", "localhost"):
        raise ValueError(f"unsupported file URL host: {parsed.netloc}")
    return Path(unquote(parsed.path)).read_text(encoding="utf-8", errors="replace")


def _resolve_path(path_value: str) -> Path:
    return Path(path_value).expanduser().resolve()


def get_lib_dir() -> Path:
    """Return library directory.

    Priority: ABXPKG_LIB_DIR env var, otherwise the platform user-config abx/lib dir.
    """
    config = load_config(BASE_CONFIG_PATH)
    return _resolve_path(str(config.ABXPKG_LIB_DIR))


def get_personas_dir() -> Path:
    """Return personas directory.

    Returns the configured personas directory from load_config().
    """
    config = load_config(BASE_CONFIG_PATH)
    return Path(str(config.PERSONAS_DIR)).expanduser().resolve()


def has_netscape_cookie_entries(path: Path | str | None) -> bool:
    """Return True only when a cookies.txt file has usable Netscape cookie rows."""
    if not path:
        return False
    cookies_path = Path(path).expanduser()
    try:
        if not cookies_path.is_file() or cookies_path.stat().st_size == 0:
            return False
        for raw_line in cookies_path.read_text(errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and parts[0] and parts[5]:
                return True
    except OSError:
        return False
    return False


# ---------------------------------------------------------------------------
# JSONL record emission
# ---------------------------------------------------------------------------


def _fsync_if_regular_file(fd: int) -> None:
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return
    if not stat.S_ISREG(mode):
        return
    try:
        os.fsync(fd)
    except OSError:
        return


def print_and_flush(stream: TextIO, text: str) -> None:
    line = text if text.endswith("\n") else f"{text}\n"
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        stream.write(line)
        stream.flush()
        return

    try:
        stream.flush()
    except Exception:
        pass

    encoding = stream.encoding or "utf-8"
    payload = line.encode(encoding, errors="replace")
    written = 0
    while written < len(payload):
        written += os.write(fd, payload[written:])

    try:
        stream.flush()
    except Exception:
        pass

    _fsync_if_regular_file(fd)


def _parse_extra_context(raw: str, source: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        print(
            f"WARNING: ignoring invalid extra context from {source}: {err}",
            file=sys.stderr,
        )
        return {}

    if not isinstance(parsed, dict):
        print(
            f"WARNING: ignoring non-object extra context from {source}",
            file=sys.stderr,
        )
        return {}

    return parsed


def get_extra_context() -> dict[str, Any]:
    context: dict[str, Any] = {}

    env_raw = (os.environ.get("EXTRA_CONTEXT") or "").strip()
    if env_raw:
        context.update(_parse_extra_context(env_raw, "EXTRA_CONTEXT"))

    argv = sys.argv[1:]
    for index, arg in enumerate(argv):
        if arg == "--extra-context":
            if index + 1 >= len(argv):
                print(
                    "WARNING: ignoring missing value for --extra-context",
                    file=sys.stderr,
                )
                return context
            context.update(_parse_extra_context(argv[index + 1], "--extra-context"))
            return context
        if arg.startswith("--extra-context="):
            context.update(
                _parse_extra_context(arg.split("=", 1)[1], "--extra-context"),
            )
            return context

    return context


def merge_EXTRA_CONTEXT(record: dict[str, Any]) -> dict[str, Any]:
    extra_context = get_extra_context()
    if not extra_context:
        return record
    return {**extra_context, **record}


def emit_archive_result_record(
    status: str,
    output_str: str,
    **extra: Any,
) -> None:
    record: dict[str, Any] = {
        "type": "ArchiveResult",
        "status": status,
        "output_str": output_str,
    }
    if extra:
        record.update(extra)
    print_and_flush(sys.stdout, json.dumps(merge_EXTRA_CONTEXT(record)))


def emit_binary_request_record(
    name: str,
    binproviders: str,
    overrides: dict[str, Any] | None = None,
    min_version: str | None = None,
) -> None:
    """Output BinaryRequest JSONL record for a dependency."""
    record: dict[str, Any] = {
        "type": "BinaryRequest",
        "name": name,
        "binproviders": binproviders,
    }
    if overrides:
        record["overrides"] = overrides
    if min_version:
        record["min_version"] = min_version
    print_and_flush(sys.stdout, json.dumps(merge_EXTRA_CONTEXT(record)))


def emit_installed_binary_record(
    name: str,
    abspath: str,
    version: str,
    sha256: str,
    binprovider: str,
    binary: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Output Binary JSONL record for a resolved dependency."""
    if (
        env is None
        and binary is not None
        and getattr(binary, "loaded_binprovider", None)
    ):
        from abxpkg import BinProvider

        env = BinProvider.build_exec_env(
            providers=[binary.loaded_binprovider],
            base_env=os.environ,
        )

    record: dict[str, Any] = {
        "type": "Binary",
        "name": name,
        "abspath": abspath,
        "version": version,
        "sha256": sha256,
        "binprovider": binprovider,
    }
    if env:
        record["env"] = dict(env)
    print_and_flush(sys.stdout, json.dumps(merge_EXTRA_CONTEXT(record)))


def emit_tag_record(name: str) -> None:
    record: dict[str, Any] = {
        "type": "Tag",
        "name": name,
    }
    print_and_flush(sys.stdout, json.dumps(merge_EXTRA_CONTEXT(record)))


def emit_snapshot_record(record: dict[str, Any]) -> None:
    snapshot_record = {key: value for key, value in record.items() if key != "type"}
    snapshot_record["type"] = "Snapshot"
    snapshot_record = merge_EXTRA_CONTEXT(snapshot_record)
    snapshot_record["id"] = (
        str(snapshot_record["id"])
        if "id" in snapshot_record and snapshot_record["id"]
        else ""
    )
    print_and_flush(sys.stdout, json.dumps(snapshot_record))


# ---------------------------------------------------------------------------
# Atomic file writing
# ---------------------------------------------------------------------------


def write_text_atomic(path: Path, text: str) -> None:
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


# ---------------------------------------------------------------------------
# HTML source discovery (for extractors that process HTML from other plugins)
# ---------------------------------------------------------------------------


def find_html_source(*, prefer_dom: bool = False) -> str | None:
    """Find HTML content from other extractors in the snapshot directory."""
    dom_patterns = [
        "dom/output.html",
        "*_dom/output.html",
        "dom/*.html",
        "*_dom/*.html",
    ]
    singlefile_patterns = [
        "singlefile/singlefile.html",
        "*_singlefile/singlefile.html",
        "singlefile/*.html",
        "*_singlefile/*.html",
    ]
    wget_patterns = [
        "wget/**/*.html",
        "*_wget/**/*.html",
        "wget/**/*.htm",
        "*_wget/**/*.htm",
    ]
    search_patterns = [
        *(dom_patterns if prefer_dom else singlefile_patterns),
        *(singlefile_patterns if prefer_dom else dom_patterns),
        *wget_patterns,
    ]

    for base in (Path.cwd(), Path.cwd().parent):
        for pattern in search_patterns:
            for match in base.glob(pattern):
                if match.is_file() and match.stat().st_size > 0:
                    return str(match)

    return None


def find_article_html_source() -> str | None:
    """Find the best HTML source for article/text extraction.

    For article extractors, the live DOM is usually a cleaner input than the
    SingleFile artifact because SingleFile can inline large amounts of CSS,
    images, and templated app-shell markup.
    """
    return find_html_source(prefer_dom=True)


# ---------------------------------------------------------------------------
# Sibling plugin output checking
# ---------------------------------------------------------------------------


def has_staticfile_output(staticfile_dir: str = "../staticfile") -> bool:
    """Check if staticfile extractor already downloaded this URL."""
    sf_dir = Path(staticfile_dir)
    if not sf_dir.exists():
        return False
    stdout_log = sf_dir / "stdout.log"
    if not stdout_log.exists():
        return False
    for line in stdout_log.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            record.get("type") == "ArchiveResult"
            and record.get("status") == "succeeded"
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Config directory permission management
# ---------------------------------------------------------------------------


def enforce_lib_permissions(config_dir: Path | str | None = None) -> None:
    """Set permissions on ~/.config/abx so snapshot hooks can read but not write lib/.

    When running as root (e.g. during dependency preflight or crawl setup), this function
    sets ownership and permissions on the config directory so that:
      - lib/ and its contents are read+execute only (0o755 dirs, 0o644 files)
        for the data dir owner, preventing snapshot hooks from modifying
        installed binaries or node_modules
      - Everything else under ~/.config/abx (personas, etc.) is writable by
        the data dir owner

    This should be called at the end of preflight or crawl-setup work that modifies lib/.
    Snapshot hooks should NOT call this.

    Args:
        config_dir: Path to the abx config dir (default: ~/.config/abx)
    """
    if os.geteuid() != 0:
        return  # Only enforce when running as root

    if config_dir is None:
        config_dir = Path.home() / ".config" / "abx"
    else:
        config_dir = Path(config_dir)

    if not config_dir.exists():
        return

    lib_dir = config_dir / "lib"
    if not lib_dir.exists():
        return

    # Determine target uid/gid from SNAP_DIR or CRAWL_DIR ownership
    # (these represent the "data user" that snapshot hooks run as)
    config = load_config(BASE_CONFIG_PATH)
    data_dir = config.SNAP_DIR or config.CRAWL_DIR
    if data_dir and Path(data_dir).exists():
        data_stat = Path(data_dir).stat()
        target_uid = data_stat.st_uid
        target_gid = data_stat.st_gid
    else:
        target_uid = os.getuid()
        target_gid = os.getgid()

    # Set config dir itself to be owned by target user
    _chown_if_needed(config_dir, target_uid, target_gid)

    # lib/ tree: owner rwx on dirs, owner r-x on files (no write for anyone but root)
    for dirpath, _dirnames, filenames in os.walk(lib_dir):
        dp = Path(dirpath)
        _chown_if_needed(dp, target_uid, target_gid)
        dp.chmod(0o755)  # rwxr-xr-x
        for fname in filenames:
            fp = dp / fname
            _chown_if_needed(fp, target_uid, target_gid)
            # Preserve execute bit for binaries. Use lstat() so dangling
            # symlinks (which are valid, just unresolved) don't crash the
            # permission enforcer; skip chmod for symlinks since chmod()
            # would follow them and fail the same way.
            current = fp.lstat().st_mode
            if stat.S_ISLNK(current):
                continue
            if current & stat.S_IXUSR:
                fp.chmod(0o755)  # rwxr-xr-x (executable)
            else:
                fp.chmod(0o644)  # rw-r--r-- (non-executable)

    # Everything else under config_dir: writable by target user
    for entry in config_dir.iterdir():
        if entry.name == "lib":
            continue
        if entry.is_dir():
            for dirpath, _dirnames, filenames in os.walk(entry):
                dp = Path(dirpath)
                _chown_if_needed(dp, target_uid, target_gid)
                dp.chmod(0o755)
                for fname in filenames:
                    fp = dp / fname
                    _chown_if_needed(fp, target_uid, target_gid)
                    # Skip symlinks — chmod would follow them and raise on
                    # a dangling target, mirroring the lib/ tree walk above.
                    if stat.S_ISLNK(fp.lstat().st_mode):
                        continue
                    fp.chmod(0o644)
        elif entry.is_file():
            _chown_if_needed(entry, target_uid, target_gid)
            if stat.S_ISLNK(entry.lstat().st_mode):
                continue
            entry.chmod(0o644)


def _chown_if_needed(path: Path, uid: int, gid: int) -> None:
    """Change ownership only if it differs from target."""
    try:
        st = path.lstat()
        if st.st_uid != uid or st.st_gid != gid:
            os.lchown(str(path), uid, gid)
    except OSError:
        pass

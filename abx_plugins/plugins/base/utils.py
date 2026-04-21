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

import inspect
import json
import os
import stat
import sys
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, TextIO

from jambo import SchemaConverter
from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Shared config resolution
# ---------------------------------------------------------------------------

BASE_CONFIG_PATH = Path(__file__).with_name("config.json")
PLUGINS_DIR = BASE_CONFIG_PATH.parent.parent
PROCESS_EXIT_SKIPPED = 10


class ConfigSchemaDocument(BaseModel):
    title: str = "PluginConfig"
    description: str = ""
    output_mimetypes: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    required_plugins: list[str] = Field(default_factory=list)
    required_binaries: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("required_binaries", mode="before")
    @classmethod
    def validate_required_binaries(cls, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [
            item
            for item in value
            if isinstance(item, dict)
            and "name" in item
            and isinstance(item["name"], str)
            and item["name"]
        ]


ConfigSchemaDocument.model_rebuild()


def normalize_config_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [normalize_config_value(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_config_value(val) for key, val in value.items()}
    return value


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
        caller_file = inspect.stack()[stack_depth].filename
        return (Path(caller_file).parent / "config.json").resolve()
    return Path(config_path).resolve()


def _load_schema(path: Path) -> ConfigSchemaDocument:
    return ConfigSchemaDocument.model_validate_json(path.read_text())


def _collect_required_schema_paths(
    config_path: Path,
    seen: set[Path] | None = None,
) -> list[Path]:
    seen = seen or set()
    resolved = config_path.resolve()
    if resolved in seen:
        return []
    seen.add(resolved)

    schema = _load_schema(resolved)
    paths: list[Path] = []
    for required_plugin in schema.required_plugins:
        required_path = (PLUGINS_DIR / str(required_plugin) / "config.json").resolve()
        if required_path.exists():
            paths.extend(_collect_required_schema_paths(required_path, seen))
    paths.append(resolved)
    return paths


@lru_cache(maxsize=None)
def _build_merged_properties(config_path_str: str) -> tuple[str, dict[str, Any]]:
    config_path = Path(config_path_str)
    root_schema = _load_schema(config_path)
    properties: dict[str, Any] = {}
    paths = [BASE_CONFIG_PATH.resolve(), *_collect_required_schema_paths(config_path)]
    for path in paths:
        properties.update(_load_schema(path).properties)
    return root_schema.title, properties


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
        if key in schema:
            return key
        for canonical_key, prop in schema.items():
            aliases = prop["x-aliases"] if "x-aliases" in prop else []
            if key in aliases:
                return canonical_key
    return key


def _resolve_schema_payload(
    properties: dict[str, Any],
    *,
    resolved_config: dict[str, Any] | None = None,
    user_config: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environ = environ or os.environ
    resolved = {
        key: normalize_config_value(value)
        for key, value in (resolved_config or {}).items()
    }
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

            if "default" in prop and payload.get(key) != prop["default"]:
                payload[key] = prop["default"]
                resolved[key] = prop["default"]
                changed = True
                continue

            if key in resolved and payload.get(key) != resolved[key]:
                payload[key] = resolved[key]
                changed = True
        if not changed:
            break

    return payload


@lru_cache(maxsize=None)
def _schema_model(schema_json: str) -> type[Any]:
    model = SchemaConverter.build(json.loads(schema_json))
    model.model_config = ConfigDict(
        validate_assignment=True,
        use_enum_values=True,
        validate_default=True,
    )
    model.model_rebuild(force=True)
    return model


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

    for _ in range(max(len(plugin_schemas), 1) + 1):
        changed = False
        for plugin_name, schema in sorted(plugin_schemas.items()):
            payload = _resolve_schema_payload(
                schema,
                resolved_config=resolved_values,
                user_config=user_config,
                environ=environ,
            )
            model = _schema_model(
                json.dumps(
                    {
                        "title": plugin_name,
                        "type": "object",
                        "properties": schema,
                    },
                    sort_keys=True,
                ),
            )
            plugin_config = normalize_config_value(
                model.model_validate(payload).model_dump(mode="json"),
            )
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


def load_config(
    config_path: Path | str | None = None,
    *,
    global_config: Mapping[str, Any] | None = None,
    user_config: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Load typed plugin config using `jambo` plus `x-aliases` and `x-fallback`.

    The resolved config always includes shared `base/config.json` properties plus any
    `required_plugins` config it depends on. Values are resolved in this order:

    1. process environment
    2. explicit `user_config`
    3. `x-fallback`
    4. schema defaults
    """
    resolved_path = _resolve_config_path(config_path, stack_depth=2)
    title, properties = _build_merged_properties(str(resolved_path))
    payload = _resolve_schema_payload(
        properties,
        resolved_config=dict(global_config or {}),
        user_config=user_config,
        environ=environ,
    )
    model = _schema_model(
        json.dumps(
            {
                "title": title,
                "type": "object",
                "properties": properties,
            },
            sort_keys=True,
        ),
    )
    return model.model_validate(payload)


def get_config(
    config_path: Path | str | None = None,
    *,
    global_config: Mapping[str, Any] | None = None,
    user_config: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Alias for `load_config()` that preserves direct-caller config lookup."""
    if config_path is None:
        config_path = _resolve_config_path(None, stack_depth=2)
    return load_config(
        config_path,
        global_config=global_config,
        user_config=user_config,
        environ=environ,
    )


def _resolve_path(path_value: str) -> Path:
    return Path(path_value).expanduser().resolve()


def get_lib_dir() -> Path:
    """Return library directory.

    Priority: LIB_DIR env var, otherwise ~/.config/abx/lib.
    """
    config = load_config(BASE_CONFIG_PATH)
    return _resolve_path(str(config.LIB_DIR))


def get_personas_dir() -> Path:
    """Return personas directory.

    Returns the configured personas directory from load_config().
    """
    config = load_config(BASE_CONFIG_PATH)
    return Path(str(config.PERSONAS_DIR)).expanduser().resolve()


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

    config = load_config(BASE_CONFIG_PATH)
    env_raw = (config.EXTRA_CONTEXT or "").strip()
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
) -> None:
    """Output Binary JSONL record for a resolved dependency."""
    record: dict[str, Any] = {
        "type": "Binary",
        "name": name,
        "abspath": abspath,
        "version": version,
        "sha256": sha256,
        "binprovider": binprovider,
    }
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
                    fp.chmod(0o644)
        elif entry.is_file():
            _chown_if_needed(entry, target_uid, target_gid)
            entry.chmod(0o644)


def _chown_if_needed(path: Path, uid: int, gid: int) -> None:
    """Change ownership only if it differs from target."""
    try:
        st = path.lstat()
        if st.st_uid != uid or st.st_gid != gid:
            os.lchown(str(path), uid, gid)
    except OSError:
        pass

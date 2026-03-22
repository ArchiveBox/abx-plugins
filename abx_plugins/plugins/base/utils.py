"""Shared utilities for abx plugins.

Provides common helpers used across multiple plugins:
- Config loading from config.json using PydanticSettings (load_config)
- Environment variable parsing (get_env, get_env_bool, get_env_int, get_env_array)
- JSONL record emission (emit_archive_result_record, emit_binary_record, emit_machine_record, emit_snapshot_record)
- Atomic file writing (write_text_atomic)
- HTML source discovery (find_html_source)
- Sibling plugin output checking (has_staticfile_output)

Import directly via the package path::

    from abx_plugins.plugins.base.utils import load_config
"""

from __future__ import annotations

import inspect
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Config loading from config.json using PydanticSettings
# ---------------------------------------------------------------------------

# Cache: config_path -> (model_class, (plugin_schema_mtime, base_schema_mtime))
# The model class is reused across calls to avoid re-parsing the JSON schema
# and re-creating the Pydantic model on every call.  A fresh *instance* is
# returned each time so that environment variable changes are picked up.
BASE_CONFIG_PATH = Path(__file__).with_name("config.json")
_config_model_cache: dict[str, tuple[type, tuple[float, float]]] = {}


def _normalize_schema_type(prop: dict[str, Any]) -> tuple[str, bool]:
    schema_type = prop.get("type", "string")
    nullable = False

    if isinstance(schema_type, list):
        nullable = "null" in schema_type
        non_null_types = [item for item in schema_type if item != "null"]
        schema_type = non_null_types[0] if non_null_types else "string"

    return str(schema_type), nullable


def load_config(config_path: Path | str | None = None) -> Any:
    """Load plugin config from config.json using PydanticSettings.

    Reads the JSON Schema config file and creates a BaseSettings model that
    auto-resolves environment variables, x-aliases, and x-fallback values.
    The shared `base/config.json` properties are merged into every plugin
    config so common runtime keys like `SNAP_DIR`, `CRAWL_DIR`, `LIB_DIR`,
    `PERSONAS_DIR`, and `EXTRA_CONTEXT` are always available.

    The model *class* is cached per config_path (keyed by resolved absolute
    path) so repeated calls within the same plugin avoid redundant schema
    parsing and ``create_model()`` overhead.  A new *instance* is created on
    every call so that env var changes between calls are always reflected.

    Args:
        config_path: Path to config.json. If None, auto-detects from the
                     **direct caller's** directory using ``inspect.stack()``.
                     Only pass None when calling from a top-level hook script
                     that lives next to its own config.json.  Helpers or
                     wrappers that call ``load_config()`` on behalf of a
                     plugin must pass the path explicitly.

    Returns:
        A PydanticSettings instance with typed, validated config values.
        Field names match the env var names from config.json (e.g. config.WGET_TIMEOUT).

    Example::

        config = load_config()
        timeout = config.WGET_TIMEOUT      # int, auto-resolved with x-fallback
        enabled = config.WGET_ENABLED       # bool, auto-resolved with x-aliases
        args = config.WGET_ARGS             # list[str], parsed from JSON env var
    """
    from pydantic import AliasChoices, Field, create_model
    from pydantic_settings import BaseSettings, SettingsConfigDict

    # Resolve config_path -------------------------------------------------
    # When config_path is None we walk up one frame to find the caller's
    # directory.  This is safe because every hook script lives alongside its
    # own config.json and calls load_config() directly (never via a shared
    # helper that would add extra stack frames).
    if config_path is None:
        caller_file = inspect.stack()[1].filename
        config_path = Path(caller_file).parent / "config.json"
    else:
        config_path = Path(config_path)

    config_path = config_path.resolve()
    base_config_path = BASE_CONFIG_PATH.resolve()
    cache_key = str(config_path)

    # Check cache ----------------------------------------------------------
    schema_mtimes = (
        config_path.stat().st_mtime,
        base_config_path.stat().st_mtime,
    )
    cached = _config_model_cache.get(cache_key)
    if cached is not None:
        model_cls, cached_mtimes = cached
        if cached_mtimes == schema_mtimes:
            return model_cls()  # fresh instance picks up env changes

    # Build model class ----------------------------------------------------
    base_schema = json.loads(base_config_path.read_text())
    base_properties = base_schema.get("properties", {})

    schema = json.loads(config_path.read_text())
    if config_path == base_config_path:
        properties = base_properties
    else:
        properties = {**base_properties, **schema.get("properties", {})}

    if not properties:

        class _EmptyConfig(BaseSettings):
            model_config = SettingsConfigDict(extra="ignore")

        _config_model_cache[cache_key] = (_EmptyConfig, schema_mtimes)
        return _EmptyConfig()

    JSON_TYPE_MAP: dict[str, type] = {
        "boolean": bool,
        "string": str,
        "integer": int,
        "number": float,
    }

    field_definitions: dict[str, Any] = {}
    for name, prop in properties.items():
        schema_type, nullable = _normalize_schema_type(prop)
        if schema_type == "array":
            python_type: Any = list
        else:
            python_type = JSON_TYPE_MAP.get(schema_type, str)
        if nullable:
            python_type = python_type | None

        default = prop.get("default")

        # Build alias choices: primary name > x-aliases > x-fallback
        choices = [name]
        choices.extend(prop.get("x-aliases", []))
        if fallback := prop.get("x-fallback"):
            choices.append(fallback)

        field_definitions[name] = (
            python_type,
            Field(default=default, validation_alias=AliasChoices(*choices)),
        )

    class _ConfigBase(BaseSettings):
        model_config = SettingsConfigDict(extra="ignore")

    model_cls = create_model("PluginConfig", __base__=_ConfigBase, **field_definitions)
    _config_model_cache[cache_key] = (model_cls, schema_mtimes)
    return model_cls()


# ---------------------------------------------------------------------------
# Environment variable helpers
# ---------------------------------------------------------------------------


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def get_env_bool(name: str, default: bool = False) -> bool:
    val = get_env(name, "").lower()
    if val in ("true", "1", "yes", "on"):
        return True
    if val in ("false", "0", "no", "off"):
        return False
    return default


def get_env_int(name: str, default: int = 0) -> int:
    try:
        return int(get_env(name, str(default)))
    except ValueError:
        return default


def get_env_array(name: str, default: list[str] | None = None) -> list[str]:
    """Parse a JSON array from environment variable."""
    val = get_env(name, "")
    if not val:
        return default if default is not None else []
    try:
        result = json.loads(val)
        if isinstance(result, list):
            return [str(item) for item in result]
        return default if default is not None else []
    except json.JSONDecodeError:
        return default if default is not None else []


def resolve_binary_path(binary: str, PATH: str | None = None) -> str | None:
    """Resolve an executable name or absolute path using abx-pkg path rules."""
    if not binary:
        return None
    from abx_pkg import bin_abspath

    resolved = bin_abspath(binary, PATH=PATH)
    return str(resolved) if resolved else None


def _resolve_path(path_value: str) -> Path:
    return Path(path_value).expanduser().resolve()


def get_lib_dir() -> Path:
    """Return library directory.

    Priority: LIB_DIR env var, otherwise ~/.config/abx/lib.
    """
    config = load_config(BASE_CONFIG_PATH)
    lib_dir = (config.LIB_DIR or "").strip()
    if lib_dir:
        return _resolve_path(lib_dir)
    return _resolve_path(str(Path.home() / ".config" / "abx" / "lib"))


def get_personas_dir() -> Path:
    """Return personas directory.

    Priority: PERSONAS_DIR env var, otherwise ~/.config/abx/personas.
    """
    config = load_config(BASE_CONFIG_PATH)
    personas_dir = (config.PERSONAS_DIR or "").strip()
    if personas_dir:
        return _resolve_path(personas_dir)
    return _resolve_path(str(Path.home() / ".config" / "abx" / "personas"))


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


def _write_stream_line_fully(stream: Any, text: str) -> None:
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

    encoding = getattr(stream, "encoding", None) or "utf-8"
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


def _merge_extra_context(record: dict[str, Any]) -> dict[str, Any]:
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
    _write_stream_line_fully(sys.stdout, json.dumps(_merge_extra_context(record)))


def emit_binary_record(
    name: str,
    binproviders: str | None = None,
    overrides: dict[str, Any] | None = None,
    min_version: str | None = None,
    abspath: str | None = None,
    version: str | None = None,
    sha256: str | None = None,
    binprovider: str | None = None,
) -> None:
    """Output Binary JSONL record for a dependency."""
    record: dict[str, Any] = {
        "type": "Binary",
        "name": name,
    }
    if binproviders is not None:
        record["binproviders"] = binproviders
    if overrides:
        record["overrides"] = overrides
    if min_version:
        record["min_version"] = min_version
    if abspath is not None:
        record["abspath"] = abspath
    if version is not None:
        record["version"] = version
    if sha256 is not None:
        record["sha256"] = sha256
    if binprovider is not None:
        record["binprovider"] = binprovider
    _write_stream_line_fully(sys.stdout, json.dumps(_merge_extra_context(record)))


def emit_machine_record(config: dict[str, Any]) -> None:
    _write_stream_line_fully(
        sys.stdout,
        json.dumps(
            _merge_extra_context(
                {
                    "type": "Machine",
                    "config": config,
                },
            ),
        ),
    )


def emit_snapshot_record(record: dict[str, Any]) -> None:
    snapshot_record = _merge_extra_context(
        {
            "type": "Snapshot",
            **{key: value for key, value in record.items() if key != "type"},
        },
    )
    _write_stream_line_fully(sys.stdout, json.dumps(snapshot_record))


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


def find_html_source() -> str | None:
    """Find HTML content from other extractors in the snapshot directory."""
    search_patterns = [
        "singlefile/singlefile.html",
        "*_singlefile/singlefile.html",
        "singlefile/*.html",
        "*_singlefile/*.html",
        "dom/output.html",
        "*_dom/output.html",
        "dom/*.html",
        "*_dom/*.html",
        "wget/**/*.html",
        "*_wget/**/*.html",
        "wget/**/*.htm",
        "*_wget/**/*.htm",
    ]

    for base in (Path.cwd(), Path.cwd().parent):
        for pattern in search_patterns:
            for match in base.glob(pattern):
                if match.is_file() and match.stat().st_size > 0:
                    return str(match)

    return None


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

    When running as root (e.g. during crawl/install hooks), this function
    sets ownership and permissions on the config directory so that:
      - lib/ and its contents are read+execute only (0o755 dirs, 0o644 files)
        for the data dir owner, preventing snapshot hooks from modifying
        installed binaries or node_modules
      - Everything else under ~/.config/abx (personas, etc.) is writable by
        the data dir owner

    This should be called at the end of crawl/install hooks that modify lib/.
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
            # Preserve execute bit for binaries
            current = fp.stat().st_mode
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

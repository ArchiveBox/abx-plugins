"""Shared utilities for abx plugins.

Provides common helpers used across multiple plugins:
- Config loading from config.json using PydanticSettings (load_config)
- Environment variable parsing (get_env, get_env_bool, get_env_int, get_env_array)
- JSONL record emission (emit_archive_result, output_binary, output_machine_config)
- Atomic file writing (write_text_atomic)
- HTML source discovery (find_html_source)
- Sibling plugin output checking (has_staticfile_output)
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Config loading from config.json using PydanticSettings
# ---------------------------------------------------------------------------

def load_config(config_path: Path | str | None = None) -> Any:
    """Load plugin config from config.json using PydanticSettings.

    Reads the JSON Schema config file and creates a BaseSettings model that
    auto-resolves environment variables, x-aliases, and x-fallback values.

    Falls back to a simple namespace-based loader if pydantic-settings is
    unavailable (e.g. in minimal environments).

    Args:
        config_path: Path to config.json. If None, auto-detects from the
                     caller's plugin directory.

    Returns:
        An object with typed config values as attributes.
        Field names match the env var names from config.json (e.g. config.WGET_TIMEOUT).

    Example::

        config = load_config()
        timeout = config.WGET_TIMEOUT      # int, auto-resolved with x-fallback
        enabled = config.WGET_ENABLED       # bool, auto-resolved with x-aliases
        args = config.WGET_ARGS             # list[str], parsed from JSON env var
    """
    if config_path is None:
        caller_file = inspect.stack()[1].filename
        config_path = Path(caller_file).parent / "config.json"
    else:
        config_path = Path(config_path)

    schema = json.loads(config_path.read_text())
    properties = schema.get("properties", {})

    try:
        return _load_config_pydantic(properties)
    except Exception:
        return _load_config_fallback(properties)


def _load_config_pydantic(properties: dict[str, Any]) -> Any:
    """Load config using PydanticSettings (preferred, with full validation)."""
    from pydantic import AliasChoices, Field, create_model
    from pydantic_settings import BaseSettings, SettingsConfigDict

    if not properties:
        class _EmptyConfig(BaseSettings):
            model_config = SettingsConfigDict(extra="ignore")
        return _EmptyConfig()

    JSON_TYPE_MAP: dict[str, type] = {
        "boolean": bool,
        "string": str,
        "integer": int,
        "number": float,
    }

    field_definitions: dict[str, Any] = {}
    for name, prop in properties.items():
        schema_type = prop.get("type", "string")
        if schema_type == "array":
            python_type: type = list
        else:
            python_type = JSON_TYPE_MAP.get(schema_type, str)

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

    return create_model("PluginConfig", __base__=_ConfigBase, **field_definitions)()


def _load_config_fallback(properties: dict[str, Any]) -> Any:
    """Fallback config loader using simple env var parsing (no pydantic needed)."""

    ENV_PARSERS: dict[str, Any] = {
        "boolean": lambda name, default: get_env_bool(name, default if default is not None else False),
        "string": lambda name, default: get_env(name, default if default is not None else ""),
        "integer": lambda name, default: get_env_int(name, default if default is not None else 0),
        "number": lambda name, default: float(get_env(name, str(default if default is not None else 0))),
        "array": lambda name, default: get_env_array(name, default if default is not None else []),
    }

    values: dict[str, Any] = {}
    for name, prop in properties.items():
        schema_type = prop.get("type", "string")
        default = prop.get("default")
        parser = ENV_PARSERS.get(schema_type, ENV_PARSERS["string"])

        # Try primary name first
        val = os.environ.get(name)
        if val is not None:
            values[name] = parser(name, default)
            continue

        # Try x-aliases
        resolved = False
        for alias in prop.get("x-aliases", []):
            if os.environ.get(alias) is not None:
                values[name] = parser(alias, default)
                resolved = True
                break
        if resolved:
            continue

        # Try x-fallback
        fallback = prop.get("x-fallback")
        if fallback and os.environ.get(fallback) is not None:
            values[name] = parser(fallback, default)
            continue

        # Use default
        values[name] = default

    # Return as a simple namespace object with attribute access
    return type("PluginConfig", (), values)()


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


# ---------------------------------------------------------------------------
# JSONL record emission
# ---------------------------------------------------------------------------

def emit_archive_result(status: str, output_str: str) -> None:
    print(
        json.dumps(
            {
                "type": "ArchiveResult",
                "status": status,
                "output_str": output_str,
            }
        )
    )


def output_binary(
    name: str, binproviders: str, overrides: dict[str, Any] | None = None
) -> None:
    """Output Binary JSONL record for a dependency."""
    machine_id = os.environ.get("MACHINE_ID", "")

    record: dict[str, Any] = {
        "type": "Binary",
        "name": name,
        "binproviders": binproviders,
        "machine_id": machine_id,
    }
    if overrides:
        record["overrides"] = overrides
    print(json.dumps(record))


def output_machine_config(config: dict) -> None:
    """Output Machine config JSONL patch."""
    if not config:
        return
    record = {
        "type": "Machine",
        "config": config,
    }
    print(json.dumps(record))


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

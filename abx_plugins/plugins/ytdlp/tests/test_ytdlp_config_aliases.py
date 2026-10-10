"""
Unit tests for ytdlp config schema aliases (ArchiveBox/ArchiveBox#1613).

The pre-plugin-refactor env var name ``YOUTUBEDL_ARGS`` and the current
docs name ``YTDLP_EXTRA_ARGS`` must both resolve to the canonical
``YTDLP_ARGS_EXTRA`` key, and env vars must take precedence over persisted
config values.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from abx_plugins.plugins.base.utils import (
    _resolve_schema_payload,
    _schema_properties,
    resolve_alias,
)

PLUGIN_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PLUGIN_DIR / "config.json"


def _ytdlp_schema() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _properties() -> dict[str, Any]:
    return _schema_properties(_ytdlp_schema())


def _resolve(
    env: dict[str, str],
    user_config: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _resolve_schema_payload(
        _properties(),
        resolved_config={},
        explicit_config_keys=None,
        user_config=user_config or {},
        environ=env,
    )


def test_ytdlp_args_extra_declares_legacy_aliases() -> None:
    prop = _properties()["YTDLP_ARGS_EXTRA"]
    assert isinstance(prop, dict)
    aliases = prop.get("x-aliases") or []
    assert "YTDLP_EXTRA_ARGS" in aliases
    assert "YOUTUBEDL_ARGS" in aliases


def test_resolve_alias_maps_legacy_and_current_names() -> None:
    schemas = {"ytdlp": _ytdlp_schema()}
    assert resolve_alias("YOUTUBEDL_ARGS", schemas) == "YTDLP_ARGS_EXTRA"
    assert resolve_alias("YTDLP_EXTRA_ARGS", schemas) == "YTDLP_ARGS_EXTRA"
    assert resolve_alias("YTDLP_ARGS_EXTRA", schemas) == "YTDLP_ARGS_EXTRA"


def test_legacy_env_var_reaches_canonical_key() -> None:
    result = _resolve({"YOUTUBEDL_ARGS": '["--restrict-filenames"]'})
    assert result.get("YTDLP_ARGS_EXTRA") == ["--restrict-filenames"]


def test_current_alias_env_var_reaches_canonical_key() -> None:
    result = _resolve({"YTDLP_EXTRA_ARGS": '["--limit-rate=10M"]'})
    assert result.get("YTDLP_ARGS_EXTRA") == ["--limit-rate=10M"]


def test_canonical_env_var_reaches_canonical_key() -> None:
    result = _resolve({"YTDLP_ARGS_EXTRA": '["--geo-bypass"]'})
    assert result.get("YTDLP_ARGS_EXTRA") == ["--geo-bypass"]


def test_canonical_wins_when_multiple_names_set() -> None:
    result = _resolve(
        {
            "YOUTUBEDL_ARGS": '["--old"]',
            "YTDLP_EXTRA_ARGS": '["--alias"]',
            "YTDLP_ARGS_EXTRA": '["--canon"]',
        }
    )
    assert result.get("YTDLP_ARGS_EXTRA") == ["--canon"]


def test_env_var_beats_persisted_config() -> None:
    result = _resolve(
        {"YOUTUBEDL_ARGS": '["--env-wins"]'},
        user_config={"YTDLP_ARGS_EXTRA": '["--persisted"]'},
    )
    assert result.get("YTDLP_ARGS_EXTRA") == ["--env-wins"]


def test_persisted_config_used_without_env() -> None:
    result = _resolve({}, user_config={"YTDLP_ARGS_EXTRA": '["--persisted"]'})
    assert result.get("YTDLP_ARGS_EXTRA") == ["--persisted"]


def test_default_is_empty_list_without_env_or_config() -> None:
    result = _resolve({})
    assert result.get("YTDLP_ARGS_EXTRA") == []

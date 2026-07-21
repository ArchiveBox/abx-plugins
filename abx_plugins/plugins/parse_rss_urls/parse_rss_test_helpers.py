from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from abx_plugins.plugins.base.testing import install_required_binary_from_config
from abxpkg import BinProvider

PLUGIN_DIR = Path(__file__).parent
_PARSE_RSS_URLS_PROVIDER: BinProvider | None = None


def parse_rss_urls_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    global _PARSE_RSS_URLS_PROVIDER
    env = dict(os.environ if base_env is None else base_env)
    if _PARSE_RSS_URLS_PROVIDER is None:
        loaded = install_required_binary_from_config(
            PLUGIN_DIR,
            "feedparser",
            env=env,
        )
        assert loaded.loaded_binprovider is not None
        _PARSE_RSS_URLS_PROVIDER = loaded.loaded_binprovider
    return BinProvider.build_exec_env(
        providers=[_PARSE_RSS_URLS_PROVIDER],
        base_env=env,
    )


def run_parse_rss_urls(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    kwargs["env"] = parse_rss_urls_env(kwargs.get("env"))
    return subprocess.run(*args, **kwargs)

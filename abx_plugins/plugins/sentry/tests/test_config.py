from pathlib import Path
import os
import subprocess
import textwrap

import pytest
from pydantic import ValidationError

from abx_plugins.plugins.base.utils import load_config


CONFIG = Path(__file__).parents[1] / "config.json"


def test_runtime_loads_sdk_from_configured_library_root(tmp_path):
    result = subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "--with",
            "django",
            "python",
            "-c",
            textwrap.dedent("""
                import sys
                from pathlib import Path
                from abx_plugins.plugins.base.utils import load_config, load_required_binary_from_config
                from abx_plugins.plugins.sentry.runtime import CONFIG_PATH, initialize

                library = Path(sys.argv[1])
                settings = {
                    "ABXPKG_LIB_DIR": str(library),
                    "SENTRY_ENABLED": True,
                    "SENTRY_DSN": "http://public@127.0.0.1:1/1",
                }
                sdk = load_required_binary_from_config(
                    "sentry_sdk", CONFIG_PATH, global_config=settings, environ={}, install=True,
                )
                assert str(sdk.loaded_version) == "2.75.0"
                assert sdk.loaded_abspath.is_relative_to(library)
                config = load_config(CONFIG_PATH, global_config=settings, environ={}, hydrate_binaries=False)
                initialize(config)
                import sentry_sdk
                assert Path(sentry_sdk.__file__).is_relative_to(library), sentry_sdk.__file__
                assert sentry_sdk.VERSION == "2.75.0"
                sentry_sdk.get_client().close()
            """),
            str(tmp_path / "configured"),
        ],
        env={**os.environ, "ABXPKG_LIB_DIR": str(tmp_path / "ambient")},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_sentry_is_unconfigured_and_disabled_by_default():
    config = load_config(CONFIG, environ={}, hydrate_binaries=False)
    assert config.SENTRY_ENABLED is False
    assert config.SENTRY_DSN == ""
    assert config.SENTRY_TRACES_SAMPLE_RATE == 0
    assert config.SENTRY_PROFILE_SESSION_SAMPLE_RATE == 0
    assert config.SENTRY_SEND_DEFAULT_PII is False


def test_dsn_alone_does_not_enable_sentry():
    config = load_config(
        CONFIG,
        environ={"SENTRY_DSN": "http://public@localhost/1"},
        hydrate_binaries=False,
    )
    assert config.SENTRY_ENABLED is False


@pytest.mark.parametrize("rate", ["-0.1", "1.1"])
@pytest.mark.parametrize(
    "setting",
    ["SENTRY_TRACES_SAMPLE_RATE", "SENTRY_PROFILE_SESSION_SAMPLE_RATE"],
)
def test_invalid_sampling_is_rejected(setting, rate):
    with pytest.raises(ValidationError):
        load_config(
            CONFIG,
            environ={setting: rate},
            hydrate_binaries=False,
        )

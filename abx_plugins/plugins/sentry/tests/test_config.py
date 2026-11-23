from pathlib import Path

import pytest
from pydantic import ValidationError

from abx_plugins.plugins.base.utils import load_config


CONFIG = Path(__file__).parents[1] / "config.json"


def test_sentry_is_unconfigured_and_disabled_by_default():
    config = load_config(CONFIG, environ={}, hydrate_binaries=False)
    assert config.SENTRY_ENABLED is False
    assert config.SENTRY_DSN == ""
    assert config.SENTRY_TRACES_SAMPLE_RATE == 0
    assert config.SENTRY_SEND_DEFAULT_PII is False


def test_dsn_alone_does_not_enable_sentry():
    config = load_config(
        CONFIG,
        environ={"SENTRY_DSN": "http://public@localhost/1"},
        hydrate_binaries=False,
    )
    assert config.SENTRY_ENABLED is False


@pytest.mark.parametrize("rate", ["-0.1", "1.1"])
def test_invalid_trace_sampling_is_rejected(rate):
    with pytest.raises(ValidationError):
        load_config(
            CONFIG,
            environ={"SENTRY_TRACES_SAMPLE_RATE": rate},
            hydrate_binaries=False,
        )

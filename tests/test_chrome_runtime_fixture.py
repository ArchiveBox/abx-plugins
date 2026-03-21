from __future__ import annotations

import conftest

import pytest
from _pytest.outcomes import Failed

from abx_pkg import Binary


def test_require_chrome_runtime_loads_node_and_npm(monkeypatch: pytest.MonkeyPatch):
    """Fixture should force actual binary resolution, not just construct providers."""
    loaded: list[str] = []

    def fake_load(self: Binary, *args, **kwargs):
        loaded.append(self.name)
        return self

    monkeypatch.setattr(Binary, "load", fake_load)

    conftest.require_chrome_runtime_impl()

    assert loaded == ["node", "npm"]


def test_require_chrome_runtime_fails_when_binary_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """Fixture should fail fast when a required runtime binary cannot be loaded."""

    def fake_load(self: Binary, *args, **kwargs):
        raise Exception(f"{self.name} missing")

    monkeypatch.setattr(Binary, "load", fake_load)
    caplog.set_level("ERROR")

    with pytest.raises(Failed, match="Chrome integration prerequisites unavailable: node missing") as excinfo:
        conftest.require_chrome_runtime_impl()

    assert caplog.messages == [
        "Chrome integration prerequisites unavailable: node missing"
    ]
    assert excinfo.value.pytrace is False

"""Pytest startup hooks that keep test runs from polluting the repo root."""

from __future__ import annotations

import sys


# Pytest loads `-p` plugins before importing shared fixture plugins, so
# this disables bytecode writes before `conftest.pyc` can land in `./__pycache__`.
sys.dont_write_bytecode = True


def test_pytest_bootstrap_disables_bytecode_writes():
    assert sys.dont_write_bytecode is True

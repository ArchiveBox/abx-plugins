"""Pytest startup hooks that keep test runs from polluting the repo root."""

from __future__ import annotations

import sys


# Pytest loads `-p` plugins before importing the repo root conftest.py, so
# this disables bytecode writes before `conftest.pyc` can land in `./__pycache__`.
sys.dont_write_bytecode = True

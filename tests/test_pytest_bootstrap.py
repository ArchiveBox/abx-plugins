"""Pytest startup hooks that keep test runs from polluting the repo root."""

from __future__ import annotations

import sys
import os
from pathlib import Path
import subprocess
import runpy
from xml.etree import ElementTree

import pytest


# Pytest loads `-p` plugins before importing shared fixture plugins, so
# this disables bytecode writes before `conftest.pyc` can land in `./__pycache__`.
sys.dont_write_bytecode = True


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    if (directory := os.environ.get("ABX_CI_TEST_RESULTS_DIR")) and not hasattr(
        config,
        "workerinput",
    ):
        reports = Path(directory)
        reports.mkdir(parents=True, exist_ok=True)
        config.option.xmlpath = str(reports / f"pytest-{os.getpid()}.xml")


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("ABX_CI_TEST_RESULTS_DIR") and not hasattr(
        session.config,
        "workerinput",
    ):
        classifier = (
            Path(__file__).resolve().parent.parent / ".github/ci_test_status.py"
        )
        session.exitstatus = runpy.run_path(str(classifier))["test_status"](
            Path(session.config.option.xmlpath),
            exitstatus,
        )


def test_pytest_bootstrap_disables_bytecode_writes():
    assert sys.dont_write_bytecode is True


def test_nested_docs_runs_preserve_separate_reports_and_real_failures(tmp_path):
    root = Path(__file__).resolve().parent.parent
    missing = tmp_path / "missing.txt"
    test_file = tmp_path / "test_required_file.py"
    test_file.write_text(
        "from pathlib import Path\n"
        "def test_required_file():\n"
        f"    Path({str(missing)!r}).read_text()\n",
    )
    readme = tmp_path / "README.md"
    readme.write_text(
        "```sh\n"
        f"uv run --no-sync --no-sources pytest -c '{root / 'pyproject.toml'}' '{test_file}' -q\n"
        "```\n",
    )
    reports = tmp_path / "reports"
    result = subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "--no-sources",
            "pytest",
            "-c",
            str(root / "pyproject.toml"),
            str(readme),
            "-q",
        ],
        cwd=root,
        env={**os.environ, "ABX_CI_TEST_RESULTS_DIR": str(reports)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    files = list(reports.glob("*.xml"))
    assert len(files) == 2
    failures = [
        failure for path in files for failure in ElementTree.parse(path).iter("failure")
    ]
    assert len(failures) == 2
    assert any(
        "FileNotFoundError" in failure.get("message", "") for failure in failures
    )

import json
import os
import subprocess
import sys
from pathlib import Path

from abx_plugins.plugins.base.utils import (
    abxpkg_native_overrides,
    emit_archive_result_record,
    emit_installed_binary_record,
)
from abx_plugins.plugins.base.testing import install_binary_with_abxpkg

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_python_emit_archive_result_merges_extra_context_from_cli(capfd, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["hook.py", '--extra-context={"snapshot_id":"snap-123","status":"ignored"}'],
    )

    emit_archive_result_record("succeeded", "ok")

    stdout, _stderr = capfd.readouterr()
    record = json.loads(stdout.strip())
    assert record["type"] == "ArchiveResult"
    assert record["status"] == "succeeded"
    assert record["output_str"] == "ok"
    assert record["snapshot_id"] == "snap-123"


def test_python_emit_installed_binary_record_merges_extra_context_from_env(
    capfd,
    monkeypatch,
):
    monkeypatch.setenv(
        "EXTRA_CONTEXT",
        json.dumps(
            {
                "machine_id": "machine-123",
                "binary_id": "binary-123",
                "plugin_name": "test-plugin",
                "hook_name": "test-hook",
            },
        ),
    )
    monkeypatch.setattr(sys, "argv", ["hook.py"])

    emit_installed_binary_record(
        name="rg",
        binprovider="env",
        abspath="/usr/bin/rg",
        version="1.0.0",
        sha256="deadbeef",
    )

    stdout, _stderr = capfd.readouterr()
    record = json.loads(stdout.strip())
    assert record["type"] == "Binary"
    assert record["name"] == "rg"
    assert record["binprovider"] == "env"
    assert record["abspath"] == "/usr/bin/rg"
    assert record["machine_id"] == "machine-123"
    assert record["binary_id"] == "binary-123"
    assert record["plugin_name"] == "test-plugin"
    assert record["hook_name"] == "test-hook"


def test_abxpkg_native_overrides_omits_plugin_metadata():
    assert abxpkg_native_overrides(
        {
            "pip": {
                "install_args": ["imagesize>=2.0.0"],
                "module_name": "imagesize",
            },
        },
    ) == {"pip": {"install_args": ["imagesize>=2.0.0"]}}


def test_js_emit_snapshot_record_merges_extra_context_from_env():
    env = os.environ.copy()
    env["EXTRA_CONTEXT"] = json.dumps({"id": "snap-999"})
    node = install_binary_with_abxpkg("node", binproviders="env,apt,brew")

    result = subprocess.run(
        [
            str(node.loaded_abspath),
            "-e",
            (
                "const { emitSnapshotRecord } = require('./abx_plugins/plugins/base/utils.js');"
                "emitSnapshotRecord({ title: 'Example Title' });"
            ),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    record = json.loads(result.stdout.strip())
    assert record["type"] == "Snapshot"
    assert record["id"] == "snap-999"
    assert record["title"] == "Example Title"

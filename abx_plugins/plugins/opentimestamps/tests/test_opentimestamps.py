"""Real hooks, real TLSNotary evidence, and real OpenTimestamps calendars."""

import hashlib
import json
import os
import shutil
import socket
from pathlib import Path

from abx_plugins.plugins.base.testing import get_hook_script, run_hook_and_parse

PLUGINS = Path(__file__).resolve().parents[2]


def run_plugin(name: str, snap: Path, **config: str):
    hook = get_hook_script(PLUGINS / name, "on_Snapshot__*")
    assert hook is not None
    code, record, stderr = run_hook_and_parse(
        hook,
        "https://news.ycombinator.com/",
        None,
        cwd=snap,
        env={**os.environ, "SNAP_DIR": str(snap), **config},
        timeout=90,
    )
    assert record is not None, stderr
    return code, record, stderr


def capture_files(snap):
    # Preserve an actual signed capture, including the same symlink layout as TLSNotary.
    target = snap / "tlsnotary" / "capture-fixture"
    shutil.copytree(PLUGINS / "tlsnotary/tests/fixtures/hacker-news", target)
    (target.parent / "current").symlink_to(target.name, target_is_directory=True)


def test_evidence_hooks_run_in_order():
    hooks = [
        get_hook_script(PLUGINS / name, "on_Snapshot__*")
        for name in ("tlsnotary", "hashes", "opentimestamps")
    ]
    assert all(hooks)
    names = [hook.name for hook in hooks if hook is not None]
    assert names == sorted(names)
    assert all(".bg." not in name for name in names)


def test_hashes_publishes_completion_and_covers_tlsnotary(tmp_path):
    capture_files(tmp_path)
    code, record, stderr = run_plugin("hashes", tmp_path, HASHES_ENABLED="true")
    assert code == 0, stderr
    assert record["status"] == "succeeded"
    manifest = (tmp_path / "hashes/hashes.json").read_bytes()
    assert (tmp_path / "hashes/hashes.sha256").read_text().strip() == hashlib.sha256(
        manifest,
    ).hexdigest()
    data = json.loads(manifest)
    for item in data["files"]:
        assert (
            item["hash"]
            == hashlib.sha256((tmp_path / item["path"]).read_bytes()).hexdigest()
        )
    paths = {item["path"] for item in data["files"]}
    assert {
        "tlsnotary/capture-fixture/receipt.json",
        "tlsnotary/capture-fixture/response.http",
    } <= paths
    # Timestamp output must not recursively enter future Merkle trees.
    shutil.copytree(tmp_path / "tlsnotary", tmp_path / "opentimestamps", symlinks=True)
    code, record, stderr = run_plugin("hashes", tmp_path, HASHES_ENABLED="true")
    assert code == 0, stderr
    assert (
        json.loads((tmp_path / "hashes/hashes.json").read_bytes())["root_hash"]
        == data["root_hash"]
    )
    code, record, stderr = run_plugin("hashes", tmp_path, HASHES_ENABLED="false")
    assert code == 0 and record["status"] == "skipped", stderr
    assert not (tmp_path / "hashes/hashes.sha256").exists()


def test_disabled_and_missing_or_unfinished_hashes(tmp_path):
    code, record, stderr = run_plugin(
        "opentimestamps",
        tmp_path,
        OPENTIMESTAMPS_ENABLED="false",
    )
    assert code == 0 and record["status"] == "skipped", stderr
    assert not (tmp_path / "opentimestamps/current").exists()
    code, record, stderr = run_plugin(
        "opentimestamps",
        tmp_path,
        OPENTIMESTAMPS_ENABLED="true",
    )
    assert code != 0 and record["status"] == "failed", stderr
    assert "hashes.sha256" in record["output_str"]
    capture_files(tmp_path)
    code, record, stderr = run_plugin("hashes", tmp_path, HASHES_ENABLED="true")
    assert code == 0, stderr
    (tmp_path / "hashes/hashes.json").write_text('{"root_hash":')
    code, record, stderr = run_plugin(
        "opentimestamps",
        tmp_path,
        OPENTIMESTAMPS_ENABLED="true",
    )
    assert code != 0 and record["status"] == "failed", stderr
    assert "completion checksum" in record["output_str"]
    assert not (tmp_path / "opentimestamps/current").exists()


def test_hash_failure_invalidates_completion_and_preserves_manifest(tmp_path):
    file = tmp_path / "output.txt"
    file.write_text("real archived bytes")
    code, record, stderr = run_plugin("hashes", tmp_path, HASHES_ENABLED="true")
    assert code == 0, stderr
    manifest = (tmp_path / "hashes/hashes.json").read_bytes()
    # Opening a real socket as a regular file fails even when tests run as root.
    # Bind a relative path to stay within the Unix socket pathname length limit.
    with socket.socket(socket.AF_UNIX) as unreadable:
        original_cwd = Path.cwd()
        try:
            os.chdir(tmp_path)
            unreadable.bind("unreadable.sock")
        finally:
            os.chdir(original_cwd)
        code, record, stderr = run_plugin("hashes", tmp_path, HASHES_ENABLED="true")
        assert code != 0 and record["status"] == "failed", stderr
        assert not (tmp_path / "hashes/hashes.sha256").exists()
        assert (tmp_path / "hashes/hashes.json").read_bytes() == manifest


def test_live_stamp_and_failed_rerun_preserves_evidence(tmp_path):
    import subprocess

    from abx_plugins.plugins.base.testing import install_required_binary_from_config

    binary = install_required_binary_from_config(PLUGINS / "opentimestamps", "ots")
    assert binary.abspath
    config = {
        "OPENTIMESTAMPS_ENABLED": "true",
        "OPENTIMESTAMPS_BINARY": str(binary.abspath),
        "HASHES_ENABLED": "true",
    }
    capture_files(tmp_path)
    code, record, stderr = run_plugin("hashes", tmp_path, HASHES_ENABLED="true")
    assert code == 0, stderr
    manifest = (tmp_path / "hashes/hashes.json").read_bytes()
    code, record, stderr = run_plugin("opentimestamps", tmp_path, **config)
    assert code == 0 and record["status"] == "succeeded", stderr
    current = tmp_path / "opentimestamps/current"
    generation = current.resolve()
    assert (current / "hashes.json").read_bytes() == manifest
    proof = (current / "hashes.json.ots").read_bytes()
    info = subprocess.run(
        [str(binary.abspath), "info", str(current / "hashes.json.ots")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert info.returncode == 0, info.stderr
    assert f"File sha256 hash: {hashlib.sha256(manifest).hexdigest()}" in info.stdout
    assert "PendingAttestation" in info.stdout
    page = (current / "index.html").read_text()
    assert json.loads(manifest)["root_hash"] in page
    assert "$ROOT_HASH" not in page and "$MANIFEST_SHA256" not in page
    assert "Bitcoin confirmation has not been checked" in page

    # The real client must reject altered manifest bytes before network verification.
    altered = tmp_path / "altered.json"
    altered.write_bytes(manifest + b"\n")
    verify = subprocess.run(
        [
            str(binary.abspath),
            "verify",
            "-f",
            str(altered),
            str(current / "hashes.json.ots"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert verify.returncode != 0
    assert "does not match" in verify.stderr

    # A real unavailable loopback endpoint, without a fake calendar server.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        config.update(
            OPENTIMESTAMPS_CALENDARS=json.dumps(
                [f"http://127.0.0.1:{unavailable.getsockname()[1]}"],
            ),
            OPENTIMESTAMPS_REQUIRED_CALENDARS="1",
            OPENTIMESTAMPS_TIMEOUT="5",
        )
        code, record, stderr = run_plugin("opentimestamps", tmp_path, **config)
    assert code != 0 and record["status"] == "failed", stderr
    assert current.resolve() == generation
    assert (current / "hashes.json.ots").read_bytes() == proof
    assert (current / "hashes.json").read_bytes() == manifest
    assert not list(current.parent.glob(".stamp-*"))

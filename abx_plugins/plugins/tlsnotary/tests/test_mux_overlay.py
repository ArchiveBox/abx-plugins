"""Exercise the removable mux correction through the real preparation hook."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import install_required_binary_from_config
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import setup_test_env
from abx_plugins.plugins.tlsnotary.vendor.mux_overlay import OVERLAY_DIR, _apply_delta

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PREPARE_HOOK = PLUGIN_DIR / "on_CrawlSetup__80_tlsnotary_prepare.py"


@pytest.fixture
def installed_extension(tmp_path):
    env = setup_test_env(tmp_path)
    env.update({"TLSNOTARY_ENABLED": "true", "TLSNOTARY_EXTENSION_DIR": ""})
    install_required_binary_from_config(PLUGIN_DIR, "tlsnotary", env=env)
    downloaded = json.loads(
        (
            Path(env["CHROMEWEBSTORE_EXTENSIONS_DIR"]) / "tlsnotary.extension.json"
        ).read_text(),
    )
    source = Path(downloaded["unpacked_path"])
    assert source.resolve().is_relative_to(tmp_path.resolve())
    return env, source


def prepare(env):
    return subprocess.run(
        [str(PREPARE_HOOK)],
        cwd=env["CRAWL_DIR"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_prepare_applies_mux_overlay_without_changing_download(installed_extension):
    env, source = installed_extension
    before = {
        str(path.relative_to(source)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in source.rglob("*")
        if path.is_file()
    }
    legacy = Path(env["PERSONAS_DIR"]) / ".tlsnotary" / "0.1.0.1501-export2"
    legacy.mkdir(parents=True)
    (legacy / "retained.txt").write_text(
        "Existing captures retain their prepared cache.\n",
    )
    prepared = prepare(env)
    assert prepared.returncode == 0, prepared.stderr
    metadata_path = Path(env["CRAWL_DIR"]) / "tlsnotary" / "extension.json"
    metadata = json.loads(metadata_path.read_text())
    assert metadata["mux_patch"] == 1
    destination = Path(metadata["unpacked_path"])
    assert destination != source and destination != legacy
    overlay = json.loads((OVERLAY_DIR / "manifest.json").read_text())
    for name, specification in overlay["files"].items():
        assert (
            hashlib.sha256((destination / name).read_bytes()).hexdigest()
            == specification["output_sha256"]
        )
    # A second real preparation checks the cached output rather than mutating it.
    prepared_again = prepare(env)
    assert prepared_again.returncode == 0, prepared_again.stderr
    assert json.loads(metadata_path.read_text()) == metadata
    assert (
        legacy / "retained.txt"
    ).read_text() == "Existing captures retain their prepared cache.\n"
    for name, digest in before.items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize(
    "filename",
    ["tlsn_wasm_bg.wasm", "c215ca8b495a7eadf02b.wasm", "71.bundle.js", "tlsn_wasm.js"],
)
def test_prepare_rejects_changed_upstream_assets(installed_extension, filename):
    env, source = installed_extension
    target = source / filename
    target.write_bytes(target.read_bytes() + b"corruption")
    result = prepare(env)
    assert result.returncode != 0
    assert f"mux overlay input checksum mismatch: {filename}" in result.stderr
    assert not (Path(env["CRAWL_DIR"]) / "tlsnotary" / "extension.json").exists()
    assert not list((Path(env["PERSONAS_DIR"]) / ".tlsnotary").glob("*mux1"))


def test_prepare_rejects_a_different_release(installed_extension):
    env, source = installed_extension
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["version"] = "0.1.0.1502"
    (source / "manifest.json").write_text(json.dumps(manifest))
    result = prepare(env)
    assert result.returncode != 0
    assert "requires release 0.1.0.1501" in result.stderr
    assert not (Path(env["CRAWL_DIR"]) / "tlsnotary" / "extension.json").exists()


@pytest.mark.parametrize("filename", ["tlsn_wasm_bg.wasm", "71.bundle.js"])
def test_prepare_rejects_changed_cached_assets(installed_extension, filename):
    env, _source = installed_extension
    result = prepare(env)
    assert result.returncode == 0, result.stderr
    metadata = json.loads(
        (Path(env["CRAWL_DIR"]) / "tlsnotary" / "extension.json").read_text(),
    )
    target = Path(metadata["unpacked_path"]) / filename
    target.write_bytes(target.read_bytes() + b"corruption")
    result = prepare(env)
    assert result.returncode != 0
    assert "Prepared extension differs from the pinned version" in result.stderr
    assert target.read_bytes().endswith(b"corruption"), (
        "Never silently replace a changed cache"
    )


def test_mux_delta_reconstructs_pinned_wasm_and_rejects_damage(installed_extension):
    _env, source = installed_extension
    specification = json.loads((OVERLAY_DIR / "manifest.json").read_text())
    patch = (OVERLAY_DIR / "tlsn_wasm_bg.wasm.bsdiff").read_bytes()
    assert hashlib.sha256(patch).hexdigest() == specification["patch_sha256"]
    wasm = specification["files"]["tlsn_wasm_bg.wasm"]
    original = (source / "tlsn_wasm_bg.wasm").read_bytes()
    output = _apply_delta(original, patch, wasm["output_size"])
    assert hashlib.sha256(output).hexdigest() == wasm["output_sha256"]
    for damaged in (b"invalid!" + patch[8:], patch[:31], patch[:-1]):
        with pytest.raises((ValueError, OSError)):
            _apply_delta(original, damaged, wasm["output_size"])

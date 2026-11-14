"""Exercise actual registry releases and the executable hook contract."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__34_packageregistry.py"


def run_hook(tmp_path, url, **settings):
    env = dict(os.environ, SNAP_DIR=str(tmp_path), **settings)
    return subprocess.run(
        [str(HOOK), "--url", url],
        env=env,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=180,
    )


@pytest.mark.parametrize(
    ("url", "registry", "name", "version"),
    [
        ("https://pypi.org/project/requests/2.32.3/", "pypi", "requests", "2.32.3"),
        (
            "https://www.npmjs.com/package/escape-string-regexp/v/5.0.0",
            "npm",
            "escape-string-regexp",
            "5.0.0",
        ),
        ("https://crates.io/crates/itoa/1.0.11", "crates", "itoa", "1.0.11"),
        ("https://rubygems.org/gems/rack/versions/3.1.8", "rubygems", "rack", "3.1.8"),
    ],
)
def test_real_registry_release(tmp_path, url, registry, name, version):
    result = run_hook(tmp_path, url)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "type": "ArchiveResult",
        "status": "succeeded",
        "output_str": "packageregistry/metadata.json",
    }
    root = tmp_path / "packageregistry"
    metadata = json.loads((root / "metadata.json").read_text())
    assert (metadata["registry"], metadata["name"], metadata["version"]) == (
        registry,
        name,
        version,
    )
    assert json.loads((root / "registry.json").read_text())
    assert metadata["files"]
    for item in metadata["files"]:
        file = root / item["filename"]
        assert file.is_file() and file.stat().st_size == item["size"] > 0
        if item["checksum"]:
            algorithm = "sha1" if len(item["checksum"]) == 40 else "sha256"
            assert (
                hashlib.file_digest(file.open("rb"), algorithm).hexdigest()
                == item["checksum"]
            )


def test_unrelated_and_disabled(tmp_path):
    result = run_hook(tmp_path, "https://example.com/")
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "noresults"
    result = run_hook(
        tmp_path,
        "https://pypi.org/project/requests/",
        PACKAGEREGISTRY_ENABLED="false",
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "skipped"


def test_failed_download_preserves_previous_capture(tmp_path):
    url = "https://pypi.org/project/requests/2.32.3/"
    result = run_hook(tmp_path, url)
    assert result.returncode == 0, result.stderr
    output = tmp_path / "packageregistry"
    previous = {path.name: path.read_bytes() for path in output.iterdir()}
    result = run_hook(tmp_path, url, PACKAGEREGISTRY_MAX_BYTES="1")
    assert result.returncode != 0
    assert json.loads(result.stdout)["status"] == "failed"
    assert {path.name: path.read_bytes() for path in output.iterdir()} == previous

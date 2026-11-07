#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["bsdiff4==1.4.1"]
# ///
"""Rebuild and verify the mux overlay without modifying the plugin checkout."""

import argparse
import hashlib
import importlib
import json
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DOWNLOADS = {
    "rustup-init": (
        "https://static.rust-lang.org/rustup/archive/1.29.3/x86_64-unknown-linux-gnu/rustup-init",
        "b1c8cb14e30889e7e2999e6b69e62d9e9b9ffc0f4df8180564d306d9680e6c55",
    ),
    "tlsn.tar.gz": (
        "https://api.github.com/repos/tlsnotary/tlsn/tarball/47aee45b53e06648c1b2ad3689b367b8c923fdec",
        "f119bebd606782842d906068b171a0d73c492f03d8f2ef1dc3016c352729607c",
    ),
    "tlsn-utils.tar.gz": (
        "https://api.github.com/repos/tlsnotary/tlsn-utils/tarball/64722f7de999cbd41c0cab7312dade306d50ea5f",
        "db1a058dd27bcaa879ec6ad0f7867cd9d07192ce0e6493ab096488dbcf57eede",
    ),
    "wasm-bindgen.tar.gz": (
        "https://github.com/wasm-bindgen/wasm-bindgen/releases/download/0.2.120/wasm-bindgen-0.2.120-x86_64-unknown-linux-musl.tar.gz",
        "ceb6f0b9fea9d2868f258215a1d7f86a4c28330a0d64432cc7d53674618772d6",
    ),
    "binaryen.tar.gz": (
        "https://github.com/WebAssembly/binaryen/releases/download/version_117/binaryen-version_117-x86_64-linux.tar.gz",
        "3dc677006555b355ea2da5e82602065a161d5e83eaefd3f759afa00b96e83212",
    ),
    "extension.zip": (
        "https://github.com/tlsnotary/tlsn-extension/releases/download/0.1.0.1501/extension-0.1.0.1501.zip",
        "1c312ad4305653b8d9351cf3eac46de4222a43be513260bf53053cfa1de45155",
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        type=Path,
        help="Fresh scratch directory outside the checkout",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(ROOT.parents[4]):
        raise ValueError("Build outside the source checkout")
    output.mkdir(parents=True, exist_ok=False)
    for filename, (url, digest) in DOWNLOADS.items():
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "ArchiveBox mux overlay rebuild"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f"Pinned download checksum mismatch: {filename}")
        (output / filename).write_bytes(payload)
    for name in ("Dockerfile", "mux-close-drain.patch"):
        shutil.copy2(ROOT / name, output / name)
    image = "abx-tlsnotary-mux-overlay:64722f7-close-drain-1"
    subprocess.run(
        ["docker", "build", "--platform", "linux/amd64", "--tag", image, str(output)],
        check=True,
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--mount",
            f"type=bind,src={ROOT / 'probe'},dst=/probe,readonly",
            "--workdir",
            "/probe",
            image,
            "bash",
            "/probe/verify.sh",
        ],
        check=True,
    )
    archive = output / "wasm-pkg.tar.gz"
    with archive.open("wb") as target:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                image,
                "tar",
                "czf",
                "-",
                "-C",
                "pkg",
                ".",
            ],
            stdout=target,
            check=True,
        )
    with tarfile.open(archive) as bundle:
        bundle.extractall(output / "pkg", filter="data")
    specification = json.loads((ROOT / "manifest.json").read_text())
    wasm = specification["files"]["tlsn_wasm_bg.wasm"]
    rebuilt = (output / "pkg" / "tlsn_wasm_bg.wasm").read_bytes()
    if hashlib.sha256(rebuilt).hexdigest() != wasm["output_sha256"]:
        raise ValueError(
            "Rebuilt WASM differs from the reviewed overlay; do not replace its pins",
        )
    with zipfile.ZipFile(output / "extension.zip") as bundle:
        original = bundle.read("tlsn_wasm_bg.wasm")
    if hashlib.sha256(original).hexdigest() != wasm["original_sha256"]:
        raise ValueError("Official WASM differs from the reviewed overlay")
    # This maintainer dependency belongs to the PEP 723 script environment.
    bsdiff4 = importlib.import_module("bsdiff4")
    patch = bsdiff4.diff(original, rebuilt)
    if hashlib.sha256(patch).hexdigest() != specification["patch_sha256"]:
        raise ValueError("Regenerated delta differs from the reviewed overlay")
    if bsdiff4.patch(original, patch) != rebuilt:
        raise ValueError("Regenerated delta does not reconstruct the source-built WASM")
    (output / "tlsn_wasm_bg.wasm.bsdiff").write_bytes(patch)
    print(json.dumps({"output": str(output), "sha256": specification["patch_sha256"]}))


if __name__ == "__main__":
    main()

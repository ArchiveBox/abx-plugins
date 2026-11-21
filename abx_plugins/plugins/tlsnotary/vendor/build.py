#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Build a matching private-proof extension/verifier from pinned upstream sources."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

PATCHES = Path(__file__).resolve().parent
SOURCES = {
    "mpz": (
        "tlsnotary/mpz2",
        "8f897838f4e24aff9da8e8b012e8ba35ae770141",
        "4277e3d66ec57dd7188fff6228ff09713d95c996d6d14be83e27ea2919b87277",
        "tlsnotary-mpz2-8f89783",
    ),
    "tlsn": (
        "tlsnotary/tlsn",
        "5f67389de3f8962227fddffd71d78a852acc9b01",
        "4b7b5ec796d5c69c5502096ec6f3cc29f3b91dbdcee45c65a8ebdf6fa118f7a6",
        "tlsnotary-tlsn-5f67389",
    ),
    "extension": (
        "tlsnotary/tlsn-extension",
        "d44623434d04ad00d7b1f8da9712cbc10cd0e99c",
        "bef84739858ca73b62fa2c896e13d452b5a1b773a80496d06bdf90fb93db1378",
        "tlsnotary-tlsn-extension-d446234",
    ),
}


BETA1_SOURCES = {
    "tlsn": (
        "tlsnotary/tlsn",
        "40a069565a2eaecbb5e691dbc634be20e8498daf",
        "162bfea805e009c3b38ec0834859682af0924de3c3e79992e8a81e4a0a49b790",
        "tlsnotary-tlsn-40a0695",
    ),
    "extension": SOURCES["extension"],
}


def write_zip(output: Path, source: Path) -> None:
    """Keep the portable ZIP independent of source tree timestamps and ownership."""
    with zipfile.ZipFile(output, "w") as archive:
        for file in sorted(source.rglob("*")):
            if not file.is_file():
                continue
            entry = zipfile.ZipInfo(file.relative_to(source).as_posix())
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            with file.open("rb") as payload, archive.open(entry, "w") as target:
                shutil.copyfileobj(payload, target)


def run(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def toolchain_env(name: str) -> dict[str, str]:
    rustc = subprocess.check_output(
        ["rustup", "which", "--toolchain", name, "rustc"],
        text=True,
    ).strip()
    directory = Path(rustc).parent
    # Selecting cargo alone can still invoke a different rustc earlier in PATH.
    return {
        **os.environ,
        "RUSTUP_TOOLCHAIN": name,
        "RUSTC": rustc,
        "RUSTDOC": str(directory / "rustdoc"),
        "PATH": str(directory) + os.pathsep + os.environ["PATH"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        type=Path,
        help="Build directory outside the source checkout",
    )
    parser.add_argument(
        "--component",
        choices=("both", "verifier", "extension"),
        default="both",
    )
    parser.add_argument("--profile", choices=("legacy", "beta1"), default="legacy")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Verify and patch source without compiling",
    )
    parser.add_argument("--wasm-toolchain", default="nightly-2026-10-12")
    args = parser.parse_args()
    beta1 = args.profile == "beta1"
    sources = BETA1_SOURCES if beta1 else SOURCES
    patches = (
        {"extension": "extension-beta1.patch"}
        if beta1
        else {name: name + ".patch" for name in sources}
    )
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = output / "src"
    source.mkdir(exist_ok=True)
    dirs = {}
    for name, (repo, revision, digest, directory) in sources.items():
        archive = output / (name + ".tar.gz")
        if not archive.exists():
            request = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/tarball/{revision}",
                headers={"User-Agent": "ArchiveBox TLSNotary build"},
            )
            with (
                urllib.request.urlopen(request, timeout=60) as response,
                archive.open("wb") as target,
            ):
                shutil.copyfileobj(response, target)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Upstream archive checksum mismatch: {name}")
        destination = source / directory
        if destination.exists():
            raise FileExistsError(
                f"Use a fresh build directory; refusing to modify {destination}",
            )
        with tarfile.open(archive) as bundle:
            bundle.extractall(source, filter="data")
        if name in patches:
            run(
                [
                    "patch",
                    "--batch",
                    "--fuzz=0",
                    "-p1",
                    "-i",
                    str(PATCHES / patches[name]),
                ],
                destination,
            )
        dirs[name] = destination

    if args.prepare_only:
        print(
            json.dumps(
                {"output": str(output), "profile": args.profile, "source": str(source)},
            ),
        )
        return

    # Both peers must reference the same protocol source; do not upgrade either independently.
    tlsn = dirs["tlsn"]
    extension = dirs["extension"]
    if args.component in ("both", "verifier"):
        env = toolchain_env("1.95.0")
        env["CARGO_TARGET_DIR"] = str(output / "target")
        run(
            [
                "cargo",
                "build",
                "--locked",
                "--release",
                "--bin",
                "tlsn-verifier-server",
            ],
            extension / "servers",
            env,
        )
        shutil.copy2(
            output / "target/release/tlsn-verifier-server",
            output / "tlsn-verifier-server",
        )
    if args.component in ("both", "extension"):
        env = toolchain_env(args.wasm_toolchain)
        env["CARGO_TARGET_DIR"] = str(output / "target-wasm")
        run(["sh", "build.sh"], tlsn / "crates/wasm", env)
        run(["npm", "ci"], extension)
        run(["npm", "run", "build:deps"], extension)
        run(["npm", "run", "build:extension"], extension)
        built = output / "extension"
        shutil.copytree(extension / "packages/extension/build", built)
        # Crawl setup verifies this marker and its bundles before using the
        # source-built localProof export; it must not patch minified code twice.
        marker = {
            "sources": {name: values[1] for name, values in sources.items()},
            "patches": {
                name: hashlib.sha256((PATCHES / patch).read_bytes()).hexdigest()
                for name, patch in patches.items()
            },
            "bundles": {
                name: hashlib.sha256((built / name).read_bytes()).hexdigest()
                for name in (
                    "offscreen.bundle.js",
                    "background.bundle.js",
                    *(file.name for file in sorted(built.glob("*.wasm"))),
                )
            },
        }
        (built / "archivebox-build.json").write_text(
            json.dumps(marker, indent=2) + "\n",
        )
        if beta1:
            write_zip(output / "tlsnotary-beta1-extension.zip", built)
        else:
            shutil.make_archive(str(output / "extension"), "zip", built)
    print(json.dumps({"output": str(output), "component": args.component}))


if __name__ == "__main__":
    main()

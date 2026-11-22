"""Build deterministic third-party notices from the emitted beta.1 dependency graph."""

import hashlib
import json
import re
import shutil
import subprocess
import tomllib
import urllib.request
from pathlib import Path


NOTICE_NAME = re.compile(
    r"^(?:license|licence|notice|copying|copyright)(?:$|[-_.])",
    re.I,
)
CHECKOUT_LICENSE = "MIT OR Apache-2.0"
SOURCE_CHECKOUTS = {
    ("tlsnotary/mpz2", "23e2cc08ed11ae86d71f72fb968f1273d70c9e88"),
    ("tlsnotary/tlsn-utils", "3e4c31e9e40f514f0fe534daa3189d00e12c7a17"),
    ("tlsnotary/tlsn-utils", "e2f31e6a30bc5d1641ed6407fb1a38a595e523ce"),
}
MOZILLA_SOURCE = "https://github.com/mozilla/webextension-polyfill/tree/1649d84ad93c6ebb7225a3db44e96c33e6bbb4a7"
QUICKJS_SOURCE = "https://github.com/quickjs-ng/quickjs/tree/7ded62c536fca860b8106c39fb75f2df8fe27180"


def notice_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and NOTICE_NAME.match(path.name)
    )


def npm_modules(modules):
    for module in modules:
        yield module
        yield from npm_modules(module.get("children", ()))
        yield from npm_modules(module.get("modules", ()))


def javascript_components(stats, checkout: Path):
    locked = json.loads((checkout / "package-lock.json").read_text())["packages"]
    packages = {}
    for chunk in stats["chunks"]:
        if not chunk["files"]:
            continue
        for module in npm_modules(chunk["modules"]):
            resource = module.get("nameForCondition")
            if not resource or "/node_modules/" not in resource:
                continue
            directory = Path(resource).parent
            for path in (directory, *directory.parents):
                if (path / "package.json").is_file():
                    data = json.loads((path / "package.json").read_text())
                    if data.get("name"):
                        break
            else:
                raise ValueError("No npm declaration for emitted Webpack module")
            relative = path.relative_to(checkout).as_posix()
            spec = locked[relative]
            if data["version"] != spec.get("version"):
                raise ValueError(f"Unlocked emitted dependency: {data['name']}")
            packages[data["name"], data["version"]] = (
                data["license"],
                path,
                spec.get("integrity"),
            )
    if not packages or not any(
        name == "webextension-polyfill" for name, _version in packages
    ):
        raise ValueError("Browser dependency graph is missing emitted modules")
    return packages


def rust_components(graph):
    packages = {item["id"]: item for item in graph["packages"]}
    nodes = {item["id"]: item["deps"] for item in graph["resolve"]["nodes"]}
    (root,) = (item["id"] for item in packages.values() if item["name"] == "tlsn-wasm")
    pending = [root]
    seen = set()
    while pending:
        key = pending.pop()
        if key in seen:
            continue
        seen.add(key)
        pending.extend(
            dep["pkg"]
            for dep in nodes[key]
            if any(kind["kind"] is None for kind in dep["dep_kinds"])
        )
        yield packages[key]


def copy_declared_texts(source: Path, destination: Path, apache_text: Path):
    texts = notice_files(source)
    if not texts:
        # These pinned workspaces declare the dual license at repository level,
        # even if they do not ship a standalone copy of either standard text.
        readme = (source / "README.md").read_text()
        section = readme.split("\n## License\n", 1)[1].split("\n## ", 1)[0]
        if "Apache" not in section or "MIT" not in section:
            raise ValueError("Source is missing its reviewed dual-license declaration")
        (destination / "LICENSE-DECLARATION.md").write_text(section.strip() + "\n")
        texts = [apache_text]
    for path in texts:
        shutil.copyfile(path, destination / path.name)


def copy_pinned_texts(pin, destination: Path, cache: Path):
    for name, digest in sorted(pin["files"].items()):
        local = cache / digest
        if local.is_file():
            data = local.read_bytes()
        else:
            url = f"https://raw.githubusercontent.com/{pin['repo']}/{pin['revision']}/{name}"
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read(64 * 1024 + 1)
        if len(data) > 64 * 1024 or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(
                f"Unexpected corresponding license bytes: {pin['repo']}/{name}",
            )
        (destination / name).write_bytes(data)


def generate(
    checkout: Path,
    tlsn: Path,
    built: Path,
    webpack_stats: Path,
    env: dict,
    cache: Path,
):
    pins = json.loads(Path(__file__).with_name("release_notice_pins.json").read_text())
    graph = json.loads(
        subprocess.check_output(
            [
                "cargo",
                "metadata",
                "--locked",
                "--filter-platform",
                "wasm32-unknown-unknown",
                "--format-version",
                "1",
            ],
            cwd=tlsn,
            env=env,
        ),
    )
    sysroot = Path(
        subprocess.check_output(
            ["rustc", "--print", "sysroot"],
            env=env,
            text=True,
        ).strip(),
    )
    rust_docs = sysroot / "share/doc/rust"
    # A minimal hosted toolchain does not include rust-docs. Supply reviewed
    # standard library notices separately rather than silently dropping them.
    rust_docs = Path(env.get("TLSNOTARY_RUST_LICENSE_DOCS", str(rust_docs)))
    if not (rust_docs / "COPYRIGHT-library.html").is_file():
        raise ValueError(
            "The exact Rust toolchain standard-library copyright file is required",
        )
    apache = rust_docs / "licenses/Apache-2.0.txt"
    output = built / "licenses"
    output.mkdir()
    shutil.copytree(rust_docs / "licenses", output / "rust-standard-library")
    shutil.copyfile(
        rust_docs / "COPYRIGHT-library.html",
        output / "rust-standard-library/COPYRIGHT-library.html",
    )
    for name, source in [("tlsnotary-extension", checkout), ("tlsnotary-wasm", tlsn)]:
        destination = output / name
        destination.mkdir()
        copy_declared_texts(source, destination, apache)

    records = []
    js = javascript_components(json.loads(webpack_stats.read_text()), checkout)
    rust = list(rust_components(graph))
    tokio = [component for component in rust if component["name"] == "tokio"]
    (tokio,) = tokio
    tokio_license = Path(tokio["manifest_path"]).with_name("LICENSE")
    assert "Copyright (c) Tokio Contributors" in tokio_license.read_text()
    lock = tomllib.loads((tlsn / "Cargo.lock").read_text())
    pin_keys_seen = set()
    for name, version, declared, package_directory, origin in sorted(
        [
            ("npm:" + name, ver, license_id, path, integrity)
            for (name, ver), (license_id, path, integrity) in js.items()
        ]
        + [
            (
                "rust:" + p["name"],
                p["version"],
                p["license"],
                Path(p["manifest_path"]).parent,
                p["source"],
            )
            for p in rust
        ],
        key=lambda item: (*item[:2], item[4] or ""),
    ):
        item = f"{name}@{version}"
        destination = output / hashlib.sha256(item.encode()).hexdigest()[:16]
        paths = notice_files(package_directory)
        if destination.exists():
            raise ValueError(f"Duplicate dependency notice identity: {item}")
        destination.mkdir()
        record = {
            "package": item,
            "spdx": declared,
            "source": origin,
            "notices": f"licenses/{destination.name}/",
        }
        for path in paths:
            shutil.copyfile(path, destination / path.name)
        if item in pins:
            pin = pins[item]
            pin_keys_seen.add(item)
            if "archive_sha256" in pin:
                (spec,) = (
                    x
                    for x in lock["package"]
                    if f"rust:{x['name']}@{x['version']}" == item
                )
                if spec["checksum"] != pin["archive_sha256"]:
                    raise ValueError("Locked wasm-tracing source changed")
                record["archive_sha256"] = spec["checksum"]
                record["code_revision"] = pin["code_revision"]
            copy_pinned_texts(pin, destination, cache)
            record.update(
                {k: pin[k] for k in ("repo", "revision", "attribution") if k in pin},
            )
        if item == "rust:futures-plex@0.1.0":
            shutil.copyfile(tokio_license, destination / "TOKIO-LICENSE-MIT")
            record["attribution"] = (
                "Port of Tokio's SimplexStream and DuplexStream; Copyright (c) Tokio Contributors."
            )
        if item == "npm:glob-to-regex.js@1.2.0":
            readme = (package_directory / "README.md").read_text()
            section = readme.split("\n## License\n", 1)[1].split("\n## ", 1)[0]
            (destination / "LICENSE-DECLARATION.md").write_text(section.strip() + "\n")
            shutil.copyfile(apache, destination / apache.name)
        if item == "npm:webextension-polyfill@0.12.0":
            record["corresponding_source"] = MOZILLA_SOURCE
        if item == "npm:@jitl/quickjs-ng-wasmfile-release-sync@0.31.0":
            if "quickjs-ng:" not in (destination / "LICENSE").read_text():
                raise ValueError("Missing embedded QuickJS-ng attribution")
            record["corresponding_source"] = QUICKJS_SOURCE
        if name.startswith("rust:") and (
            not declared
            or not any(destination.iterdir())
            or item == "rust:futures-plex@0.1.0"
        ):
            if package_directory.is_relative_to(tlsn):
                record["spdx"] = declared or CHECKOUT_LICENSE
                record["workspace_notices"] = "licenses/tlsnotary-wasm/"
            elif origin and origin.startswith("git+https://github.com/"):
                repo, revision = (
                    origin.removeprefix("git+https://github.com/").split("?", 1)[0],
                    origin.rsplit("#", 1)[-1],
                )
                if (repo, revision) in SOURCE_CHECKOUTS:
                    workspace = next(
                        parent
                        for parent in package_directory.parents
                        if parent.name == revision[:7]
                    )
                    workspace_name = f"{repo.rsplit('/', 1)[-1]}-{revision[:7]}"
                    workspace_notices = output / workspace_name
                    if not workspace_notices.exists():
                        workspace_notices.mkdir()
                        copy_declared_texts(workspace, workspace_notices, apache)
                    record["spdx"] = declared or CHECKOUT_LICENSE
                    record["workspace_notices"] = f"licenses/{workspace_name}/"
        if not record["spdx"] or not (
            any(destination.iterdir()) or record.get("workspace_notices")
        ):
            raise ValueError(f"Missing license or notice text for {item}")
        records.append(record)
    if pin_keys_seen != pins.keys() or not any(
        row.get("corresponding_source") == MOZILLA_SOURCE for row in records
    ):
        raise ValueError("Required pinned notices do not match the built release graph")
    (output / "components.json").write_text(
        json.dumps(
            {
                "npm_lock_sha256": hashlib.sha256(
                    (checkout / "package-lock.json").read_bytes(),
                ).hexdigest(),
                "rust_lock_sha256": hashlib.sha256(
                    (tlsn / "Cargo.lock").read_bytes(),
                ).hexdigest(),
                "components": records,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    (built / "THIRD_PARTY_NOTICES.txt").write_text(
        "ArchiveBox TLSNotary beta.1 browser build.\n"
        "Component licenses, attribution, and pinned provenance: licenses/components.json\n"
        "The bundled Mozilla webextension-polyfill 0.12.0 is MPL-2.0. Obtain its\n"
        f"corresponding source at {MOZILLA_SOURCE}\n",
    )

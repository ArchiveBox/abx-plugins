#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""Download the selected release from a public package registry."""

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import rich_click as click
from abx_plugins.plugins.base.utils import emit_archive_result_record, load_config

CONFIG = load_config()


def request(url, timeout):
    return urllib.request.urlopen(
        urllib.request.Request(
            url,
            headers={
                "User-Agent": "ArchiveBox-PackageRegistry/1.0 (https://archivebox.io)",
                "Accept": "*/*",
            },
        ),
        timeout=timeout,
    )


def json_url(url, timeout):
    with request(url, timeout) as response:
        return json.load(response)


def identify(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if host == "pypi.org" and len(parts) in (2, 3) and parts[0] == "project":
        return "pypi", parts[1], parts[2] if len(parts) == 3 else None
    if host == "npmjs.com" and len(parts) >= 2 and parts[0] == "package":
        name_end = 3 if parts[1].startswith("@") else 2
        if (
            len(parts) < name_end
            or len(parts) not in (name_end, name_end + 2)
            or (len(parts) > name_end and parts[name_end] != "v")
        ):
            return None
        return (
            "npm",
            "/".join(parts[1:name_end]),
            parts[name_end + 1] if len(parts) > name_end else None,
        )
    if host == "crates.io" and len(parts) in (2, 3) and parts[0] == "crates":
        return "crates", parts[1], parts[2] if len(parts) == 3 else None
    if (
        host == "rubygems.org"
        and len(parts) in (2, 4)
        and parts[0] == "gems"
        and (len(parts) == 2 or parts[2] == "versions")
    ):
        return "rubygems", parts[1], parts[3] if len(parts) == 4 else None
    return None


def gather(registry, name, version, timeout):
    qname = urllib.parse.quote(name, safe="")
    raw = {}
    files = []
    if registry == "pypi":
        url = (
            f"https://pypi.org/pypi/{qname}/"
            + (urllib.parse.quote(version, safe="") + "/" if version else "")
            + "json"
        )
        raw = json_url(url, timeout)
        info = raw["info"]
        version = info["version"]
        description = info.get("summary") or ""
        readme = info.get("description") or ""
        for item in raw.get("urls", []):
            files.append(
                (
                    item["url"],
                    item["filename"],
                    item.get("digests", {}).get("sha256"),
                    None,
                )
            )
    elif registry == "npm":
        raw = json_url(f"https://registry.npmjs.org/{qname}", timeout)
        version = version or raw["dist-tags"]["latest"]
        info = raw["versions"][version]
        description = info.get("description") or ""
        readme = raw.get("readme") or ""
        dist = info["dist"]
        files.append(
            (
                dist["tarball"],
                f"{name.split('/')[-1]}-{version}.tgz",
                dist.get("shasum"),
                dist.get("integrity"),
            )
        )
    elif registry == "crates":
        raw = json_url(f"https://crates.io/api/v1/crates/{qname}", timeout)
        version = (
            version or raw["crate"]["max_stable_version"] or raw["crate"]["max_version"]
        )
        info = next(item for item in raw["versions"] if item["num"] == version)
        description = raw["crate"].get("description") or ""
        readme = ""
        files.append(
            (
                f"https://crates.io/api/v1/crates/{qname}/{urllib.parse.quote(version, safe='')}/download",
                f"{name}-{version}.crate",
                info.get("checksum"),
                None,
            )
        )
    else:
        raw = json_url(f"https://rubygems.org/api/v1/gems/{qname}.json", timeout)
        version = version or raw["version"]
        info = json_url(
            f"https://rubygems.org/api/v2/rubygems/{qname}/versions/{urllib.parse.quote(version, safe='')}.json",
            timeout,
        )
        raw = {"gem": raw, "version": info}
        description = info.get("info") or ""
        readme = ""
        files.append(
            (
                f"https://rubygems.org/downloads/{qname}-{urllib.parse.quote(version, safe='')}.gem",
                f"{name}-{version}.gem",
                info.get("sha"),
                None,
            )
        )
    return version, description, readme, raw, files


def download(url, filename, checksum, integrity, directory, timeout, remaining):
    if Path(filename).name != filename or filename in (
        "",
        ".",
        "..",
        "metadata.json",
        "registry.json",
    ):
        raise ValueError("Invalid distribution filename")
    digest = hashlib.sha1() if checksum and len(checksum) == 40 else hashlib.sha256()
    sri = None
    if integrity:
        candidates = [part.split("-", 1) for part in integrity.split() if "-" in part]
        for algorithm, value in candidates:
            if algorithm in ("sha512", "sha384", "sha256"):
                sri = (hashlib.new(algorithm), value)
                break
    size = 0
    with request(url, timeout) as response, (directory / filename).open("wb") as out:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > remaining:
                raise ValueError(
                    "Distribution downloads exceed PACKAGEREGISTRY_MAX_BYTES"
                )
            digest.update(chunk)
            if sri:
                sri[0].update(chunk)
            out.write(chunk)
    if checksum and digest.hexdigest().lower() != checksum.lower():
        raise ValueError(f"Checksum mismatch for {filename}")
    if sri and base64.b64encode(sri[0].digest()).decode() != sri[1]:
        raise ValueError(f"Integrity mismatch for {filename}")
    return size


@click.command()
@click.option("--url", required=True)
def main(url):
    if not CONFIG.PACKAGEREGISTRY_ENABLED:
        emit_archive_result_record("skipped", "PACKAGEREGISTRY_ENABLED=False")
        return
    target = identify(url)
    if not target:
        emit_archive_result_record("noresults", "Not a supported package registry URL")
        return
    output = Path(CONFIG.SNAP_DIR or ".").resolve() / "packageregistry"
    output.mkdir(parents=True, exist_ok=True)
    try:
        registry, name, requested_version = target
        if not re.fullmatch(r"[A-Za-z0-9@._+/-]+", name) or (
            requested_version
            and not re.fullmatch(r"[A-Za-z0-9._+-]+", requested_version)
        ):
            raise ValueError("Invalid package name or version")
        version, description, readme, raw, files = gather(
            registry, name, requested_version, int(CONFIG.PACKAGEREGISTRY_TIMEOUT)
        )
        if not files:
            emit_archive_result_record(
                "noresults", "No distributions available for release"
            )
            return
        with tempfile.TemporaryDirectory(prefix=".capture-", dir=output) as temporary:
            stage = Path(temporary)
            remaining = int(CONFIG.PACKAGEREGISTRY_MAX_BYTES)
            captured = []
            for file_url, filename, checksum, integrity in files:
                size = download(
                    file_url,
                    filename,
                    checksum,
                    integrity,
                    stage,
                    int(CONFIG.PACKAGEREGISTRY_TIMEOUT),
                    remaining,
                )
                remaining -= size
                captured.append(
                    {
                        "filename": filename,
                        "url": file_url,
                        "size": size,
                        "checksum": checksum,
                        "integrity": integrity,
                    }
                )
            manifest = {
                "registry": registry,
                "name": name,
                "version": version,
                "source_url": url,
                "description": description,
                "readme": readme,
                "files": captured,
            }
            (stage / "registry.json").write_text(
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
            )
            (stage / "metadata.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            )
            for path in stage.iterdir():
                os.replace(path, output / path.name)
        emit_archive_result_record("succeeded", "packageregistry/metadata.json")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            emit_archive_result_record("noresults", "Package or release not found")
            return
        print(f"Package registry request failed: {exc}", file=sys.stderr)
        emit_archive_result_record("failed", str(exc))
        raise SystemExit(1)
    except Exception as exc:
        print(f"Package registry capture failed: {exc}", file=sys.stderr)
        emit_archive_result_record("failed", str(exc))
        raise SystemExit(1)


if __name__ == "__main__":
    main()

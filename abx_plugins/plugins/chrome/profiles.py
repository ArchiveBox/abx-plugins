"""Copy Chromium profiles without carrying volatile or orphaned browser state."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil


VOLATILE_PROFILE_COPY_PATTERNS = (
    # Chromium/macOS atomic-write staging files (e.g.
    # .com.brave.Browser.TransportSecurity.Ze7UBU) disappear after rename.
    ".*.??????",
    "Cache",
    "Code Cache",
    "GPUCache",
    "ShaderCache",
    "Crashpad",
    "BrowserMetrics",
    "BrowserMetrics-spare.pma",
    "RunningChromeVersion",
    "DevToolsActivePort",
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "chrome.log",
    "chrome_debug.log",
)


def _is_copyable_profile_entry(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if path.is_dir():
            return os.access(path, os.R_OK | os.X_OK)
        return os.access(path, os.R_OK)
    except OSError:
        return False


def profile_copy_ignore(src: str, names: list[str]) -> set[str]:
    ignored = set(shutil.ignore_patterns(*VOLATILE_PROFILE_COPY_PATTERNS)(src, names))
    src_path = Path(src)
    if src_path.name in {"IndexedDB", "Local Extension Settings"}:
        # Uninstalled extensions leave their databases and settings behind. In particular,
        # old unpacked recorder IDs can retain entire captures that no installed
        # extension can access. Cloning those stores can exhaust the disk
        # before the first hook starts. Never delete the source, and preserve
        # website databases and installed (including disabled) extension state.
        # Service Worker/ScriptCache is deliberately retained: its script bytes
        # back registrations in the copied profile, including offline sites.
        settings = None
        # Chromium can move protected extension settings into Secure Preferences.
        # Both files belong to the same profile; ignoring either can misidentify
        # an installed extension as an orphan and omit its authentication state.
        for filename in ("Preferences", "Secure Preferences"):
            try:
                preferences = json.loads((src_path.parent / filename).read_text())
                entries = preferences.get("extensions", {}).get("settings")
                if isinstance(entries, dict):
                    settings = {**(settings or {}), **entries}
            except FileNotFoundError:
                continue
            except (OSError, ValueError, AttributeError, TypeError):
                settings = None
                break
        # Missing/malformed preferences cannot establish that an origin is
        # orphaned. Preserve unknown state rather than guess from directory size.
        if isinstance(settings, dict):
            installed_ids = {
                key
                for key, value in settings.items()
                if isinstance(value, dict) and isinstance(value.get("path"), str)
            }
            # On-demand extensions retain their identity even when Chrome has
            # removed a disposable install path from Preferences between runs.
            try:
                keys = src_path.parent.parent / "archivebox-extension-keys"
                try:
                    key_files = list(keys.iterdir())
                except FileNotFoundError:
                    key_files = []
                for key_file in key_files:
                    if key_file.suffix != ".pub":
                        continue
                    key = base64.b64decode(key_file.read_text().strip(), validate=True)
                    if not key:
                        raise ValueError("Empty extension identity")
                    extension_id = (
                        hashlib.sha256(key)
                        .hexdigest()[:32]
                        .translate(
                            str.maketrans("0123456789abcdef", "abcdefghijklmnop"),
                        )
                    )
                    installed_ids.add(extension_id)
            except (OSError, ValueError):
                installed_ids = None
            for name in names:
                extension_id = None
                if src_path.name == "IndexedDB" and name.startswith(
                    "chrome-extension_",
                ):
                    extension_id = name.split("_", 2)[1]
                elif src_path.name == "Local Extension Settings" and re.fullmatch(
                    r"[a-p]{32}",
                    name,
                ):
                    extension_id = name
                if (
                    installed_ids is not None
                    and extension_id
                    and extension_id not in installed_ids
                    and isinstance(settings.get(extension_id, {}), dict)
                ):
                    ignored.add(name)
    for name in names:
        if name in ignored:
            continue
        if not _is_copyable_profile_entry(src_path / name):
            # Chrome can leave generated profile components unreadable when a
            # previous run used a different runtime uid, a mounted profile came
            # from the host, or Chrome tightened permissions on cache/model
            # directories. Those entries are not required to launch a cloned
            # profile, and letting copytree abort here prevents the crawl from
            # even creating its first Snapshot.
            ignored.add(name)
    return ignored


def validate_copy(source: Path, destination: Path) -> None:
    if not source.is_absolute() or not destination.is_absolute():
        raise ValueError("Profile paths must be absolute")
    if not source.is_dir():
        raise ValueError(f"Profile directory does not exist: {source}")
    source, destination = source.resolve(), destination.resolve()
    # Resolve symlinks before removal: a destination alias must never erase its source.
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError("Source and destination profile directories overlap")


def copy_profile(source: Path, destination: Path) -> None:
    validate_copy(source, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        destination.unlink()
    elif destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=True, ignore=profile_copy_ignore)


def run(request: dict) -> Path:
    action = request["action"]
    if action == "copy":
        destination = Path(request["destination_dir"])
        copy_profile(Path(request["source_dir"]), destination)
    elif action == "import":
        source, destination = (
            Path(request["source_dir"]),
            Path(request["destination_dir"]),
        )
        profile_name = request["profile_dir"]
        if (
            not profile_name
            or Path(profile_name).name != profile_name
            or profile_name in {".", ".."}
        ):
            raise ValueError("profile_dir must name a single browser profile")
        selected = source / profile_name
        validate_copy(source, destination)
        validate_copy(selected, destination)
        local_state = source / "Local State"
        state = None
        if local_state.exists():
            state = json.loads(local_state.read_text())
            profile_state = state.setdefault("profile", {})
            # Runtime launches select Default; import only the selected identity.
            profile_state["last_used"] = "Default"
            profile_state["last_active_profiles"] = ["Default"]
            info = profile_state.get("info_cache", {}).get(profile_name)
            profile_state["info_cache"] = {"Default": info} if info else {}
        # Validate the source and metadata before replacing an existing destination.
        if destination.is_symlink():
            destination.unlink()
        elif destination.exists():
            shutil.rmtree(destination)
        copy_profile(selected, destination / "Default")
        if state is not None:
            (destination / "Local State").write_text(json.dumps(state))
    elif action == "prepare":
        destination = Path(request["persona_dir"])
        if not destination.is_absolute():
            raise ValueError("persona_dir must be absolute")
        source = Path(request["source_dir"]) if request.get("source_dir") else None
        if source is not None:
            validate_copy(source, destination)
            copy_profile(source, destination / "chrome_profile")
            (destination / "template_dir.txt").write_text(str(source))
        else:
            (destination / "chrome_profile").mkdir(parents=True, exist_ok=True)
        (destination / "chrome_downloads").mkdir(parents=True, exist_ok=True)
        if request.get("name"):
            (destination / "persona_name.txt").write_text(request["name"])
        if request.get("browser_binary"):
            (destination / "chrome_binary.txt").write_text(request["browser_binary"])
    else:
        raise ValueError(f"Unknown profile action: {action}")
    return destination

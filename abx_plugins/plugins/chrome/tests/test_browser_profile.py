"""Run Chrome's declared profile command against real filesystem state."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


PLUGIN = Path(__file__).resolve().parent.parent


def run_profile(**request):
    command = json.loads((PLUGIN / "config.json").read_text())["commands"][
        "browser-profile"
    ]
    return subprocess.run(
        [sys.executable, str(PLUGIN / command[0]), *command[1:]],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_profile_copy_preserves_source_and_session_state(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    profile = source / "Default"
    profile.mkdir(parents=True)
    (profile / "Preferences").write_text('{"extensions":{"settings":{}}}')
    (profile / "Sessions").mkdir()
    (profile / "Sessions" / "Session_1").write_bytes(b"saved session")
    (profile / "Cache").mkdir()
    (profile / "Cache" / "cached").write_text("cache")
    (source / "SingletonLock").symlink_to("missing-lock")
    (source / "chrome.log").write_text("log")
    (source / "session-link").symlink_to("Default/Sessions/Session_1")
    destination.mkdir()
    (destination / "stale").write_text("old")
    result = run_profile(
        action="copy",
        source_dir=str(source),
        destination_dir=str(destination),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"path": str(destination)}
    assert (destination / "Default/Sessions/Session_1").read_bytes() == b"saved session"
    assert (destination / "session-link").is_symlink()
    assert (destination / "session-link").read_bytes() == b"saved session"
    assert not (destination / "SingletonLock").is_symlink()
    assert not (destination / "Default/Cache").exists()
    assert not (destination / "chrome.log").exists()
    assert not (destination / "stale").exists()
    assert (source / "SingletonLock").is_symlink()
    assert (source / "Default/Cache/cached").read_text() == "cache"
    assert (source / "chrome.log").read_text() == "log"


def test_profile_import_normalizes_only_selected_profile(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    for name in ("Default", "Profile 2"):
        (source / name).mkdir(parents=True)
        (source / name / "Preferences").write_text(name)
    state = {
        "profile": {
            "last_used": "Profile 2",
            "info_cache": {
                "Default": {"name": "Other"},
                "Profile 2": {"name": "Selected"},
            },
        },
    }
    (source / "Local State").write_text(json.dumps(state))
    result = run_profile(
        action="import",
        source_dir=str(source),
        destination_dir=str(destination),
        profile_dir="Profile 2",
    )
    assert result.returncode == 0, result.stderr
    assert (destination / "Default/Preferences").read_text() == "Profile 2"
    assert not (destination / "Profile 2").exists()
    assert json.loads((destination / "Local State").read_text())["profile"] == {
        "last_used": "Default",
        "last_active_profiles": ["Default"],
        "info_cache": {"Default": {"name": "Selected"}},
    }
    assert json.loads((source / "Local State").read_text()) == state


def test_profile_prepare_preserves_existing_persona_files(tmp_path):
    persona = tmp_path / "persona"
    persona.mkdir()
    (persona / "auth.json").write_text('{"cookies":[]}')
    result = run_profile(
        action="prepare",
        persona_dir=str(persona),
        name="Personal",
        browser_binary="/browser",
    )
    assert result.returncode == 0, result.stderr
    assert (persona / "chrome_profile").is_dir()
    assert (persona / "chrome_downloads").is_dir()
    assert (persona / "persona_name.txt").read_text() == "Personal"
    assert (persona / "chrome_binary.txt").read_text() == "/browser"
    assert (persona / "auth.json").read_text() == '{"cookies":[]}'


@pytest.mark.parametrize("action", ["copy", "import", "prepare"])
@pytest.mark.parametrize("relationship", ["equal", "parent", "child", "symlink"])
def test_profile_copy_rejects_overlaps_before_mutation(tmp_path, action, relationship):
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep").write_text("source")
    (source / "Default").mkdir()
    destination = {
        "equal": source,
        "parent": tmp_path,
        "child": source / "child",
        "symlink": tmp_path / "link",
    }[relationship]
    if relationship == "symlink":
        destination.symlink_to(source, target_is_directory=True)
    result = run_profile(
        action=action,
        source_dir=str(source),
        destination_dir=str(destination),
        persona_dir=str(destination),
        profile_dir="Default",
    )
    assert result.returncode != 0
    assert "overlap" in result.stderr.lower()
    assert (source / "keep").read_text() == "source"


@pytest.mark.parametrize("profile_dir", ["Missing", "../outside", "/outside"])
def test_profile_import_rejects_invalid_source_before_mutation(tmp_path, profile_dir):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (destination / "keep").write_text("destination")
    result = run_profile(
        action="import",
        source_dir=str(source),
        destination_dir=str(destination),
        profile_dir=profile_dir,
    )
    assert result.returncode != 0
    assert (destination / "keep").read_text() == "destination"


def test_profile_import_rejects_invalid_metadata_before_mutation(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    (source / "Default").mkdir(parents=True)
    (source / "Local State").write_text("invalid JSON")
    destination.mkdir()
    (destination / "keep").write_text("destination")
    result = run_profile(
        action="import",
        source_dir=str(source),
        destination_dir=str(destination),
        profile_dir="Default",
    )
    assert result.returncode != 0
    assert (destination / "keep").read_text() == "destination"

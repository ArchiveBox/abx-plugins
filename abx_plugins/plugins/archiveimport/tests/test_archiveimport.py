"""Exercise the real executable against public archive captures."""
import json
import os
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__34_archiveimport.py"


def run_hook(tmp_path, url, **settings):
    env = dict(os.environ, SNAP_DIR=str(tmp_path), **settings)
    return subprocess.run([str(HOOK), "--url", url], env=env, cwd=tmp_path,
                          text=True, capture_output=True, timeout=180)


def test_internet_archive_item(tmp_path):
    result = run_hook(tmp_path, "https://archive.org/details/hello-world.")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "succeeded"
    output = tmp_path / "archiveimport"
    metadata = json.loads((output / "metadata.json").read_text())
    assert metadata["metadata"]["identifier"] == "hello-world."
    assert metadata["metadata"]["title"] == "Hello World."
    files = metadata["archiveimport"]["files"]
    assert {entry["name"] for entry in files} == {entry["name"] for entry in metadata["files"]}
    for entry in files:
        assert (output / entry["path"]).stat().st_size == entry["size"]
    assert (output / "files/Hello world..txt").is_file()


def test_wayback_raw_response(tmp_path):
    result = run_hook(tmp_path, "https://web.archive.org/web/20091231215808/http://www.example.com/")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "succeeded"
    output = tmp_path / "archiveimport"
    metadata = json.loads((output / "metadata.json").read_text())["archiveimport"]
    assert metadata["source"] == "Wayback Machine raw response"
    assert metadata["capture"]["datetime"]
    assert b"Example Web Page" in (output / "response").read_bytes()
    assert metadata["files"][0]["size"] == (output / "response").stat().st_size


def test_missing_perma_key_and_disabled(tmp_path):
    result = run_hook(tmp_path, "https://perma.cc/W6PY-UZ99", ARCHIVEIMPORT_PERMA_API_KEY="")
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "noresults"
    result = run_hook(tmp_path, "https://archive.org/details/hello-world.", ARCHIVEIMPORT_ENABLED="false")
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "skipped"


def test_ghostarchive_original_warc_with_browser_user_agent(tmp_path):
    result = run_hook(tmp_path, "https://ghostarchive.org/archive/oKL6H",
                      USER_AGENT="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "succeeded", (result.stdout, result.stderr)
    output = tmp_path / "archiveimport"
    metadata = json.loads((output / "metadata.json").read_text())["archiveimport"]
    warc = output / "oKL6H.warc"
    assert metadata["source"] == "Ghostarchive"
    assert warc.read_bytes().startswith(b"WARC/1.0\r\n")
    assert warc.stat().st_size == metadata["files"][0]["size"] > 1000
    assert b"WARC-Type: response" in warc.read_bytes()

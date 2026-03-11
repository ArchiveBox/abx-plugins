import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.request import urlopen

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_hook_script,
    get_plugin_dir,
)


PLUGIN_DIR = get_plugin_dir(__file__)
PLUGINS_ROOT = PLUGIN_DIR.parent
_DEFUDDLE_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_defuddle.*")
if _DEFUDDLE_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
DEFUDDLE_HOOK = _DEFUDDLE_HOOK

_DEFUDDLE_CRAWL_HOOK = get_hook_script(PLUGIN_DIR, "on_Crawl__*_defuddle_install.*")
if _DEFUDDLE_CRAWL_HOOK is None:
    raise FileNotFoundError(f"Crawl hook not found in {PLUGIN_DIR}")
DEFUDDLE_CRAWL_HOOK = _DEFUDDLE_CRAWL_HOOK


TEST_URL = "https://example.com"
_defuddle_binary_path = None
_defuddle_lib_root = None


def create_example_html(tmpdir: Path) -> Path:
    """Create a local singlefile HTML fixture used as parser input."""
    singlefile_dir = tmpdir / "singlefile"
    singlefile_dir.mkdir(parents=True, exist_ok=True)
    html_file = singlefile_dir / "singlefile.html"
    html_file.write_text(
        "<html><head><title>Example Domain</title></head><body><article><h1>Example Domain</h1><p>Example text body</p></article></body></html>",
        encoding="utf-8",
    )
    return html_file


def require_defuddle_binary() -> str:
    """Return defuddle binary path or fail with actionable context."""
    binary_path = get_defuddle_binary_path()
    assert binary_path, (
        "defuddle installation failed. Install hook should install "
        "the binary automatically in this test environment."
    )
    assert Path(binary_path).is_file(), f"defuddle binary path invalid: {binary_path}"
    return binary_path


def get_defuddle_binary_path() -> str | None:
    """Get defuddle path from cache or by running install hooks."""
    global _defuddle_binary_path
    if _defuddle_binary_path and Path(_defuddle_binary_path).is_file():
        return _defuddle_binary_path

    from abx_pkg import Binary, EnvProvider, NpmProvider

    try:
        binary = Binary(
            name="defuddle",
            binproviders=[NpmProvider(), EnvProvider()],
            overrides={"npm": {"packages": ["defuddle"]}},
        ).load()
        if binary and binary.abspath:
            _defuddle_binary_path = str(binary.abspath)
            return _defuddle_binary_path
    except Exception:
        pass

    npm_hook = PLUGINS_ROOT / "npm" / "on_Binary__10_npm_install.py"
    if not npm_hook.exists():
        return None

    binary_id = str(uuid.uuid4())
    machine_id = str(uuid.uuid4())
    binproviders = "*"
    overrides = None

    crawl_result = subprocess.run(
        [sys.executable, str(DEFUDDLE_CRAWL_HOOK)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    for line in crawl_result.stdout.strip().split("\n"):
        if not line.strip().startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "Binary" and record.get("name") == "defuddle":
            binproviders = record.get("binproviders", "*")
            overrides = record.get("overrides")
            break

    global _defuddle_lib_root
    if not _defuddle_lib_root:
        _defuddle_lib_root = tempfile.mkdtemp(prefix="defuddle-lib-")

    env = os.environ.copy()
    env["LIB_DIR"] = str(Path(_defuddle_lib_root) / ".config" / "abx" / "lib")
    env["SNAP_DIR"] = str(Path(_defuddle_lib_root) / "data")
    env["CRAWL_DIR"] = str(Path(_defuddle_lib_root) / "crawl")

    cmd = [
        "uv",
        "run",
        str(npm_hook),
        "--binary-id",
        binary_id,
        "--machine-id",
        machine_id,
        "--name",
        "defuddle",
        f"--binproviders={binproviders}",
    ]
    if overrides:
        cmd.append(f"--overrides={json.dumps(overrides)}")

    install_result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )

    for line in install_result.stdout.strip().split("\n"):
        if not line.strip().startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "Binary" and record.get("name") == "defuddle":
            _defuddle_binary_path = record.get("abspath")
            return _defuddle_binary_path

    return None


def test_hook_script_exists():
    assert DEFUDDLE_HOOK.exists(), f"Hook script not found: {DEFUDDLE_HOOK}"


def test_crawl_hook_emits_defuddle_binary_record():
    result = subprocess.run(
        [sys.executable, str(DEFUDDLE_CRAWL_HOOK)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    records = [
        json.loads(line)
        for line in result.stdout.splitlines()
        if line.strip().startswith("{")
    ]
    assert records, "Expected crawl hook to emit Binary record"
    binary = records[0]
    assert binary.get("type") == "Binary"
    assert binary.get("name") == "defuddle"
    assert binary.get("overrides", {}).get("npm", {}).get("packages") == ["defuddle"]


def test_reports_missing_dependency_when_not_installed():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        create_example_html(snap_dir)

        env = {"PATH": "/nonexistent", "HOME": str(tmpdir), "SNAP_DIR": str(snap_dir)}
        result = subprocess.run(
            [
                sys.executable,
                str(DEFUDDLE_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test123",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env=env,
        )

        assert result.returncode == 1
        jsonl_lines = [
            line for line in result.stdout.strip().split("\n") if line.strip().startswith("{")
        ]
        assert len(jsonl_lines) == 1
        record = json.loads(jsonl_lines[0])
        assert record["type"] == "ArchiveResult"
        assert record["status"] == "failed"
        assert "defuddle" in result.stderr.lower() or "error" in result.stderr.lower()


def test_verify_deps_with_abx_pkg():
    binary_path = require_defuddle_binary()
    assert Path(binary_path).is_file()


def test_extracts_article_with_real_binary(httpserver):
    binary_path = require_defuddle_binary()
    test_url = httpserver.url_for("/defuddle-article")

    httpserver.expect_request("/defuddle-article").respond_with_data(
        "<html><head><title>Defuddle Test Article</title></head><body>"
        "<article><h1>Defuddle Test Article</h1>"
        "<p>This is test content for defuddle parser integration.</p>"
        "</article></body></html>",
        content_type="text/html; charset=utf-8",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        singlefile_dir = snap_dir / "singlefile"
        singlefile_dir.mkdir(parents=True, exist_ok=True)
        html_source = singlefile_dir / "singlefile.html"
        with urlopen(test_url, timeout=10) as response:
            page_html = response.read().decode("utf-8")
        html_source.write_text(
            page_html,
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["DEFUDDLE_BINARY"] = binary_path

        result = subprocess.run(
            [
                sys.executable,
                str(DEFUDDLE_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test456",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, result.stderr

        output_dir = snap_dir / "defuddle"
        assert (output_dir / "content.html").exists()
        assert (output_dir / "content.txt").exists()
        assert (output_dir / "article.json").exists()

        assert "defuddle parser integration" in (
            output_dir / "content.html"
        ).read_text(encoding="utf-8").lower()
        assert "defuddle parser integration" in (
            output_dir / "content.txt"
        ).read_text(encoding="utf-8").lower()
        metadata = json.loads((output_dir / "article.json").read_text(encoding="utf-8"))
        assert metadata.get("title")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    get_hook_script,
    get_plugin_dir,
)


PLUGIN_DIR = get_plugin_dir(__file__)
_DEFUDDLE_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_defuddle.*")
if _DEFUDDLE_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
DEFUDDLE_HOOK = _DEFUDDLE_HOOK

_DEFUDDLE_CRAWL_HOOK = get_hook_script(PLUGIN_DIR, "on_Crawl__*_defuddle_install.*")
if _DEFUDDLE_CRAWL_HOOK is None:
    raise FileNotFoundError(f"Crawl hook not found in {PLUGIN_DIR}")
DEFUDDLE_CRAWL_HOOK = _DEFUDDLE_CRAWL_HOOK


TEST_URL = "https://example.com"


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
        assert len(jsonl_lines) == 0
        assert "defuddle" in result.stderr.lower() or "error" in result.stderr.lower()


def test_extracts_article_with_json_output_from_binary():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        fake_binary = tmpdir / "fake_defuddle.py"
        fake_binary.write_text(
            "import json,sys; print(json.dumps({'content':'<article>Example</article>','textContent':'Example text','title':'Example Title'}))"
        )
        fake_binary.chmod(fake_binary.stat().st_mode | stat.S_IXUSR)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["DEFUDDLE_BINARY"] = sys.executable
        env["DEFUDDLE_ARGS"] = json.dumps([str(fake_binary)])

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

        assert "Example" in (output_dir / "content.html").read_text(encoding="utf-8")
        assert "Example text" in (output_dir / "content.txt").read_text(encoding="utf-8")
        metadata = json.loads((output_dir / "article.json").read_text(encoding="utf-8"))
        assert metadata.get("title") == "Example Title"

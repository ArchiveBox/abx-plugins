"""Integration tests for trafilatura plugin."""

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import get_hook_script, get_plugin_dir

PLUGIN_DIR = get_plugin_dir(__file__)
_TRAFILATURA_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_trafilatura.*")
if _TRAFILATURA_HOOK is None:
    raise FileNotFoundError(f"Hook not found in {PLUGIN_DIR}")
TRAFILATURA_HOOK = _TRAFILATURA_HOOK
TEST_URL = "https://example.com"


def create_fake_trafilatura(binary_path: Path) -> None:
    """Create a deterministic fake trafilatura CLI binary for tests."""
    binary_path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "fmt = 'txt'\n"
        "for i, arg in enumerate(sys.argv):\n"
        "    if arg == '--output-format' and i + 1 < len(sys.argv):\n"
        "        fmt = sys.argv[i + 1]\n"
        "payload = {\n"
        "    'txt': 'Example Domain plain text output',\n"
        "    'markdown': '# Example Domain\\n\\nMarkdown output',\n"
        "    'html': '<article><h1>Example Domain</h1><p>HTML output</p></article>',\n"
        "    'csv': 'title,text\\nExample Domain,CSV output',\n"
        "    'json': '{\"title\":\"Example Domain\"}',\n"
        "    'xml': '<doc><title>Example Domain</title></doc>',\n"
        "    'xmltei': '<TEI><title>Example Domain</title></TEI>',\n"
        "}\n"
        "sys.stdout.write(payload.get(fmt, ''))\n",
        encoding="utf-8",
    )
    binary_path.chmod(binary_path.stat().st_mode | stat.S_IEXEC)


def test_hook_script_exists():
    assert TRAFILATURA_HOOK.exists(), f"Hook script not found: {TRAFILATURA_HOOK}"


def test_extracts_local_html_outputs():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        (snap_dir / "singlefile").mkdir(parents=True, exist_ok=True)
        (snap_dir / "singlefile" / "singlefile.html").write_text(
            "<html><body><article><h1>Example Domain</h1>"
            "<p>This domain is for use in illustrative examples in documents.</p>"
            "</article></body></html>",
            encoding="utf-8",
        )

        fake_binary = tmpdir / "trafilatura"
        create_fake_trafilatura(fake_binary)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["TRAFILATURA_BINARY"] = str(fake_binary)
        env["TRAFILATURA_OUTPUT_JSON"] = "true"
        result = subprocess.run(
            [
                sys.executable,
                str(TRAFILATURA_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test123",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

        assert result.returncode == 0, f"Extraction failed: {result.stderr}"

        result_json = None
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    record = json.loads(line)
                    if record.get("type") == "ArchiveResult":
                        result_json = record
                        break
                except json.JSONDecodeError:
                    pass

        assert result_json and result_json["status"] == "succeeded"
        assert (snap_dir / "trafilatura" / "content.txt").exists()
        assert (snap_dir / "trafilatura" / "content.md").exists()
        assert (snap_dir / "trafilatura" / "content.html").exists()
        assert (snap_dir / "trafilatura" / "content.json").exists()


def test_fails_without_html_source():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        snap_dir = tmpdir / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)

        fake_binary = tmpdir / "trafilatura"
        create_fake_trafilatura(fake_binary)

        env = os.environ.copy()
        env["SNAP_DIR"] = str(snap_dir)
        env["TRAFILATURA_BINARY"] = str(fake_binary)
        result = subprocess.run(
            [
                sys.executable,
                str(TRAFILATURA_HOOK),
                "--url",
                TEST_URL,
                "--snapshot-id",
                "test999",
            ],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode != 0
        assert "no html source" in (result.stdout + result.stderr).lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

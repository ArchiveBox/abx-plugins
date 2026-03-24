from __future__ import annotations

import json
import subprocess
from pathlib import Path

from abx_plugins.plugins.base.utils import has_staticfile_output


REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_UTILS_JS = REPO_ROOT / "abx_plugins" / "plugins" / "base" / "utils.js"


def test_python_has_staticfile_output_accepts_succeeded_static_artifact(
    tmp_path: Path,
) -> None:
    staticfile_dir = tmp_path / "staticfile"
    staticfile_dir.mkdir()
    (staticfile_dir / "stdout.log").write_text(
        json.dumps(
            {
                "type": "ArchiveResult",
                "status": "succeeded",
                "output_str": "responses/example.com/test.json",
                "content_type": "application/json",
            },
        )
        + "\n",
        encoding="utf-8",
    )

    assert has_staticfile_output(str(staticfile_dir)) is True


def test_python_has_staticfile_output_rejects_html_noresults(tmp_path: Path) -> None:
    staticfile_dir = tmp_path / "staticfile"
    staticfile_dir.mkdir()
    (staticfile_dir / "stdout.log").write_text(
        json.dumps(
            {
                "type": "ArchiveResult",
                "status": "noresults",
                "output_str": "Page is HTML (not staticfile)",
                "content_type": "text/html",
            },
        )
        + "\n",
        encoding="utf-8",
    )

    assert has_staticfile_output(str(staticfile_dir)) is False


def test_js_has_staticfile_output_accepts_succeeded_static_artifact(
    tmp_path: Path,
) -> None:
    staticfile_dir = tmp_path / "staticfile"
    staticfile_dir.mkdir()
    (staticfile_dir / "stdout.log").write_text(
        json.dumps(
            {
                "type": "ArchiveResult",
                "status": "succeeded",
                "output_str": "staticfile/test.json",
                "content_type": "application/json",
            },
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "node",
            "-e",
            (
                "const { hasStaticFileOutput } = require("
                f"{json.dumps(str(BASE_UTILS_JS))}"
                ");"
                "console.log(hasStaticFileOutput(process.argv[1]) ? 'true' : 'false');"
            ),
            str(staticfile_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "true"


def test_js_has_staticfile_output_rejects_html_noresults(tmp_path: Path) -> None:
    staticfile_dir = tmp_path / "staticfile"
    staticfile_dir.mkdir()
    (staticfile_dir / "stdout.log").write_text(
        json.dumps(
            {
                "type": "ArchiveResult",
                "status": "noresults",
                "output_str": "Page is HTML (not staticfile)",
                "content_type": "text/html",
            },
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "node",
            "-e",
            (
                "const { hasStaticFileOutput } = require("
                f"{json.dumps(str(BASE_UTILS_JS))}"
                ");"
                "console.log(hasStaticFileOutput(process.argv[1]) ? 'true' : 'false');"
            ),
            str(staticfile_dir),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "false"

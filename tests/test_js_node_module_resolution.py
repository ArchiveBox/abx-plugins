from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_CRAWL_WAIT_HOOK,
    chrome_session,
    get_test_env,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
TITLE_HOOK = (
    REPO_ROOT / "abx_plugins" / "plugins" / "title" / "on_Snapshot__54_title.js"
)
CHROME_UTILS = REPO_ROOT / "abx_plugins" / "plugins" / "chrome" / "chrome_utils.js"

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


def _node_binary() -> str:
    node_binary = os.environ.get("NODE_BINARY")
    if not node_binary or not Path(node_binary).is_file():
        raise AssertionError("NODE_BINARY was not resolved by abxpkg")
    return node_binary


@pytest.fixture
def module_resolution_url(httpserver) -> str:
    httpserver.expect_request("/").respond_with_data(
        """
        <!doctype html>
        <html>
        <head><title>Module Alias Title</title></head>
        <body><h1>Module Alias Fixture</h1></body>
        </html>
        """.strip(),
        content_type="text/html",
    )
    return httpserver.url_for("/")


def test_title_hook_uses_abxpkg_exported_node_path(
    tmp_path: Path,
    module_resolution_url: str,
) -> None:
    with chrome_session(
        tmp_path,
        test_url=module_resolution_url,
        navigate=True,
        timeout=45,
    ) as (
        _process,
        _pid,
        snapshot_chrome_dir,
        env,
    ):
        snap_dir = snapshot_chrome_dir.parent
        title_dir = snap_dir / "title"
        title_dir.mkdir(exist_ok=True)
        hook_env = env.copy()
        hook_env["SNAP_DIR"] = str(snap_dir)
        assert hook_env["NODE_MODULES_DIR"]
        assert hook_env["NODE_PATH"]

        result = subprocess.run(
            [
                _node_binary(),
                str(TITLE_HOOK),
                f"--url={module_resolution_url}",
                "--snapshot-id=test-title",
            ],
            cwd=title_dir,
            capture_output=True,
            text=True,
            env=hook_env,
            timeout=60,
        )

    output_file = title_dir / "title.txt"
    assert result.returncode == 0, result.stderr
    assert output_file.read_text(encoding="utf-8") == "Module Alias Title"
    assert "Cannot find module" not in result.stderr


def test_chrome_wait_hook_resolves_puppeteer_from_lib_dir(
    tmp_path: Path,
    module_resolution_url: str,
) -> None:
    with chrome_session(
        tmp_path,
        test_url=module_resolution_url,
        navigate=False,
        timeout=45,
    ) as (
        _process,
        _pid,
        _snapshot_chrome_dir,
        env,
    ):
        lib_env = env.copy()
        node_modules_dir = Path(lib_env["NODE_MODULES_DIR"])
        lib_env.pop("NODE_MODULES_DIR", None)
        lib_env.pop("NODE_MODULE_DIR", None)
        lib_env.pop("NODE_PATH", None)
        lib_env["ABXPKG_LIB_DIR"] = str(node_modules_dir.parents[3])

        result = subprocess.run(
            [
                str(CHROME_CRAWL_WAIT_HOOK),
                f"--url={module_resolution_url}",
                "--snapshot-id=test-wait",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=lib_env,
            timeout=60,
        )

    assert result.returncode == 0, result.stderr
    assert "ready pid=" in result.stdout
    assert "Cannot find module" not in result.stderr


def test_chrome_launch_prerequisites_use_resolved_runtime_env(tmp_path: Path) -> None:
    real_env = get_test_env()
    real_node_modules = next(
        (
            Path(entry)
            for entry in real_env["NODE_PATH"].split(os.pathsep)
            if entry
            and (
                (Path(entry) / "puppeteer").exists()
                or (Path(entry) / "puppeteer-core").exists()
            )
        ),
        Path(real_env["NODE_MODULES_DIR"]),
    )
    real_chrome_binary = Path(os.environ["CHROME_BINARY"])
    assert real_node_modules.exists()
    assert real_chrome_binary.exists()

    chrome_utils_path = json.dumps(str(CHROME_UTILS))

    env = real_env.copy()
    env["CHROME_BINARY"] = str(real_chrome_binary)

    result = subprocess.run(
        [
            _node_binary(),
            "-e",
            f"""
const {{ getChromeLaunchPrerequisites }} = require({chrome_utils_path});
try {{
  const prereqs = getChromeLaunchPrerequisites();
  console.log(JSON.stringify({{
    hasPuppeteer: !!prereqs.puppeteer,
    binary: prereqs.binary,
  }}));
}} catch (error) {{
  console.error(error.message);
  process.exit(1);
}}
""".strip(),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["hasPuppeteer"] is True
    assert payload["binary"] == str(real_chrome_binary)

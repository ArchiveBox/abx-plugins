from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
TITLE_HOOK = (
    REPO_ROOT / "abx_plugins" / "plugins" / "title" / "on_Snapshot__54_title.js"
)
CHROME_WAIT_HOOK = (
    REPO_ROOT
    / "abx_plugins"
    / "plugins"
    / "chrome"
    / "on_Crawl__91_chrome_wait.js"
)


def _node_binary() -> str:
    node_binary = shutil.which("node")
    if not node_binary:
        raise AssertionError("Node.js is required for JS module resolution tests")
    return node_binary


def _write_stub_module(node_modules_dir: Path, package_name: str, contents: str) -> None:
    package_dir = node_modules_dir / package_name
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "index.js").write_text(contents, encoding="utf-8")


def test_title_hook_respects_node_module_dir_alias(tmp_path: Path) -> None:
    snap_dir = tmp_path / "snap"
    chrome_dir = snap_dir / "chrome"
    chrome_dir.mkdir(parents=True)
    (chrome_dir / "cdp_url.txt").write_text("ws://127.0.0.1:9222/devtools/browser/test\n", encoding="utf-8")
    (chrome_dir / "target_id.txt").write_text("target-1\n", encoding="utf-8")

    node_modules_dir = tmp_path / "alias_node_modules"
    _write_stub_module(
        node_modules_dir,
        "puppeteer-core",
        "module.exports = { connect: async () => ({}) };\n",
    )

    preload_path = tmp_path / "preload_title.js"
    preload_path.write_text(
        """
const Module = require('module');
const originalLoad = Module._load;

Module._load = function(request, parent, isMain) {
    if (request === '../chrome/chrome_utils.js') {
        return {
            connectToPage: async () => ({
                browser: { disconnect: () => {} },
                page: { title: async () => 'Resolved Title' },
            }),
            waitForPageLoaded: async () => {},
        };
    }
    return originalLoad(request, parent, isMain);
};
""".lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.pop("NODE_MODULES_DIR", None)
    env["NODE_MODULE_DIR"] = str(node_modules_dir)
    env["SNAP_DIR"] = str(snap_dir)

    result = subprocess.run(
        [
            _node_binary(),
            "--require",
            str(preload_path),
            str(TITLE_HOOK),
            "--url=https://example.com",
            "--snapshot-id=test-title",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    output_file = snap_dir / "title" / "title.txt"
    assert result.returncode == 0, result.stderr
    assert output_file.exists()
    assert output_file.read_text(encoding="utf-8") == "Resolved Title"
    assert "Cannot find module 'puppeteer-core'" not in result.stderr


def test_chrome_wait_hook_resolves_puppeteer_from_lib_dir(tmp_path: Path) -> None:
    lib_dir = tmp_path / "lib"
    node_modules_dir = lib_dir / "npm" / "node_modules"
    _write_stub_module(
        node_modules_dir,
        "puppeteer",
        """
module.exports = {
  connect: async () => ({
    disconnect: () => {},
  }),
};
""".lstrip(),
    )

    preload_path = tmp_path / "preload_chrome_wait.js"
    preload_path.write_text(
        """
const Module = require('module');
const originalLoad = Module._load;

Module._load = function(request, parent, isMain) {
    if (request === './chrome_utils.js') {
        const actual = originalLoad(request, parent, isMain);
        return {
            ...actual,
            waitForChromeSessionState: async () => ({
                cdpUrl: 'ws://127.0.0.1:9222/devtools/browser/test',
                pid: 4321,
            }),
        };
    }
    return originalLoad(request, parent, isMain);
};
""".lstrip(),
        encoding="utf-8",
    )

    crawl_dir = tmp_path / "crawl"
    env = os.environ.copy()
    env.pop("NODE_MODULES_DIR", None)
    env.pop("NODE_MODULE_DIR", None)
    env["LIB_DIR"] = str(lib_dir)
    env["CRAWL_DIR"] = str(crawl_dir)

    result = subprocess.run(
        [
            _node_binary(),
            "--require",
            str(preload_path),
            str(CHROME_WAIT_HOOK),
            "--url=https://example.com",
            "--snapshot-id=test-wait",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "browser ready pid=4321" in result.stdout
    assert "Cannot find module 'puppeteer'" not in result.stderr

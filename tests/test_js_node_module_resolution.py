from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
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
            waitForNavigationComplete: async () => {},
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


def test_chrome_launch_prerequisites_wait_for_late_installs(tmp_path: Path) -> None:
    lib_dir = tmp_path / "lib"
    node_modules_dir = lib_dir / "npm" / "node_modules"
    puppeteer_dir = node_modules_dir / "puppeteer"
    chrome_binary = tmp_path / "bin" / "chromium"
    chrome_utils_path = json.dumps(
        str(REPO_ROOT / "abx_plugins" / "plugins" / "chrome" / "chrome_utils.js")
    )

    def materialize_prereqs() -> None:
        time.sleep(0.5)
        puppeteer_dir.mkdir(parents=True, exist_ok=True)
        (puppeteer_dir / "index.js").write_text("module.exports = { launch: async () => ({}) };\n", encoding="utf-8")
        chrome_binary.parent.mkdir(parents=True, exist_ok=True)
        chrome_binary.write_text("#!/bin/sh\necho Chromium 123\n", encoding="utf-8")
        chrome_binary.chmod(0o755)

    writer = threading.Thread(target=materialize_prereqs, daemon=True)
    writer.start()

    env = os.environ.copy()
    env["LIB_DIR"] = str(lib_dir)
    env["CHROME_BINARY"] = str(chrome_binary)

    result = subprocess.run(
        [
            _node_binary(),
            "-e",
            f"""
const {{ waitForChromeLaunchPrerequisites }} = require({chrome_utils_path});
(async () => {{
  const startedAt = Date.now();
  const prereqs = await waitForChromeLaunchPrerequisites({{
    timeoutMs: 5000,
    initialIntervalMs: 50,
    maxIntervalMs: 100,
  }});
  console.log(JSON.stringify({{
    waitedMs: Date.now() - startedAt,
    hasPuppeteer: !!prereqs.puppeteer,
    binary: prereqs.binary,
  }}));
}})().catch(error => {{
  console.error(error.message);
  process.exit(1);
}});
""".strip(),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    writer.join(timeout=2)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["hasPuppeteer"] is True
    assert payload["binary"] == str(chrome_binary)
    assert payload["waitedMs"] >= 400

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SINGLEFILE_HELPER = (
    REPO_ROOT
    / "abx_plugins"
    / "plugins"
    / "singlefile"
    / "singlefile_extension_save.js"
)


def test_singlefile_helper_honors_node_modules_dir(tmp_path: Path) -> None:
    """singlefile helper should resolve puppeteer-core from NODE_MODULES_DIR."""
    node_binary = shutil.which("node")
    if not node_binary:
        raise AssertionError("Node.js is required for singlefile helper tests")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    output_path = run_dir / "singlefile.html"

    node_modules_dir = tmp_path / "node_modules"
    puppeteer_dir = node_modules_dir / "puppeteer-core"
    puppeteer_dir.mkdir(parents=True)
    (puppeteer_dir / "index.js").write_text(
        "module.exports = { connect: async () => ({}) };\n",
        encoding="utf-8",
    )

    preload_path = tmp_path / "preload.js"
    preload_path.write_text(
        """
const fs = require('fs');
const Module = require('module');
const originalLoad = Module._load;

Module._load = function(request, parent, isMain) {
    if (request === '../chrome/chrome_utils.js') {
        return {
            installExtensionWithCache: async () => ({ name: 'singlefile', version: 'test' }),
            connectToPage: async () => ({
                browser: { disconnect: async () => {} },
                page: {
                    url: async () => process.env.TEST_URL,
                    goto: async () => {},
                    createCDPSession: async () => ({ send: async () => {} }),
                    target: () => ({ createCDPSession: async () => ({ send: async () => {} }) }),
                },
            }),
            readExtensionsMetadata: () => [{ name: 'singlefile', id: 'test-extension-id' }],
            findExtensionMetadataByName: () => ({ id: 'test-extension-id' }),
            waitForExtensionTargetHandle: async () => ({}),
            loadExtensionFromTarget: async (extensions) => {
                extensions[0].dispatchAction = async () => {};
            },
            setBrowserDownloadBehavior: async () => true,
        };
    }

    if (request === './on_Install__82_singlefile.js') {
        return {
            EXTENSION: { name: 'singlefile' },
            saveSinglefileWithExtension: async (_page, _extension, options) => {
                fs.writeFileSync(options.outputPath, '<!DOCTYPE html><html><body>ok</body></html>');
                return options.outputPath;
            },
        };
    }

    return originalLoad(request, parent, isMain);
};
""".lstrip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["NODE_MODULES_DIR"] = str(node_modules_dir)
    env["TEST_URL"] = "https://example.com"

    result = subprocess.run(
        [
            node_binary,
            "--require",
            str(preload_path),
            str(SINGLEFILE_HELPER),
            "--url=https://example.com",
            f"--output-path={output_path}",
        ],
        cwd=run_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert output_path.exists(), (
        "singlefile helper should emit the requested output file"
    )
    assert "Cannot find module 'puppeteer-core'" not in result.stderr
    assert "[singlefile] dependencies loaded" in result.stderr

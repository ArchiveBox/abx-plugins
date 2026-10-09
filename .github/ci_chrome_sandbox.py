"""Find the sandbox shipped with the Chrome installed for this CI job."""

import json
import shlex
import subprocess
from pathlib import Path

import abx_plugins


def sandbox_for_browser(browser: Path) -> Path:
    browser = browser.resolve(strict=True)
    # abxpkg's Puppeteer provider uses an exec launcher on Linux, whereas
    # Playwright uses a symlink. Path.resolve() only follows the latter.
    with browser.open("rb") as executable:
        launcher = executable.read(4096)
    if launcher.startswith(b"#!/bin/sh\nexec "):
        command = shlex.split(launcher.decode().splitlines()[1])
        assert len(command) == 3 and command[0] == "exec" and command[2] == "$@", (
            browser
        )
        browser = Path(command[1]).resolve(strict=True)
    helper = browser.with_name("chrome_sandbox")
    assert helper.is_file(), helper
    return helper


if __name__ == "__main__":
    config = Path(abx_plugins.__file__).parent / "plugins/chrome/config.json"
    env = json.loads(
        subprocess.check_output(
            [
                "abxpkg",
                "env",
                "--install",
                "--json",
                f"--deps-from={config}:required_binaries",
                "browsers",
            ],
            text=True,
        ),
    )
    print(sandbox_for_browser(Path(env["CHROME_BINARY"])))

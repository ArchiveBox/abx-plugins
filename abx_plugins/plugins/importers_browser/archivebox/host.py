#!/usr/bin/env -S abxpkg run --script python3
# /// script
# requires-python = ">=3.12"
# ///
"""Optional ArchiveBox browser handoff; standalone importers use supplied CDP env."""

import os
import runpy
import select
import signal
import subprocess
import sys
import time
from contextlib import chdir, contextmanager
from pathlib import Path


@contextmanager
def connection(run_dir):
    from archivebox.config.django import setup_django

    setup_django(check_db=True)
    from abx_plugins.plugins.opencode import runtime
    from abx_plugins.plugins.opencode.archivebox.browser import (
        browser_environment,
        published_browsers,
    )
    from archivebox.config.common import get_config
    from archivebox.config.constants import CONSTANTS
    from archivebox.personas.models import Persona

    name = os.environ.get("ACTIVE_PERSONA")
    persona = Persona.find_named(name) if name else None
    if persona is None:
        raise ValueError("Select a server persona before running a browser importer.")
    config = get_config(persona=persona).model_dump(mode="json")
    settings = runtime._settings(config, CONSTANTS.DATA_DIR)
    binary, env = runtime._resolve_binary(settings["binary"], config)
    owner = None
    log = None
    try:
        # Reuse the same published persona browser as OpenCode. A cold persona is
        # opened by ArchiveBox's existing browser owner, with its normal hydration.
        if not any(
            source.get("persona") == persona.name
            for browser in published_browsers()
            for source in browser["sources"]
        ):
            log = (run_dir / "browser.log").open("w")
            owner = subprocess.Popen(
                [
                    str(Path(sys.executable).with_name("archivebox")),
                    "persona",
                    "open",
                    persona.name,
                    "--headless",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                env=os.environ.copy(),
                cwd=CONSTANTS.DATA_DIR,
            )
            assert owner.stdout is not None
            deadline = time.monotonic() + 60
            ready = False
            while time.monotonic() < deadline and owner.poll() is None:
                if select.select(
                    [owner.stdout],
                    [],
                    [],
                    max(0, min(1, deadline - time.monotonic())),
                )[0] and owner.stdout.readline().startswith(
                    "Browser ready for persona ",
                ):
                    ready = True
                    break
            if not ready:
                raise RuntimeError(
                    "Persona browser did not become ready; see this run's browser.log.",
                )
        env.update(browser_environment(persona_name=persona.name, remember=False))
        binary_path = binary.loaded_abspath
        if binary.loaded_binprovider is not None:
            binary_path = binary.loaded_binprovider._exec_bin_abspath(Path(binary_path))
        # Use the same provider credentials/config as the Agent UI, but its own
        # run directory and session. Do not start or change the web agent server.
        env = runtime.process_environment(settings, env)
        yield str(binary_path), env
    finally:
        if owner is not None and owner.poll() is None:
            owner.terminate()
            try:
                owner.wait(timeout=15)
            except subprocess.TimeoutExpired:
                owner.kill()
                owner.wait(timeout=5)
        if log:
            log.close()


if __name__ == "__main__":

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    run_dir = Path.cwd()
    with chdir(os.environ["DATA_DIR"]), connection(run_dir) as (binary, env):
        # Run in this process so cancellation lets the importer clean up its
        # learner before the host closes the persona browser.
        env["OPENCODE_BINARY"] = binary
        os.environ.update(env)
        with chdir(run_dir):
            runpy.run_path(
                str(Path(__file__).parents[1] / "importer.py"),
                run_name="__main__",
            )

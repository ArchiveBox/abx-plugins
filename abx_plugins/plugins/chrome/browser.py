#!/usr/bin/env -S abxpkg run --script python3
# /// script
# requires-python = ">=3.12"
# ///
"""Open, export, or recover browser sessions; JSON request on stdin."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from abxpkg import BinProvider
from abx_plugins.plugins.base.utils import load_config, load_required_binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=("open", "export-state", "recover", "profile"),
    )
    parser.add_argument("--persona-dir")
    parser.add_argument("--persona-name")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    operation = args.operation
    if args.persona_dir:
        if operation != "open":
            parser.error("--persona-dir requires open")
        request = {"persona_dir": args.persona_dir, "headless": args.headless}
        if args.persona_name:
            request["name"] = args.persona_name
    else:
        request = json.load(sys.stdin)
    if operation == "open" and not request.get("persona_dir"):
        raise ValueError("persona_dir is required")
    if operation == "export-state" and not (
        request.get("user_data_dir") or request.get("cdp_url")
    ):
        raise ValueError("user_data_dir or cdp_url is required")
    if operation == "profile":
        from abx_plugins.plugins.chrome.profiles import run

        print(json.dumps({"path": str(run(request))}))
        return
    config_path = Path(__file__).with_name("config.json")
    request_config = request.get("config", {})
    config_env = {
        **os.environ,
        **{
            key: value if isinstance(value, str) else json.dumps(value)
            for key, value in request_config.items()
        },
    }
    payload = load_config(
        config_path,
        global_config=request_config,
        environ=config_env,
        hydrate_binaries=False,
    ).model_dump(mode="json")
    if operation == "open":
        persona = Path(request["persona_dir"])
        payload.update(
            ACTIVE_PERSONA=request.get("name", persona.name),
            PERSONAS_DIR=str(persona.parent),
            CHROME_USER_DATA_DIR=str(persona / "chrome_profile"),
            CHROME_HEADLESS=bool(request.get("headless")),
        )
    else:
        if request.get("user_data_dir"):
            payload.update(
                CHROME_USER_DATA_DIR=request["user_data_dir"],
                CHROME_CDP_URL="",
                CHROME_IS_LOCAL=True,
            )
        if request.get("cdp_url"):
            payload.update(CHROME_CDP_URL=request["cdp_url"], CHROME_IS_LOCAL=False)
        if request.get("browser_binary"):
            payload["CHROME_BINARY"] = request["browser_binary"]
    binaries = {}
    if operation == "recover":
        from abxpkg.exceptions import BinaryLoadError

        paths = request.get("paths")
        if not isinstance(paths, list) or any(
            not isinstance(path, str) or not Path(path).is_absolute() for path in paths
        ):
            raise ValueError("paths must be a list of absolute runtime roots")
        dependency = next(
            item
            for item in json.loads(config_path.read_text())["required_binaries"]
            if item["name"] == "{NODE_BINARY}"
        )
        try:
            node = load_required_binary(dependency, config=payload, install=False)
        except BinaryLoadError:
            print(json.dumps({"processes_orphaned": 0}))
            return
        env = BinProvider.build_exec_env(
            providers=[node.loaded_binprovider] if node.loaded_binprovider else [],
            base_env=os.environ,
        )
        os.execvpe(
            str(node.abspath),
            [
                str(node.abspath),
                str(Path(__file__).with_name("chrome_utils.js")),
                "recover",
                *paths,
            ],
            env,
        )
    for dependency in json.loads(config_path.read_text())["required_binaries"]:
        name = dependency["name"]
        if operation == "export-state" and name == "{CHROME_BINARY}":
            continue
        binaries[name] = load_required_binary(dependency, config=payload, install=True)
        if name.startswith("{") and name.endswith("}"):
            payload[name[1:-1]] = str(binaries[name].abspath)
    env = BinProvider.build_exec_env(
        providers=[
            binary.loaded_binprovider
            for binary in binaries.values()
            if binary.loaded_binprovider
        ],
        base_env=os.environ,
    )
    env.update(
        {
            key: value if isinstance(value, str) else json.dumps(value)
            for key, value in payload.items()
            if value is not None
        },
    )
    node = str(binaries["{NODE_BINARY}"].abspath)
    env["NODE_BINARY"] = node
    env["NODE_MODULES_DIR"] = str(
        Path(payload["ABXPKG_LIB_DIR"]) / "pnpm/packages/chrome/node_modules",
    )
    if operation == "open":
        import psutil

        collection = Path(
            request.get("collection_dir") or request["persona_dir"],
        ).resolve()
        if os.environ.get("DISPLAY") and not request.get("headless"):
            for process in psutil.process_iter():
                try:
                    args = process.cmdline()
                    profile = next(
                        (
                            arg.split("=", 1)[1]
                            for arg in args
                            if arg.startswith("--user-data-dir=")
                        ),
                        None,
                    )
                    if (
                        profile
                        and not any(
                            arg.startswith(("--type=", "--headless")) for arg in args
                        )
                        and Path(profile).resolve().is_relative_to(collection)
                        and process.environ().get("DISPLAY") == os.environ["DISPLAY"]
                    ):
                        print(
                            "A browser is already open on this display; waiting for it to close.",
                            file=sys.stderr,
                        )
                        process.wait()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        with tempfile.TemporaryFile(mode="w+") as source:
            json.dump(payload, source)
            source.seek(0)
            os.dup2(source.fileno(), 0)
            os.execvpe(
                node,
                [node, str(Path(__file__).with_name("open_browser.js"))],
                env,
            )
    else:
        env["CHROME_PROFILE_DIRECTORY"] = request.get("profile_dir") or "Default"
        env["COOKIES_OUTPUT_FILE"] = request.get("cookies_output_file", "")
        env["AUTH_STORAGE_OUTPUT_FILE"] = request.get("auth_output_file", "")
        os.execvpe(
            node,
            [node, str(Path(__file__).with_name("export_browser_state.js"))],
            env,
        )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, IndexError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env -S abxpkg run --script python3
# /// script
# requires-python = ">=3.12"
# ///
"""Open, export, or recover browser sessions; JSON request on stdin."""

import argparse
import json
import os
import re
from pathlib import Path
import sys
import tempfile

from abxpkg import BinProvider
from abx_plugins.plugins.base.utils import load_config, load_required_binary


def local_crawl_profile_lock(profile, browser_cwd, roots, tmp_dir):
    """Read the browser lock only for a verified crawl's local Persona."""
    if (
        os.name != "posix"
        or not browser_cwd
        or not isinstance(tmp_dir, str)
        or not Path(tmp_dir).is_absolute()
    ):
        return None
    cwd = Path(browser_cwd)
    crawl_id = cwd.parent.name
    if (
        not cwd.is_absolute()
        or len(cwd.parents) < 4
        or cwd.name != "chrome"
        or not re.fullmatch(r"[0-9a-f]{32}", crawl_id)
        or not re.fullmatch(r"[0-9]{8}", cwd.parents[2].name)
    ):
        return None
    if cwd.parents[3].name != "crawls" or cwd.parents[3] not in roots:
        return None

    # Core puts browser state on local storage and leaves this link in the
    # crawl output directory. Inspect only that link, not the archive tree.
    tmp_root = Path(os.path.abspath(tmp_dir))
    target = tmp_root / "personas" / crawl_id / ".persona"
    if profile.name != "chrome_profile" or profile.parent.parent != target:
        return None
    try:
        if Path(os.readlink(cwd.parent / ".persona")) != target:
            return None
        # Do not follow a substituted directory link outside the declared
        # local runtime subtree while reading the native SingletonLock.
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(tmp_root, flags)
        try:
            for component in profile.relative_to(tmp_root).parts:
                next_fd = os.open(component, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return os.readlink("SingletonLock", dir_fd=fd)
        finally:
            os.close(fd)
    except OSError:
        return None


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
    profile_state = operation == "profile" and request.get("action") in {
        "remove-domain",
        "import-state",
        "inspect-domains",
    }
    if operation == "profile" and not profile_state:
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
        if profile_state:
            user_data_dir = Path(request["user_data_dir"])
            if not user_data_dir.is_absolute():
                raise ValueError("user_data_dir must be an absolute directory")
            if request["action"] == "import-state":
                user_data_dir.mkdir(parents=True, exist_ok=True)
            elif not user_data_dir.is_dir():
                raise ValueError("user_data_dir must be an existing directory")
            profile_dir = request.get("profile_dir") or "Default"
            if Path(profile_dir).name != profile_dir or profile_dir in {".", ".."}:
                raise ValueError("profile_dir must name a single browser profile")
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
    recovery_pids = None
    if operation == "recover":
        import psutil

        from abxpkg.exceptions import BinaryLoadError

        paths = request.get("paths")
        if not isinstance(paths, list) or any(
            not isinstance(path, str) or not Path(path).is_absolute() for path in paths
        ):
            raise ValueError("paths must be a list of absolute runtime roots")
        # Start with real browsers, never historical PID files or archive trees.
        # Chrome's native profile lock is published at process startup, before
        # CDP hydration/optional observers, and stays on the local runtime volume.
        roots = [Path(os.path.abspath(path)) for path in paths]
        recovery_pids = set()
        processes = list(psutil.process_iter(["cmdline", "cwd", "status"]))
        hook_names = {
            "on_CrawlSetup__90_chrome_launch.daemon.bg.js",
            "on_Snapshot__00_chrome_launch.daemon.bg.js",
            "on_Snapshot__01_chrome_tab.daemon.bg.js",
        }
        owners = [
            process
            for process in processes
            if process.info["status"] != psutil.STATUS_ZOMBIE
            and any(
                Path(arg).name in hook_names for arg in process.info["cmdline"] or []
            )
        ]
        for process in processes:
            command = process.info["cmdline"] or []
            if process.info["status"] == psutil.STATUS_ZOMBIE or not any(
                arg.startswith("--remote-debugging-port=")
                or arg == "--remote-debugging-pipe"
                for arg in command
            ):
                continue
            profile_arg = next(
                (
                    arg.split("=", 1)[1]
                    for arg in command
                    if arg.startswith("--user-data-dir=")
                ),
                "",
            )
            # Normalize ../ lexically without resolving archive-backed links.
            profile = Path(os.path.abspath(profile_arg))
            browser_cwd = process.info["cwd"]
            if not Path(profile_arg).is_absolute():
                continue
            try:
                lock = (
                    os.readlink(profile / "SingletonLock")
                    if any(profile.is_relative_to(root) for root in roots)
                    else local_crawl_profile_lock(
                        profile, browser_cwd, roots, request_config.get("TMP_DIR")
                    )
                )
                host, _, locked_pid = (lock or "").rpartition("-")
                if not host or int(locked_pid) != process.pid:
                    continue
            except (OSError, ValueError):
                continue  # An unrelated profile or an exited browser owns no session.
            owned = False
            for owner in owners:
                try:
                    if owner in process.parents() or (
                        browser_cwd and owner.info["cwd"] == browser_cwd
                    ):
                        owned = True
                        break
                    # A tab daemon can outlive its launch hook and still own the
                    # shared crawl browser. Compare its declared runtime identity
                    # lexically; resolving archive paths would touch remote storage.
                    env = owner.environ()
                    owner_profile = env.get("CHROME_USER_DATA_DIR") or (
                        str(
                            Path(env["PERSONAS_DIR"])
                            / (env.get("ACTIVE_PERSONA") or "Default")
                            / "chrome_profile",
                        )
                        if env.get("PERSONAS_DIR")
                        else ""
                    )
                    if (
                        owner_profile
                        and Path(os.path.abspath(owner_profile)) == profile
                    ) or (
                        browser_cwd
                        and env.get("CRAWL_DIR")
                        and str(Path(env["CRAWL_DIR"]) / "chrome") == browser_cwd
                    ):
                        owned = True
                        break
                except psutil.NoSuchProcess:
                    continue
                except psutil.AccessDenied:
                    owned = True  # Ownership cannot safely be disproved.
                    break
            if not owned:
                recovery_pids.add(process.pid)
        if not recovery_pids:
            print(json.dumps({"processes_orphaned": 0}))
            return
        dependency = next(
            item
            for item in json.loads(config_path.read_text())["required_binaries"]
            if item["name"] == "{NODE_BINARY}"
        )
        try:
            binaries["{NODE_BINARY}"] = load_required_binary(
                dependency,
                config=payload,
                install=False,
            )
        except BinaryLoadError:
            print(json.dumps({"processes_orphaned": 0}))
            return
    else:
        for dependency in json.loads(config_path.read_text())["required_binaries"]:
            name = dependency["name"]
            if operation == "export-state" and name == "{CHROME_BINARY}":
                continue
            binaries[name] = load_required_binary(
                dependency,
                config=payload,
                install=True,
            )
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
    if recovery_pids is not None:
        # Recovery uses the same JS helper as browser operations, so it needs
        # the same resolved module environment even though it never installs.
        os.execvpe(
            node,
            [
                node,
                str(Path(__file__).with_name("chrome_utils.js")),
                "recover",
                *(str(pid) for pid in sorted(recovery_pids)),
            ],
            env,
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
        if profile_state:
            with tempfile.TemporaryFile(mode="w+") as source:
                json.dump(request, source)
                source.seek(0)
                os.dup2(source.fileno(), 0)
                os.execvpe(
                    node,
                    [node, str(Path(__file__).with_name("profile_state.js"))],
                    env,
                )
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

"""Attach OpenCode tools to a specific ArchiveBox capture's existing browser."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def _chrome_environment(config):
    from abx_plugins.plugins.base.utils import load_required_binary_from_config
    from abxpkg import BinProvider

    chrome_config = Path(__file__).parents[2] / "chrome" / "config.json"
    binaries = [
        load_required_binary_from_config(
            name,
            chrome_config,
            global_config=config,
            install=False,
        )
        for name in ("node", "browsers")
    ]
    return BinProvider.build_exec_env(
        providers=[
            binary.loaded_binprovider
            for binary in binaries
            if binary.loaded_binprovider
        ],
        base_env=os.environ.copy(),
    )


def browser_live_dir(tmp_dir, endpoint: str) -> Path:
    """Match chrome/browser_observer.js: hot tab/viewer/frame state lives on
    local TMP_DIR, not on possibly-remote crawl/persona storage."""
    return (
        Path(tmp_dir or tempfile.gettempdir())
        / "chrome_browser"
        / hashlib.sha256(endpoint.encode()).hexdigest()
    )


def published_browsers(config, *, snapshot_targets=False):
    """Discover live published sessions, grouped by browser WebSocket identity.

    The Agent panel polls this every second. Session dirs live on archive
    storage that may be remote (e.g. rclone FUSE), and probing their
    descriptor files for every persona and running chrome process made each
    poll do dozens of network round trips. Each browser's observer publishes
    its endpoint, session dir, PID and tabs on local TMP_DIR, so discovery
    reads only those files plus process rows the runner already owns.
    """
    from archivebox.machine.models import Machine, Process
    from archivebox.personas.models import Persona

    processes = list(
        Process.objects.filter(
            machine=Machine.current_readonly(),
            status=Process.StatusChoices.RUNNING,
            pwd__endswith="/chrome",
        ).values(
            "pwd",
            "cmd",
            "env__CRAWL_DIR",
            "env__ACTIVE_PERSONA",
            "env__TMP_DIR",
        ),
    )
    # Crawl-isolated snapshot hooks attach to their crawl's browser, whose
    # session dir is CRAWL_DIR/chrome; snapshot-isolated ones own their pwd.
    sources_by_dir = {}
    for process in processes:
        source = {
            key: arg.split("=", 1)[1]
            for key in ("crawl", "snapshot")
            for arg in process["cmd"]
            if arg.startswith(f"--{key}-id=")
        }
        if process["env__CRAWL_DIR"]:
            source["crawl"] = Path(process["env__CRAWL_DIR"]).name
        if process["env__ACTIVE_PERSONA"]:
            source["persona_name"] = process["env__ACTIVE_PERSONA"]
        session_dirs = {process["pwd"]}
        if process["env__CRAWL_DIR"]:
            session_dirs.add(str(Path(process["env__CRAWL_DIR"]) / "chrome"))
        for session_dir in session_dirs:
            sources_by_dir.setdefault(session_dir, []).append(
                (Path(process["pwd"]), source),
            )
    # Map persona session dirs by name: Persona.path lists PERSONAS_DIR.
    personas = {
        name.casefold(): name for name in Persona.objects.values_list("name", flat=True)
    }
    # Owners publish under their own TMP_DIR. CLI and server startup resolve
    # the same collection TMP_DIR; runner hooks record theirs on Process rows.
    tmp_dirs = {
        config.get("TMP_DIR") or tempfile.gettempdir(),
        *(process["env__TMP_DIR"] for process in processes if process["env__TMP_DIR"]),
    }
    sessions = {}
    for tmp_dir in tmp_dirs:
        try:
            live_dirs = list(
                (Path(tmp_dir) / "chrome_browser").iterdir(),
            )
        except OSError:
            continue
        for live_dir in live_dirs:
            try:
                state = json.loads((live_dir / "tabs.json").read_text())
                endpoint = state["cdp_url"]
                if not state["connected"] or live_dir != browser_live_dir(
                    tmp_dir,
                    endpoint,
                ):
                    continue
                os.kill(int(state["owner_pid"]), 0)
                directory = Path(state["session_dir"])
            except (OSError, ValueError, TypeError, KeyError):
                continue  # Stale/partial artifacts cannot take down the agent UI.
            browser_id = hashlib.sha256(endpoint.encode()).hexdigest()[:16]
            sources = []
            if directory.name == ".browser" and (
                persona := personas.get(directory.parent.name.casefold())
            ):
                sources.append({"persona": persona})
            for source_dir, source in sources_by_dir.get(str(directory), []):
                if snapshot_targets:
                    # Explicit agent listings only; never on the panel poll.
                    try:
                        target_id = (source_dir / "target_id.txt").read_text()
                        source = {**source, "target_id": target_id.strip()}
                    except OSError:
                        pass
                if source not in sources:
                    sources.append(source)
            sessions[browser_id] = {
                "id": browser_id,
                "cdp_url": endpoint,
                "sources": sources,
                "pid": state.get("pid"),
                "available": True,
                "tabs": state["tabs"],
                "directory": str(directory),
                "live_dir": str(live_dir),
            }
    return list(sessions.values())


def browser_inventory(config, *, snapshot_targets=False):
    """Read owner-published state only: no CDP, installs, or child processes."""
    return [
        {
            key: value
            for key, value in session.items()
            if key not in ("directory", "live_dir")
        }
        for session in published_browsers(config, snapshot_targets=snapshot_targets)
    ]


def selection_path(config):
    from abx_plugins.plugins.opencode import runtime

    return runtime._settings(config)["opencode_dir"] / "browser.json"


def save_selection(config, selection):
    filename = selection_path(config)
    filename.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=filename.parent,
        delete=False,
    ) as handle:
        json.dump(selection, handle)
    Path(handle.name).replace(filename)


def browser_environment(
    *,
    persona_name: str | None = None,
    snapshot_id: str | None = None,
    crawl_id: str | None = None,
    browser_id: str | None = None,
    target_id: str | None = None,
    current: bool = False,
    remember: bool = True,
) -> dict[str, str]:
    from archivebox.config.common import get_config
    from archivebox.core.models import Snapshot
    from archivebox.crawls.models import Crawl
    from archivebox.personas.models import Persona
    from abx_plugins.plugins.opencode import runtime

    if (
        sum(
            bool(value)
            for value in (persona_name, snapshot_id, crawl_id, browser_id, current)
        )
        != 1
    ):
        raise ValueError(
            "Select exactly one persona, snapshot, crawl, browser or current UI selection",
        )
    snapshot = (
        Snapshot.objects.select_related("crawl").get(pk=snapshot_id)
        if snapshot_id
        else None
    )
    crawl = (
        snapshot.crawl
        if snapshot
        else Crawl.objects.get(pk=crawl_id)
        if crawl_id
        else None
    )
    persona = (
        Persona.find_named(persona_name)
        if persona_name
        else crawl.resolve_persona()
        if crawl
        else None
    )
    if persona_name and persona is None:
        raise ValueError(f"Persona not found: {persona_name}")
    config = get_config(snapshot=snapshot, crawl=crawl, persona=persona).model_dump(
        mode="json",
    )
    if crawl and not snapshot and config.get("CHROME_ISOLATION") == "snapshot":
        raise ValueError("This crawl uses snapshot isolation; select a snapshot")
    _, env = runtime._resolve_binary(
        str(config.get("OPENCODE_BINARY") or "opencode"),
        config,
    )
    chrome_env = _chrome_environment(config)
    env.update(
        {key: value for key, value in chrome_env.items() if key.startswith("NODE_")},
    )
    if browser_id or current:
        selected = (
            json.loads(selection_path(config).read_text())
            if current
            else {"id": browser_id, "target_id": target_id}
        )
        session = next(
            (
                item
                for item in published_browsers(config)
                if item["id"] == selected["id"] and item["available"]
            ),
            None,
        )
        if not session:
            raise ValueError("Selected browser is no longer available")
        target_id = target_id or selected.get("target_id")
        if target_id and not any(
            tab["target_id"] == target_id and tab["available"]
            for tab in session["tabs"]
        ):
            raise ValueError("Selected tab is no longer available")
        session_dir = Path(session["directory"])
        persona_name = next(
            (
                source["persona"]
                for source in session["sources"]
                if source.get("persona")
            ),
            None,
        )
    else:
        if persona_name:
            assert persona is not None
            session_dir = persona.path / ".browser"
        else:
            capture = snapshot or crawl
            assert capture is not None
            session_dir = Path(capture.output_dir) / "chrome"
    # Probe only when the agent attaches, never on a panel request.
    result = subprocess.run(
        [
            env.get("NODE_BINARY") or "node",
            str(Path(__file__).parents[1] / "browser.js"),
            str(session_dir),
            "snapshot" if snapshot else "crawl",
            target_id or "",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    session = json.loads(result.stdout)
    endpoint = session["cdp_url"]
    if remember:
        save_selection(
            config,
            {
                "id": hashlib.sha256(endpoint.encode()).hexdigest()[:16],
                "target_id": session["target_id"],
            },
        )
    return {
        **env,
        "ARCHIVEBOX_BROWSER_CDP_URL": endpoint,
        "ARCHIVEBOX_BROWSER_TARGET_ID": session["target_id"] or "",
        "ARCHIVEBOX_BROWSER_CONTEXT_ID": session["browser_context_id"] or "",
        "ARCHIVEBOX_PERSONA_NAME": persona.name if persona else persona_name or "",
        "ARCHIVEBOX_BROWSER_PROFILE_SCOPE": "persona" if persona_name else "capture",
        "BU_CDP_WS": endpoint,
        "BU_CDP_URL": endpoint,
        "BU_NAME": "abx-" + hashlib.sha256(endpoint.encode()).hexdigest()[:16],
        "BH_TAB_MARKER": "0",
        # Unix socket paths have a 104-byte limit on macOS. Collection paths
        # routinely exceed that; use the harness's private runtime directory.
        "BH_RUNTIME_DIR": str(Path(tempfile.gettempdir()) / f"abx-bh-{os.getuid()}"),
        "BH_RUNTIME_DIR_SHARED": "1",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--persona")
    source.add_argument("--snapshot")
    source.add_argument("--crawl")
    source.add_argument("--browser")
    source.add_argument(
        "--current",
        action="store_true",
        help="Use the browser/tab selected in the Agent UI",
    )
    source.add_argument(
        "--list",
        action="store_true",
        help="List live browsers, CDP URLs, tabs and crawl/persona identities",
    )
    parser.add_argument("--target", help="Exact tab target ID from --list")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    from archivebox.config.django import setup_django

    setup_django(check_db=True)
    if args.list:
        from archivebox.config.common import get_config

        print(
            json.dumps(
                browser_inventory(
                    get_config().model_dump(mode="json"),
                    snapshot_targets=True,
                ),
            ),
        )
        return
    env = browser_environment(
        persona_name=args.persona,
        snapshot_id=args.snapshot,
        crawl_id=args.crawl,
        browser_id=args.browser,
        target_id=args.target,
        current=args.current,
    )
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if command:
        os.execvpe(command[0], command, env)
    print(
        json.dumps(
            {
                key: value
                for key, value in env.items()
                if key.startswith("ARCHIVEBOX_BROWSER_")
                or key in {"BU_NAME", "ARCHIVEBOX_STAGEHAND_MODULE"}
            },
        ),
    )


if __name__ == "__main__":
    main()

"""Attach tools to a selected capture's existing browser."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

import rich_click as click

from abx_plugins.plugins.base.agents import agent_environment


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


def published_browsers(config, *, sources, snapshot_targets=False):
    """Discover live published sessions, grouped by browser WebSocket identity.

    The Agent panel polls this every second. Session dirs live on archive
    storage that may be remote (e.g. rclone FUSE), and probing their
    descriptor files for every persona and running chrome process made each
    poll do dozens of network round trips. Each browser's observer publishes
    its endpoint, session dir, PID and tabs on local TMP_DIR, so discovery
    reads only those files plus process rows the runner already owns.
    """
    processes = [
        process
        for process in sources["processes"]
        if process["pwd"].endswith("/chrome")
    ]
    persona_names = sources["personas"]
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
    personas = {name.casefold(): name for name in persona_names}
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


def browser_inventory(config, *, sources, snapshot_targets=False):
    """Read owner-published state only: no CDP, installs, or child processes."""
    return [
        {
            key: value
            for key, value in session.items()
            if key not in ("directory", "live_dir")
        }
        for session in published_browsers(
            config,
            snapshot_targets=snapshot_targets,
            sources=sources,
        )
    ]


def selection_path(config):
    from abx_plugins.plugins.opencode import runtime

    return runtime._settings(config)["opencode_dir"] / "browser.json"


def save_selection(config, selection):
    filename = selection_path(config)
    # Re-selecting a tab is idempotent. Replacing the same persistent file on
    # every click adds filesystem writes to the request path under I/O pressure.
    try:
        if json.loads(filename.read_text()) == selection:
            return
    except (OSError, ValueError):
        pass
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
    binding,
    sources,
    target_id: str | None = None,
    remember: bool = True,
) -> dict[str, str]:
    from abx_plugins.plugins.opencode import runtime

    config = binding["config"]
    persona_name = binding["persona_name"]
    scope = binding["scope"]
    browser_id = binding["browser_id"]
    current = binding["current"]
    explicit_persona = binding["explicit_persona"]
    if scope == "crawl" and config.get("CHROME_ISOLATION") == "snapshot":
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
                for item in published_browsers(config, sources=sources)
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
        if explicit_persona:
            session_dir = Path(binding["persona_dir"]) / ".browser"
        else:
            session_dir = Path(binding["output_dir"]) / "chrome"
    # Probe only when the agent attaches, never on a panel request.
    result = subprocess.run(
        [
            env.get("NODE_BINARY") or "node",
            str(Path(__file__).parents[1] / "browser.js"),
            str(session_dir),
            "snapshot" if scope == "snapshot" else "crawl",
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
        **agent_environment("browser", endpoint, config=config, environ=env),
        "ARCHIVEBOX_BROWSER_CDP_URL": endpoint,
        "ARCHIVEBOX_BROWSER_TARGET_ID": session["target_id"] or "",
        "ARCHIVEBOX_BROWSER_CONTEXT_ID": session["browser_context_id"] or "",
        "ARCHIVEBOX_PERSONA_NAME": persona_name or "",
        "ARCHIVEBOX_BROWSER_PROFILE_SCOPE": "persona"
        if explicit_persona or ((browser_id or current) and persona_name)
        else "capture",
    }


def get_cli_command(host):
    @click.command(
        "browser",
        context_settings={
            "ignore_unknown_options": True,
            "allow_interspersed_args": False,
        },
    )
    @click.option("--persona")
    @click.option("--snapshot")
    @click.option("--crawl")
    @click.option("--browser")
    @click.option(
        "--current",
        is_flag=True,
        help="Use the browser/tab selected in the Agent UI",
    )
    @click.option(
        "--list",
        "list_browsers",
        is_flag=True,
        help="List live browsers, tabs and capture identities",
    )
    @click.option("--target", help="Exact tab target ID from --list")
    @click.argument("command", nargs=-1, type=click.UNPROCESSED)
    def browser_command(
        persona,
        snapshot,
        crawl,
        browser,
        current,
        list_browsers,
        target,
        command,
    ):
        """Attach tools to an existing browser."""
        if (
            sum(
                bool(value)
                for value in (persona, snapshot, crawl, browser, current, list_browsers)
            )
            != 1
        ):
            raise click.UsageError(
                "Select exactly one persona, snapshot, crawl, browser, current selection or --list",
            )
        if list_browsers:
            config = host.request_config()
            click.echo(
                json.dumps(
                    browser_inventory(
                        config,
                        sources=host.browser_sources(),
                        snapshot_targets=True,
                    ),
                ),
            )
            return
        try:
            binding = host.browser_binding(
                persona_name=persona,
                snapshot_id=snapshot,
                crawl_id=crawl,
                browser_id=browser,
                current=current,
            )
            env = browser_environment(
                binding=binding,
                sources=host.browser_sources(),
                target_id=target,
            )
        except (ValueError, RuntimeError, OSError) as error:
            raise click.ClickException(str(error)) from error
        if command:
            os.execvpe(command[0], command, env)
        click.echo(
            json.dumps(
                {
                    key: value
                    for key, value in env.items()
                    if key.startswith("ARCHIVEBOX_BROWSER_")
                    or key in {"BU_NAME", "ARCHIVEBOX_STAGEHAND_MODULE"}
                },
            ),
        )

    return browser_command

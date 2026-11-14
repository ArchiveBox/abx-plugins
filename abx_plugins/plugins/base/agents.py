"""Plugin-declared tools and instructions for agents, independent of the host."""

import importlib
import json
import os
from pathlib import Path


def registrations():
    # Match the host catalog's packaged plugins plus ordered directory overrides.
    roots = [Path(__file__).parents[1]]
    roots.extend(
        Path(value).expanduser()
        for value in os.environ.get("ABX_PLUGINS_DIR", "").split(os.pathsep)
        if value
    )
    paths = {}
    for root in roots:
        for path in sorted(root.glob("*/config.json")):
            paths[path.parent.name] = path
    runtime = os.environ.get("ABX_RUNTIME", "abx-dl").lower()
    for path in paths.values():
        config = json.loads(path.read_text())
        if config.get("x-runtimes") and runtime not in config["x-runtimes"]:
            continue
        if config.get("agent_tools") or config.get("agent_skills"):
            yield path, config


def agent_skills(scope, section):
    return "\n\n".join(
        (path.parent / config["agent_skills"][section]).read_text().rstrip("\n")
        for path, config in registrations()
        if config.get("agent_tools", {}).get("scope") == scope
        and section in config.get("agent_skills", {})
    )


def agent_environment(scope, endpoint, *, config=None, environ=None):
    """Resolve registered tools only when an agent explicitly attaches."""
    from abxpkg import BinProvider
    from .utils import load_required_binary_from_config

    original = dict(os.environ if environ is None else environ)
    env = original.copy()
    for path, plugin in registrations():
        registration = plugin.get("agent_tools")
        if not registration or registration.get("scope") != scope:
            continue
        binaries = {
            name: load_required_binary_from_config(
                name,
                path,
                global_config=config,
                environ=env,
                install=True,
            )
            for name in registration["binaries"]
        }
        env = BinProvider.build_exec_env(
            providers=[
                binary.loaded_binprovider
                for binary in binaries.values()
                if binary.loaded_binprovider is not None
            ],
            base_env=env,
        )
        if registration.get("environment"):
            module, function = registration["environment"].split(":")
            env.update(
                getattr(importlib.import_module(module), function)(endpoint, binaries),
            )
    # Python tools use their own executable shebangs; keep the host Python intact.
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        env.pop(key, None)
        if key in original:
            env[key] = original[key]
    return env

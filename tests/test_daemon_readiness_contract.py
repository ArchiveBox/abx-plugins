from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO_ROOT / "abx_plugins" / "plugins"
FINITE_DAEMON_HOOKS = {
    "headers/on_Snapshot__27_headers.daemon.bg.js",
    "redirects/on_Snapshot__25_redirects.daemon.bg.js",
    "staticfile/on_Snapshot__26_staticfile.daemon.bg.js",
}


def test_every_long_lived_daemon_hook_publishes_structured_readiness():
    daemon_hooks = sorted(PLUGINS_DIR.rglob("*.daemon.bg.*"))
    daemon_paths = {
        hook.relative_to(PLUGINS_DIR).as_posix(): hook for hook in daemon_hooks
    }

    assert FINITE_DAEMON_HOOKS <= daemon_paths.keys()

    long_lived_hooks = {
        path: hook
        for path, hook in daemon_paths.items()
        if path not in FINITE_DAEMON_HOOKS
    }
    assert long_lived_hooks

    missing_readiness = [
        path
        for path, hook in long_lived_hooks.items()
        if "emitProcessReadyRecord(" not in hook.read_text()
    ]
    assert missing_readiness == []

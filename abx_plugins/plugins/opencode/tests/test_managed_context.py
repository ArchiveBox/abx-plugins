import json
from functools import partial

from abx_dl.catalog import PluginCatalog
from abx_plugins.plugins.opencode import runtime


def test_managed_context_catalog_refresh_preserves_editable_skill(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    # Real declarative plugin packages, without executable hooks or providers.
    for name in (
        "custom_export",
        "custom-export",
        "ytdlp",
        "liteparse",
        "bad\nname",
        "{bad}",
    ):
        plugin = plugins / name
        plugin.mkdir()
        (plugin / "config.json").write_text(json.dumps({"x-runtimes": ["archivebox"]}))
    other = plugins / "other_runtime"
    other.mkdir()
    (other / "config.json").write_text(json.dumps({"x-runtimes": ["abx-dl"]}))
    settings = runtime._settings({}, tmp_path / "collection")
    settings.update(
        context_command=["archivebox", "plugin", "opencode", "context"],
        archivebox_admin_url="http://localhost/admin",
        plugin_catalog=partial(
            PluginCatalog.discover, plugins_dir=plugins, runtime="archivebox"
        ),
    )
    runtime._ensure_project_files(settings)
    skill = settings["opencode_dir"] / "SKILL.md"
    context = settings["opencode_dir"] / "server_context.md"
    alias = settings["config_home"] / "opencode/skills/archivebox/SKILL.md"
    assert alias.resolve() == skill.resolve()
    assert "avoid ad-hoc script files" in skill.read_text()
    assert "--deps-from=./config.json:required_binaries" in skill.read_text()
    assert "Explain WHY complex code is needed" in skill.read_text()
    assert "Explain WHY complex code is needed" not in context.read_text()
    assert (
        "Available plugins (not necessarily enabled for this crawl): custom-export, custom_export, liteparse, ytdlp\n"
        in context.read_text()
    )
    assert "bad\nname" not in context.read_text()
    assert "{bad}" not in context.read_text()
    assert "other_runtime" not in context.read_text()
    assert str(plugins) not in context.read_text()
    config = json.loads(
        runtime.process_environment(settings, {})["OPENCODE_CONFIG_CONTENT"]
    )
    assert str(context) in config["instructions"]
    assert str(skill) in config["instructions"]

    skill.write_text("Administrator-authored instructions\n")
    context.write_text("stale managed prompt")
    new_plugin = plugins / "new_processor"
    new_plugin.mkdir()
    (new_plugin / "config.json").write_text("{}")
    # Landing on an existing session refreshes the managed prompt, without a
    # provider call, server launch or replacement of the editable instructions.
    runtime.agent_context(settings, session_id="ses_existing")
    assert "new_processor" in context.read_text()
    assert skill.read_text() == "Administrator-authored instructions\n"
    assert alias.read_text() == skill.read_text()
    before = context.stat().st_mtime_ns
    runtime.agent_context(settings, session_id="ses_existing")
    assert context.stat().st_mtime_ns == before


def test_managed_context_without_catalog_or_with_empty_catalog(tmp_path):
    plugins = tmp_path / "empty_plugins"
    plugins.mkdir()
    settings = runtime._settings({}, tmp_path / "collection")
    settings["context_command"] = ["archivebox", "plugin", "opencode", "context"]
    # Older hosts do not provide a catalog callback. Keep their normal context,
    # without implying an empty inventory or enumerating enabled extractors.
    runtime._ensure_project_files(settings)
    path = settings["opencode_dir"] / "server_context.md"
    before = path.read_text()
    assert "archivebox plugin opencode context" in before
    assert "Available plugins" not in before
    assert "{available_plugins}" not in before
    settings["plugin_catalog"] = partial(PluginCatalog.discover, plugins_dir=plugins)
    runtime._ensure_server_context(settings)
    assert path.read_text() == before


def test_concurrent_managed_context_refresh(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    settings = runtime._settings({}, tmp_path / "collection")
    settings["context_command"] = ["archivebox", "plugin", "opencode", "context"]
    runtime._ensure_project_files(settings)
    path = settings["opencode_dir"] / "server_context.md"
    expected = path.read_text()
    path.write_text("stale context")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(runtime._ensure_server_context, [settings] * 20))
    assert path.read_text() == expected
    assert list(path.parent.glob(".server_context.md.*.tmp")) == []

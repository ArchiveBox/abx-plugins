import importlib.util
import json
import sys
import types
from pathlib import Path

from click.testing import CliRunner


PLUGIN_DIR = Path(__file__).resolve().parent.parent
BINARY_HOOK = PLUGIN_DIR / "on_Binary__12_brew_install.py"


def test_brew_hook_respects_brew_only_and_maps_openjdk(monkeypatch, tmp_path):
    fake_abx_pkg = types.SimpleNamespace(
        Binary=object, BrewProvider=object, EnvProvider=object
    )
    monkeypatch.setitem(sys.modules, "rich_click", __import__("click"))
    monkeypatch.setitem(sys.modules, "abx_pkg", fake_abx_pkg)

    spec = importlib.util.spec_from_file_location("brew_install_hook", BINARY_HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    prefix = tmp_path / "brew-prefix"
    java_bin = prefix / "opt" / "openjdk" / "bin" / "java"
    java_bin.parent.mkdir(parents=True, exist_ok=True)
    java_bin.write_text("", encoding="utf-8")
    java_bin.chmod(0o755)

    captured = {}

    class FakeEnvProvider:
        name = "env"

    class FakeBrewProvider:
        name = "brew"
        INSTALLER_BIN = "brew"
        PATH = str(prefix / "bin")

    class FakeBinaryResult:
        abspath = java_bin
        version = "21.0.10"
        sha256 = "deadbeef"
        binprovider = "brew"

    class FakeBinary:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def load_or_install(self):
            return FakeBinaryResult()

    monkeypatch.setattr(module, "EnvProvider", FakeEnvProvider)
    monkeypatch.setattr(module, "BrewProvider", FakeBrewProvider)
    monkeypatch.setattr(module, "Binary", FakeBinary)

    runner = CliRunner()
    result = runner.invoke(
        module.main,
        [
            "--machine-id=test-machine",
            "--binary-id=test-binary",
            "--plugin-name=test-suite",
            "--hook-name=test_brew_install",
            "--name=java",
            "--binproviders=brew",
            '--overrides={"brew":{"install_args":["openjdk"]}}',
        ],
    )

    assert result.exit_code == 0, result.output
    assert [provider.name for provider in captured["binproviders"]] == ["brew"]
    assert captured["overrides"]["brew"]["abspath"] == str(java_bin)

    records = [
        json.loads(line) for line in result.output.splitlines() if line.startswith("{")
    ]
    assert records[0]["abspath"] == str(java_bin)

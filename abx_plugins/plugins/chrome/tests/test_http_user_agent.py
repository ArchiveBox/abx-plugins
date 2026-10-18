"""HTTP hook identities follow actual saved Chrome metadata without live CDP."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from abx_plugins.plugins.base.utils import (
    load_config,
    resolve_user_agent,
    read_snapshot_source_text,
)
from abx_plugins.plugins.base.testing import parse_jsonl_output
from abx_plugins.plugins.favicon.tests.test_favicon import TEST_ICON_BYTES
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

PLUGINS = Path(__file__).resolve().parents[2]


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_shared_config_reuses_actual_browser_user_agent(tmp_path, httpserver):
    httpserver.expect_request("/").respond_with_data(
        "<!doctype html><h1>Shared browser identity</h1>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(b"", status=204)
    with chrome_session(tmp_path, test_url=httpserver.url_for("/")) as (
        _process,
        _pid,
        chrome,
        chrome_env,
    ):
        metadata = (chrome / "browser.json").read_bytes()
        expected = json.loads(metadata)["userAgentOverride"]["userAgent"]
        assert "Chrome/" in expected and "HeadlessChrome/" not in expected
        env: dict[str, str] = {**chrome_env, "SNAP_DIR": str(chrome.parent)}
    # Resolve in ordinary hook code after the actual browser has closed.
    env.pop("USER_AGENT", None)
    env.pop("WGET_USER_AGENT", None)
    version_match = re.search(r"Chrome/([\d.]+)", expected)
    assert version_match
    current_version = version_match[1]
    cases = (
        ({}, expected, expected),
        (
            {
                "USER_AGENT": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 ArchiveBox/0.9.0rc260 (+https://github.com/ArchiveBox/ArchiveBox/)",
            },
            expected,
            expected,
        ),
        (
            {"USER_AGENT": "Mozilla/5.0 Chrome/131.0.0.0 Custom/1.0"},
            f"Mozilla/5.0 Chrome/{current_version} Custom/1.0",
            f"Mozilla/5.0 Chrome/{current_version} Custom/1.0",
        ),
        (
            {"USER_AGENT": "ExplicitGlobal/1.0"},
            "ExplicitGlobal/1.0",
            "ExplicitGlobal/1.0",
        ),
        ({"WGET_USER_AGENT": "ExplicitWget/1.0"}, expected, "ExplicitWget/1.0"),
        (
            {"USER_AGENT": "Mozilla/5.0 (compatible; ArchiveBox/1.0)"},
            expected,
            expected,
        ),
        (
            {
                "USER_AGENT": "Mozilla/5.0 (compatible; abx-dl/1.0; +https://github.com/ArchiveBox/abx-dl)",
            },
            expected,
            expected,
        ),
    )
    for overrides, global_ua, wget_ua in cases:
        assert_loaders({**env, **overrides}, chrome.parent, [global_ua, wget_ua])
    assert (chrome / "browser.json").read_bytes() == metadata

    # Actual hooks send the same identity even after Chrome has exited.
    httpserver.expect_request("/icon-page").respond_with_data(
        '<link rel="icon" href="/shared-icon.png">',
        content_type="text/html",
    )
    httpserver.expect_request("/shared-icon.png").respond_with_data(
        TEST_ICON_BYTES,
        content_type="image/png",
    )
    httpserver.expect_request(re.compile(r"^/missing-save/")).respond_with_data(
        "Not found",
        status=404,
    )
    hooks: tuple[tuple[str, str, dict[str, str], str], ...] = (
        ("favicon", httpserver.url_for("/icon-page"), {}, "succeeded"),
        (
            "archivedotorg",
            "https://archivebox.io/tests/",
            {
                "ARCHIVEDOTORG_SAVE_URL": httpserver.url_for("/missing-save/"),
                "ARCHIVEDOTORG_ENABLED": "true",
            },
            "failed",
        ),
    )
    for plugin, target, extra, status in hooks:
        hook = next((PLUGINS / plugin).glob("on_Snapshot__*.py"))
        before = len(httpserver.log)
        run_env: dict[str, str] = {**env, **extra}
        result = subprocess.run(
            [str(hook), "--url", target],
            cwd=chrome.parent,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == (0 if status == "succeeded" else 1), result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record and record["status"] == status, result.stdout
        requests = [request for request, _response in httpserver.log[before:]]
        assert requests
        assert all(request.headers["User-Agent"] == expected for request in requests)
    assert (chrome.parent / "favicon/favicon.ico").read_bytes() == TEST_ICON_BYTES
    assert not (chrome.parent / "archivedotorg/archive.org.txt").exists()


def assert_loaders(env, cwd, expected):
    config_path = PLUGINS / "wget" / "config.json"
    config = load_config(config_path, environ=env, hydrate_binaries=False)
    assert [config.USER_AGENT, config.WGET_USER_AGENT] == expected
    script = f"const u=require({json.dumps(str(PLUGINS / 'base/utils.js'))});for(let i=0;i<2;i++){{const c=u.loadConfig({json.dumps(str(config_path))});console.log(JSON.stringify([c.USER_AGENT,c.WGET_USER_AGENT]));}}"
    result = subprocess.run(
        [env["NODE_BINARY"], "-e", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert [json.loads(line) for line in result.stdout.splitlines()] == [
        expected,
        expected,
    ]


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_native_cache_never_persists_persona_override(tmp_path, httpserver):
    httpserver.expect_request("/").respond_with_data(
        "<h1>Persona identity</h1>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(b"", status=204)
    lib = tmp_path / "lib"
    lib.mkdir()
    with chrome_session(
        tmp_path,
        test_url=httpserver.url_for("/"),
        env_overrides={
            "ABXPKG_LIB_DIR": str(lib),
            "CHROME_USER_AGENT": "ExplicitPersona/1.0",
        },
    ) as (_process, _pid, chrome, env):
        metadata = json.loads((chrome / "browser.json").read_text())
        assert metadata["userAgentOverride"]["userAgent"] == "ExplicitPersona/1.0"
        cache = lib / "chrome-user-agent.json"
        native = json.loads(cache.read_text())["userAgent"]
        assert "Chrome/" in native and "HeadlessChrome/" not in native
        assert "ExplicitPersona" not in native
        # The running crawl already publishes identity before its next tab.
        assert_loaders(
            {
                **env,
                "SNAP_DIR": str(tmp_path / "next-snapshot"),
                "USER_AGENT": "",
                "WGET_USER_AGENT": "",
            },
            tmp_path,
            ["ExplicitPersona/1.0"] * 2,
        )
    env = {**env, "USER_AGENT": "", "WGET_USER_AGENT": ""}
    assert_loaders(env, tmp_path, ["ExplicitPersona/1.0"] * 2)
    # Normal shutdown clears the crawl metadata, but preserves native cache.
    env["SNAP_DIR"] = str(tmp_path / "next-snapshot")
    env["CRAWL_DIR"] = str(tmp_path / "next-crawl")
    assert_loaders(env, tmp_path, [native] * 2)
    assert resolve_user_agent({"ABXPKG_LIB_DIR": str(lib)}) == native
    # Reusing the crawl directory must not seed the next browser from the old
    # persona metadata. Frozen historical generated defaults are automatic too.
    legacy = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 ArchiveBox/0.9.0rc260 (+https://github.com/ArchiveBox/ArchiveBox/)"
    with chrome_session(
        tmp_path,
        test_url=httpserver.url_for("/"),
        env_overrides={
            "ABXPKG_LIB_DIR": str(lib),
            "USER_AGENT": legacy,
            "CHROME_USER_AGENT": "",
        },
    ) as (_process, _pid, next_chrome, _env):
        next_identity = json.loads((next_chrome / "browser.json").read_text())[
            "userAgentOverride"
        ]["userAgent"]
        assert next_identity == native
        assert json.loads(cache.read_text())["userAgent"] == native
    for bad in (
        "not JSON",
        "[]",
        '{"userAgent": []}',
        '{"userAgent": "bad\\r\\nheader"}',
    ):
        cache.write_text(bad)
        assert_loaders(env, tmp_path, [""] * 2)
    cache.unlink()
    assert_loaders(env, tmp_path, [""] * 2)
    assert (
        resolve_user_agent(
            {"USER_AGENT": "ExplicitGlobal/1.0", "ABXPKG_LIB_DIR": str(lib)},
        )
        == "ExplicitGlobal/1.0"
    )


def test_browserless_fetch_omits_empty_user_agent(tmp_path, httpserver):
    httpserver.expect_request("/").respond_with_data(
        "browserless source",
        content_type="text/html",
    )
    assert (
        read_snapshot_source_text(
            httpserver.url_for("/"),
            snap_dir=tmp_path,
            timeout=5,
            user_agent="",
        )
        == "browserless source"
    )
    assert len(httpserver.log) == 1
    assert httpserver.log[0][0].headers["User-Agent"].startswith("Python-urllib/")


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_frozen_persona_chrome_version_matches_browser_and_http(tmp_path, httpserver):
    frozen = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
    httpserver.expect_request("/").respond_with_data(
        "<h1>Frozen identity</h1>",
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data(b"", status=204)
    with chrome_session(
        tmp_path,
        test_url=httpserver.url_for("/"),
        env_overrides={
            "USER_AGENT": frozen,
            "CHROME_USER_AGENT": frozen,
            "WGET_USER_AGENT": frozen,
        },
    ) as (_process, _pid, chrome, env):
        actual = json.loads((chrome / "browser.json").read_text())["userAgentOverride"][
            "userAgent"
        ]
        assert re.sub(r"Chrome/[\d.]+", "Chrome/VERSION", actual) == re.sub(
            r"Chrome/[\d.]+",
            "Chrome/VERSION",
            frozen,
        )
        assert httpserver.log[0][0].headers["User-Agent"] == actual
        assert_loaders(env, tmp_path, [actual, actual])
        assert_loaders(
            {**env, "WGET_USER_AGENT": "ExplicitWget/1.0"},
            tmp_path,
            [actual, "ExplicitWget/1.0"],
        )
    # Saved browser identity remains usable after Chrome exits.
    httpserver.expect_request(
        "/source",
        headers={"User-Agent": actual},
    ).respond_with_data("frozen persona source", content_type="text/html")
    config = load_config(
        PLUGINS / "base/config.json",
        environ=env,
        hydrate_binaries=False,
    )
    assert (
        read_snapshot_source_text(
            httpserver.url_for("/source"),
            snap_dir=tmp_path / "source",
            timeout=5,
            user_agent=config.USER_AGENT,
        )
        == "frozen persona source"
    )
    assert resolve_user_agent({"USER_AGENT": frozen}) == frozen

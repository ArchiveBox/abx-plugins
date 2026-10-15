"""Use the actual agent clients against a Chrome hook's live capture tab."""

import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import struct
import signal
import sys
import tempfile
import time

import pytest
from abxpkg import BinProvider

from abx_plugins.plugins.base.testing import install_required_binary_from_config
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session
from abx_plugins.plugins.importers_browser.importer import task_tab
from abx_plugins.plugins.importers_browser.importer import capture_failure
from abx_plugins.plugins.importers_browser.importer import replay
from abx_plugins.plugins.importers_browser.importer import learn

PLUGIN = Path(__file__).parents[1]


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_harness_and_stagehand_share_live_capture(tmp_path, httpserver):
    httpserver.expect_request("/").respond_with_data(
        '<title>Live persona</title><a href="/one">One</a><a href="/two">Two</a><a href="/three">Three</a>',
        content_type="text/html",
    )
    httpserver.expect_request("/favicon.ico").respond_with_data("")
    httpserver.expect_request("/after-failure").respond_with_data(
        '<title>Later failure</title><body style="background:#b00020;color:white">Later failure screen</body>',
        content_type="text/html",
    )
    with chrome_session(
        tmp_path,
        test_url=httpserver.url_for("/"),
        env_overrides={
            "BROWSER_LANGUAGE": "fr-FR",
            "BROWSER_TIMEZONE": "Europe/Paris",
        },
    ) as (_, _, chrome_dir, env):
        harness = install_required_binary_from_config(
            PLUGIN,
            "browser-harness",
            env=env,
        )
        stagehand = install_required_binary_from_config(PLUGIN, "stagehand", env=env)
        env = BinProvider.build_exec_env(
            providers=[harness.loaded_binprovider, stagehand.loaded_binprovider],
            base_env=env,
        )
        probe = subprocess.run(
            [
                env["NODE_BINARY"],
                str(PLUGIN / "browser.js"),
                str(chrome_dir),
                "snapshot",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert probe.returncode == 0, probe.stderr
        session = json.loads(probe.stdout)
        assert (
            session["target_id"] == (chrome_dir / "target_id.txt").read_text().strip()
        )
        env.update(
            {
                "BU_CDP_WS": session["cdp_url"],
                "BU_CDP_URL": session["cdp_url"],
                "BU_NAME": "abx-test-"
                + hashlib.sha256(session["cdp_url"].encode()).hexdigest()[:16],
                "BH_TAB_MARKER": "0",
                "BH_RUNTIME_DIR": tempfile.mkdtemp(prefix="abx-bh-", dir="/tmp"),
                "BH_RUNTIME_DIR_SHARED": "1",
                "ARCHIVEBOX_BROWSER_TARGET_ID": session["target_id"],
                "ARCHIVEBOX_STAGEHAND_MODULE": str(stagehand.loaded_abspath),
            },
        )

        def run_harness(code):
            result = subprocess.run(
                [str(harness.loaded_abspath)],
                input=code,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert result.returncode == 0, result.stderr + result.stdout
            return result.stdout

        try:
            output = run_harness("""import os, json
switch_tab(os.environ['ARCHIVEBOX_BROWSER_TARGET_ID'])
print(json.dumps(js("window.agentHeap = 'still-live'; sessionStorage.setItem('agent','session'); localStorage.setItem('agent','local'); document.cookie = 'agent=session-cookie; SameSite=Lax'; ({language:navigator.language, timezone:Intl.DateTimeFormat().resolvedOptions().timeZone})")))
""")
            assert "fr-FR" in output and "Europe/Paris" in output
            harness_screenshot = tmp_path / "harness.png"
            # Native attachment preserves foreground selection. Pixel capture
            # must activate the headless compositor, as the browser skill does.
            assert str(harness_screenshot) in run_harness(
                f"activate_tab(current_tab()); print(capture_screenshot(path={str(harness_screenshot)!r}))",
            )
            capture_failure(env, tmp_path)
            failure_screenshot = tmp_path / "failure-0.png"
            assert failure_screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            assert failure_screenshot.stat().st_mode & 0o777 == 0o600
            assert "still-live" in run_harness("print(js('window.agentHeap'))")
            # Cleanup after a later failure must retain the earlier viewport.
            evidence_dir = tmp_path / "failure-retention"
            evidence_dir.mkdir()
            evidence_env = {**env, "BU_NAME": env["BU_NAME"] + "-evidence"}
            screens = {}
            with pytest.raises(RuntimeError, match="Later discovery failure"):
                with task_tab(evidence_env, evidence_dir):
                    for attempt, url in enumerate(
                        (httpserver.url_for("/"), httpserver.url_for("/after-failure")),
                    ):
                        navigation = subprocess.run(
                            [str(harness.loaded_abspath)],
                            input=f"goto_url({url!r}); wait_for_load(); print(js('document.title'))\n",
                            env=evidence_env,
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        assert navigation.returncode == 0, navigation.stderr
                        assert ("Live persona", "Later failure")[
                            attempt
                        ] in navigation.stdout
                        capture_failure(evidence_env, evidence_dir, attempt)
                        screens[attempt] = (
                            evidence_dir / f"failure-{attempt}.png"
                        ).read_bytes()
                    assert screens[0] != screens[1]
                    raise RuntimeError("Later discovery failure")
            assert (evidence_dir / "failure-0.png").read_bytes() == screens[0]
            assert (evidence_dir / "failure-1.png").read_bytes() == screens[1]
            assert "still-live" in run_harness("print(js('window.agentHeap'))")
            task = tmp_path / "stagehand.mts"
            task.write_text("""const {StagehandClient} = await import(process.env.ARCHIVEBOX_STAGEHAND_MODULE);
const {writeFile} = await import('node:fs/promises');
const client = new StagehandClient({cdp_url: process.env.BU_CDP_WS, keep_alive:true, logging:{sentry_enabled:false}});
await client.connect();
try {
  const pages = await client.browser.pages({targetId:process.env.ARCHIVEBOX_BROWSER_TARGET_ID});
  if (pages.length !== 1) throw new Error('Expected the existing capture tab');
  const state = await pages[0].evaluate({expression: "({heap:window.agentHeap, session:sessionStorage.getItem('agent'), local:localStorage.getItem('agent'), cookie:document.cookie, language:navigator.language, timezone:Intl.DateTimeFormat().resolvedOptions().timeZone})"});
  console.log('STATE=' + JSON.stringify(state));
  await pages[0].bringToFront();
  const shot = await pages[0].screenshot({options:{type:'png', fullPage:false}});
  if (!shot.screenshot) throw new Error('Screenshot missing');
  await writeFile(process.env.STAGEHAND_SCREENSHOT, Buffer.from(shot.screenshot.replace(/^data:image\\/png;base64,/, ''), 'base64'));
} finally { await client.close(); }
""")
            stagehand_screenshot = tmp_path / "stagehand.png"
            result = subprocess.run(
                ["tsx", str(task)],
                env={**env, "STAGEHAND_SCREENSHOT": str(stagehand_screenshot)},
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert result.returncode == 0, result.stderr + result.stdout
            state = json.loads(
                next(
                    line.removeprefix("STATE=")
                    for line in result.stdout.splitlines()
                    if line.startswith("STATE=")
                ),
            )
            assert state == {
                "heap": "still-live",
                "session": "session",
                "local": "local",
                "cookie": "agent=session-cookie",
                "language": "fr-FR",
                "timezone": "Europe/Paris",
            }
            # Both native clients return real PNGs of this same capture viewport.
            dimensions = []
            for screenshot in (harness_screenshot, stagehand_screenshot):
                data = screenshot.read_bytes()
                assert data.startswith(b"\x89PNG\r\n\x1a\n")
                assert data[-8:-4] == b"IEND"
                assert len(data) > 500
                dimensions.append(struct.unpack(">II", data[16:24]))
            assert dimensions[0] == dimensions[1]
            assert min(dimensions[0]) > 100
            assert "still-live" in run_harness("print(js('window.agentHeap'))")
            # Importers must expose a rendered tab to the learner. In headless
            # Chrome, background tabs stall real mouse input and DOM evaluation.
            importer_env = {**env, "BU_NAME": env["BU_NAME"] + "-importer"}
            original_tabs = json.loads(run_harness("print(json.dumps(list_tabs()))"))
            with task_tab(importer_env, tmp_path):
                assert (
                    len(json.loads(run_harness("print(json.dumps(list_tabs()))")))
                    == len(original_tabs) + 1
                )
                visibility = subprocess.run(
                    [str(harness.loaded_abspath)],
                    input="print(js('document.visibilityState'))",
                    env=importer_env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=True,
                )
                assert visibility.stdout.strip() == "visible"
                # A real browser reader exercises the replay/probe boundary:
                # discover actual links, retain a bounded cursor, then read the
                # next batch. No agent, replacement binary or canned JSONL.
                workspace = tmp_path / "feed-reader"
                workspace.mkdir()
                (workspace / "importer.py").write_text("""import os, json
from pathlib import Path
from abx_plugins.plugins.base.importers import emit_collection
request = json.loads(Path(os.environ['IMPORTERS_REQUEST_FILE']).read_text())
switch_tab(os.environ['IMPORTERS_TAB_ID'])
goto_url(request['settings']['url'])
wait_for_load()
source = js('({id:location.origin,label:document.title})')
links = js("Array.from(document.links, a => ({id:a.href,url:a.href,title:a.textContent,metadata:{}}))")
emit_collection(request, links, message=source['label'], account=source)
""")
                request = {
                    "version": 1,
                    "action": "import",
                    "limit": 2,
                    "settings": {"url": httpserver.url_for("/")},
                    "checkpoint": {},
                }
                request_file = tmp_path / "request.json"
                request_file.write_text(json.dumps(request))
                replay_env = {
                    **importer_env,
                    "IMPORTERS_REQUEST_FILE": str(request_file),
                }
                first, error = replay(
                    workspace,
                    request,
                    replay_env,
                    tmp_path,
                    0,
                    time.monotonic() + 120,
                )
                assert first is not None, error
                assert len(first) == 3 and first[-1]["has_more"] is True
                probe_file = tmp_path / "continuation-request.json"
                probe = json.loads(probe_file.read_text())
                assert probe_file.stat().st_mode & 0o777 == 0o600
                assert probe["checkpoint"] == first[-1]["checkpoint"]
                assert probe["delivered_ids"] == [item["id"] for item in first[:-1]]
                assert (
                    "Continuation validation" in (tmp_path / "replay-0.log").read_text()
                )
                assert json.loads(request_file.read_text()) == request
                stale = {**request, "delivered_ids": probe["delivered_ids"]}
                request_file.write_text(json.dumps(stale))
                rejected, error = replay(
                    workspace,
                    stale,
                    replay_env,
                    tmp_path,
                    0,
                    time.monotonic() + 120,
                )
                assert rejected is None
                assert "already delivered" in error
                assert "already delivered" in (tmp_path / "replay-0.log").read_text()
                request = {**request, "checkpoint": first[-1]["checkpoint"]}
                request_file.write_text(json.dumps(request))
                second, error = replay(
                    workspace,
                    request,
                    replay_env,
                    tmp_path,
                    1,
                    time.monotonic() + 120,
                )
                assert second is not None, error
                assert len(second) == 2 and second[-1]["has_more"] is False
                assert second[0]["id"] not in probe["delivered_ids"]
                request = {**request, "checkpoint": second[-1]["checkpoint"]}
                request_file.write_text(json.dumps(request))
                repeated, error = replay(
                    workspace,
                    request,
                    replay_env,
                    tmp_path,
                    2,
                    time.monotonic() + 120,
                )
                assert repeated is not None, error
                assert len(repeated) == 1 and repeated[0]["has_more"] is False
                assert not list(tmp_path.glob("learning-*.jsonl"))
                request_file.unlink()
                rejected, error = replay(
                    workspace,
                    request,
                    replay_env,
                    tmp_path,
                    0,
                    time.monotonic() + 120,
                )
                assert rejected is None and error
                assert "Replay rejected:" in (tmp_path / "replay-0.log").read_text()
            assert importer_env["IMPORTERS_TAB_ID"] not in run_harness(
                "print(json.dumps(list_tabs()))",
            )
            assert "still-live" in run_harness("print(js('window.agentHeap'))")
            # Exercise the complete standalone entry point and OpenCode's real
            # tool resolution. Host-wide skills must not leak into a task whose
            # cwd is an adapter workspace rather than an ArchiveBox collection.
            opencode = install_required_binary_from_config(PLUGIN, "opencode", env=env)
            standalone_dir = tmp_path / "standalone-importer"
            standalone_dir.mkdir()
            state_dir = standalone_dir / "state"
            standalone_workspace = state_dir / "current"
            standalone_workspace.mkdir(parents=True)
            reader = (workspace / "importer.py").read_text()
            reader = reader.replace(
                "emit_collection(request, links,",
                """import subprocess
agent = subprocess.run([os.environ['OPENCODE_BINARY'], 'debug', 'agent', 'build'], capture_output=True, text=True, check=True, timeout=30)
skills_enabled = json.loads(agent.stdout)['tools'].get('skill', False)
for link in links:
    link['metadata']['host_skills_enabled'] = skills_enabled
emit_collection(request, links,""",
            )
            (standalone_workspace / "importer.py").write_text(reader)
            standalone = subprocess.run(
                [sys.executable, str(Path(replay.__code__.co_filename))],
                input=json.dumps(
                    {
                        **request,
                        "action": "preview",
                        "checkpoint": {},
                        "feed": "x_bookmarks",
                    },
                ),
                cwd=standalone_dir,
                env={
                    **env,
                    "IMPORTERS_STATE_DIR": str(state_dir),
                    "OPENCODE_BINARY": str(opencode.loaded_abspath),
                    "OPENCODE_CONFIG_CONTENT": json.dumps(
                        {
                            "model": "openai/gpt-6-luna",
                            "small_model": "openai/gpt-6-luna",
                        },
                    ),
                },
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert standalone.returncode == 0, standalone.stdout + standalone.stderr
            standalone_records = [
                json.loads(line) for line in standalone.stdout.splitlines()
            ]
            assert len(standalone_records) == 3
            assert standalone_records[-1]["status"] == "succeeded"
            assert all(
                item["metadata"]["host_skills_enabled"] is False
                for item in standalone_records[:-1]
            )
            assert not list(standalone_dir.glob("learning-*.jsonl"))
            # A real OpenCode client exceeding its learning budget must abort
            # discovery, even if an adapter file already exists to replay.
            # Load the process owner before measuring the client wait budget.
            importlib.import_module("abx_plugins.plugins.opencode.runtime")
            with pytest.raises(subprocess.TimeoutExpired):
                learn(
                    str(opencode.loaded_abspath),
                    "Inspect the supplied browser session.",
                    {
                        **env,
                        "IMPORTERS_REQUEST_FILE": str(standalone_dir / "request.json"),
                        "OPENCODE_CONFIG_CONTENT": json.dumps(
                            {
                                "model": "openai/gpt-6-luna",
                                "small_model": "openai/gpt-6-luna",
                                "permission": {"skill": "deny"},
                            },
                        ),
                    },
                    standalone_workspace,
                    tmp_path,
                    "",
                    99,
                    time.monotonic() + 120.1,
                )
            timeout_screen = tmp_path / "failure-99.png"
            assert timeout_screen.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            assert timeout_screen.stat().st_mode & 0o777 == 0o600
            assert "still-live" in run_harness("print(js('window.agentHeap'))")
            # The official daemon shutdown command must also preserve this external browser.
            interrupted_dir = tmp_path / "interrupted-importer"
            interrupted_dir.mkdir()
            interrupted = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-c",
                    """import json, os, signal, sys
from pathlib import Path
from abx_plugins.plugins.importers_browser.importer import task_tab
env = os.environ.copy()
with task_tab(env, Path(sys.argv[1])):
    print(json.dumps(env['IMPORTERS_TAB_ID']), flush=True)
    signal.pause()
""",
                    str(interrupted_dir),
                ],
                env={**env, "BU_NAME": env["BU_NAME"] + "-interrupted"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                assert interrupted.stdout is not None
                interrupted_tab = json.loads(interrupted.stdout.readline())
                interrupted.send_signal(signal.SIGINT)
                _, interrupted_error = interrupted.communicate(timeout=30)
                assert interrupted.returncode != 0, interrupted_error
                interrupted_screen = interrupted_dir / "failure-0.png"
                assert interrupted_screen.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
                assert interrupted_screen.stat().st_mode & 0o777 == 0o600
                assert interrupted_tab not in run_harness(
                    "print(json.dumps(list_tabs()))",
                )
                assert "still-live" in run_harness("print(js('window.agentHeap'))")
            finally:
                if interrupted.poll() is None:
                    interrupted.kill()
                    interrupted.communicate(timeout=10)
            # The official daemon shutdown command must preserve the external browser.
            result = subprocess.run(
                [str(harness.loaded_abspath), "--reload"],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert result.returncode == 0, result.stderr
            assert "still-live" in run_harness(
                "import os; switch_tab(os.environ['ARCHIVEBOX_BROWSER_TARGET_ID']); print(js('window.agentHeap'))",
            )
        finally:
            subprocess.run(
                [str(harness.loaded_abspath), "--reload"],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )

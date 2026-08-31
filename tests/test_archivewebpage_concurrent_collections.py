import json
import shutil
import signal
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TypedDict

import pytest

from abx_plugins.plugins.base.testing import install_required_binary_from_config
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_UTILS,
    kill_chromium_session,
    launch_chromium_session,
    launch_snapshot_tab,
    setup_test_env,
    wait_for_chrome_session_state,
)


pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")

ARCHIVEWEBPAGE_PLUGIN_DIR = CHROME_UTILS.parent.parent / "archivewebpage"
ARCHIVEWEBPAGE_START_HOOK = (
    ARCHIVEWEBPAGE_PLUGIN_DIR / "on_Snapshot__16_archivewebpage_start.js"
)
AWP_INTERNAL = ARCHIVEWEBPAGE_PLUGIN_DIR / "awp_internal.js"


class AwpStatus(TypedDict):
    type: str
    recording: bool
    collId: str
    collectionTitle: str


class CollectionSelection(TypedDict):
    selectedId: str
    matchingId: str


def _run_start_hook(
    snapshot_dir: Path,
    env: dict[str, str],
    url: str,
) -> subprocess.CompletedProcess[str]:
    output_dir = snapshot_dir / "archivewebpage"
    output_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            str(ARCHIVEWEBPAGE_START_HOOK),
            f"--url={url}",
            # Keep the model identity deliberately different from the output
            # directory name. parseArgs normalizes --snapshot-id to
            # args.snapshot_id; if the hook accidentally reads snapshotId, its
            # directory fallback masks the bug unless these values differ.
            f"--snapshot-id=model-{snapshot_dir.name}",
            "--crawl-id=test-archivewebpage-concurrent-collections",
        ],
        cwd=output_dir,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _open_child_target(snapshot_chrome_dir: Path, env: dict[str, str]) -> str:
    script = r"""
const chromeUtils = require(process.argv[1]);
const chromeSessionDir = process.argv[2];

(async () => {
  const puppeteer = chromeUtils.resolvePuppeteerModule();
  const { browser, page } = await chromeUtils.connectToPage({
    chromeSessionDir,
    timeoutMs: 10000,
    requireTargetId: true,
    puppeteer,
  });
  try {
    const existingTargetIds = new Set(
      browser.targets().map((target) => chromeUtils.getTargetIdFromTarget(target))
    );
    const childTargetPromise = browser.waitForTarget(
      (target) =>
        target.type() === "page" &&
        !existingTargetIds.has(chromeUtils.getTargetIdFromTarget(target)),
      { timeout: 10000 }
    );
    const pageSession = await page.target().createCDPSession();
    try {
      await pageSession.send("Runtime.evaluate", {
        expression: "window.open('about:blank', '_blank')",
        userGesture: true,
      });
    } finally {
      await pageSession.detach();
    }
    const childTarget = await childTargetPromise;
    const targetId = chromeUtils.getTargetIdFromTarget(childTarget);
    if (!targetId) throw new Error("Child tab has no CDP target id");
    process.stdout.write(targetId);
  } finally {
    await browser.disconnect();
  }
})().catch((error) => {
  console.error(error && (error.stack || error.message || String(error)));
  process.exit(1);
});
"""
    result = subprocess.run(
        [
            env["NODE_BINARY"],
            "-e",
            script,
            str(CHROME_UTILS),
            str(snapshot_chrome_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip(), result
    return result.stdout.strip()


def _read_awp_status(snapshot_chrome_dir: Path, env: dict[str, str]) -> AwpStatus:
    script = r"""
const chromeUtils = require(process.argv[1]);
const awpInternal = require(process.argv[2]);
const chromeSessionDir = process.argv[3];

(async () => {
  const puppeteer = chromeUtils.resolvePuppeteerModule();
  const { browser, page } = await chromeUtils.connectToPage({
    chromeSessionDir,
    timeoutMs: 10000,
    requireTargetId: true,
    puppeteer,
  });
  try {
    const { id: extensionId } = awpInternal.resolveAwpExtension(chromeSessionDir);
    if (!extensionId) throw new Error("ArchiveWeb.page extension is not loaded");
    const tabId = await awpInternal.getChromeTabIdForPage(
      browser,
      page,
      extensionId,
      10000
    );
    const helperPage = await awpInternal.openAwpHelperTab(browser, extensionId, 10000);
    try {
      const status = await helperPage.evaluate(async (targetTabId) => {
        const port = document.querySelector("wr-popup-viewer")?.port;
        if (!port) throw new Error("AWP popup port is not ready");
        return await new Promise((resolve, reject) => {
          let recordingStatus = null;
          let collections = [];
          const timeout = setTimeout(() => {
            port.onMessage.removeListener(onMessage);
            reject(new Error("Timed out waiting for AWP recording status"));
          }, 10000);
          const onMessage = (message) => {
            if (message?.type === "collections" && Array.isArray(message.collections)) {
              collections = message.collections;
            } else if (message?.type === "status" && message.recording === true) {
              recordingStatus = message;
            }
            const collection = recordingStatus
              ? collections.find((item) => item.id === recordingStatus.collId)
              : null;
            if (!recordingStatus || !collection) return;
            clearTimeout(timeout);
            port.onMessage.removeListener(onMessage);
            resolve({ ...recordingStatus, collectionTitle: collection.title });
          };
          port.onMessage.addListener(onMessage);
          port.postMessage({ type: "startUpdates", tabId: targetTabId });
        });
      }, tabId);
      process.stdout.write(JSON.stringify(status));
    } finally {
      await helperPage.close({ runBeforeUnload: false }).catch(() => {});
    }
  } finally {
    await browser.disconnect();
  }
})().catch((error) => {
  console.error(error && (error.stack || error.message || String(error)));
  process.exit(1);
});
"""
    result = subprocess.run(
        [
            env["NODE_BINARY"],
            "-e",
            script,
            str(CHROME_UTILS),
            str(AWP_INTERNAL),
            str(snapshot_chrome_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["type"] == "status"
    assert isinstance(status["recording"], bool)
    assert isinstance(status["collId"], str)
    assert isinstance(status["collectionTitle"], str)
    return AwpStatus(
        type=status["type"],
        recording=status["recording"],
        collId=status["collId"],
        collectionTitle=status["collectionTitle"],
    )


def _select_new_collection_after_queued_updates(
    snapshot_chrome_dir: Path,
    env: dict[str, str],
    collection_title: str,
) -> tuple[str, str]:
    script = r"""
const chromeUtils = require(process.argv[1]);
const awpInternal = require(process.argv[2]);
const chromeSessionDir = process.argv[3];
const collectionTitle = process.argv[4];

(async () => {
  const puppeteer = chromeUtils.resolvePuppeteerModule();
  const { browser, page } = await chromeUtils.connectToPage({
    chromeSessionDir,
    timeoutMs: 10000,
    requireTargetId: true,
    puppeteer,
  });
  try {
    const { id: extensionId } = awpInternal.resolveAwpExtension(chromeSessionDir);
    if (!extensionId) throw new Error("ArchiveWeb.page extension is not loaded");
    const tabId = await awpInternal.getChromeTabIdForPage(
      browser,
      page,
      extensionId,
      10000
    );
    const helperPage = await awpInternal.openAwpHelperTab(browser, extensionId, 10000);
    try {
      const messages = await helperPage.evaluate(
        async ({ targetTabId, collectionTitle }) => {
          const port = document.querySelector("wr-popup-viewer")?.port;
          if (!port) throw new Error("AWP popup port is not ready");
          const queue = [];
          const waiters = [];
          let collectionsSeen = 0;
          let markSecondCollectionsQueued;
          const secondCollectionsQueued = new Promise((resolve) => {
            markSecondCollectionsQueued = resolve;
          });
          port.onMessage.addListener((message) => {
            if (message?.type === "collections") {
              collectionsSeen += 1;
              if (collectionsSeen === 2) markSecondCollectionsQueued();
            }
            const waiterIndex = waiters.findIndex(({ predicate }) =>
              predicate(message)
            );
            if (waiterIndex === -1) {
              queue.push(message);
              return;
            }
            const [waiter] = waiters.splice(waiterIndex, 1);
            clearTimeout(waiter.timeout);
            waiter.resolve(message);
          });
          function waitFor(predicate) {
            return new Promise((resolve, reject) => {
              const queuedIndex = queue.findIndex(predicate);
              if (queuedIndex !== -1) {
                resolve(queue.splice(queuedIndex, 1)[0]);
                return;
              }
              const waiter = { predicate, resolve, timeout: null };
              waiter.timeout = setTimeout(
                () => reject(new Error("Timed out waiting for AWP message")),
                10000
              );
              waiters.push(waiter);
            });
          }

          // These are two genuine AWP responses on the same popup port, not
          // synthesized messages. Consuming only the first leaves exactly the
          // stale collections reply that the loaded popup UI can leave ahead
          // of the hook's newColl response during a busy crawl.
          port.postMessage({ type: "startUpdates", tabId: targetTabId });
          port.postMessage({ type: "startUpdates", tabId: targetTabId });
          await waitFor((message) => message?.type === "collections");
          await secondCollectionsQueued;

          port.postMessage({ type: "newColl", title: collectionTitle });
          const created = await waitFor(
            (message) =>
              message?.type === "collections" &&
              message.collections?.some(
                (item) => item.title === collectionTitle
              )
          );
          return [
            ...queue.filter((message) => message?.type === "collections"),
            created,
          ];
        },
        { targetTabId: tabId, collectionTitle }
      );
      const selected = messages.find((message) =>
        Boolean(
          awpInternal.resolveCreatedCollectionId(message, collectionTitle)
        )
      );
      const selectedId = awpInternal.resolveCreatedCollectionId(
        selected,
        collectionTitle
      );
      const created = messages.find((message) =>
        message.collections?.some((item) => item.title === collectionTitle)
      );
      const matchingId = created.collections.find(
        (item) => item.title === collectionTitle
      ).id;
      process.stdout.write(JSON.stringify({ selectedId, matchingId }));
    } finally {
      await helperPage.close({ runBeforeUnload: false }).catch(() => {});
    }
  } finally {
    await browser.disconnect();
  }
})().catch((error) => {
  console.error(error && (error.stack || error.message || String(error)));
  process.exit(1);
});
"""
    result = subprocess.run(
        [
            env["NODE_BINARY"],
            "-e",
            script,
            str(CHROME_UTILS),
            str(AWP_INTERNAL),
            str(snapshot_chrome_dir),
            collection_title,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    selected = json.loads(result.stdout)
    assert isinstance(selected, dict)
    assert isinstance(selected.get("selectedId"), str)
    assert isinstance(selected.get("matchingId"), str)
    typed_selection = CollectionSelection(
        selectedId=selected["selectedId"],
        matchingId=selected["matchingId"],
    )
    return typed_selection["selectedId"], typed_selection["matchingId"]


def _publish_child_snapshot_session(
    crawl_chrome_dir: Path,
    child_chrome_dir: Path,
    child_target_id: str,
    env: dict[str, str],
) -> None:
    child_chrome_dir.mkdir(parents=True)
    for file_name in ("browser.json", "cdp_url.txt", "chrome.pid"):
        shutil.copy2(crawl_chrome_dir / file_name, child_chrome_dir / file_name)
    (child_chrome_dir / "target_id.txt").write_text(f"{child_target_id}\n")
    (child_chrome_dir / "url.txt").write_text("about:blank\n")
    wait_for_chrome_session_state(
        child_chrome_dir,
        env=env,
        require_target_id=True,
        require_browser_ready=True,
        require_connectable=True,
    )


def test_start_reassigns_inherited_tab_recorder_to_its_requested_collection(
    tmp_path,
    chrome_test_url,
):
    env = setup_test_env(tmp_path)
    env.update(
        {
            "CHROME_HEADLESS": "true",
            "CHROME_ISOLATION": "crawl",
            "CHROME_KEEPALIVE": "false",
        },
    )
    extensions_dir = Path(env["CHROMEWEBSTORE_EXTENSIONS_DIR"])
    installed = install_required_binary_from_config(
        ARCHIVEWEBPAGE_PLUGIN_DIR,
        "archivewebpage",
        env=env,
    )
    assert installed.loaded_abspath is not None
    assert extensions_dir.joinpath("archivewebpage.extension.json").is_file()

    crawl_dir = Path(env["CRAWL_DIR"])
    crawl_chrome_dir = crawl_dir / "chrome"
    launch_process = None
    first_tab_process = None
    try:
        launch_process, _cdp_url = launch_chromium_session(
            env,
            crawl_chrome_dir,
            "test-archivewebpage-concurrent-collections",
        )

        first_snapshot_dir = tmp_path / "snapshots" / "first"
        first_chrome_dir = first_snapshot_dir / "chrome"
        first_chrome_dir.mkdir(parents=True)
        first_env = env | {"SNAP_DIR": str(first_snapshot_dir)}
        first_tab_process = launch_snapshot_tab(
            snapshot_chrome_dir=first_chrome_dir,
            tab_env=first_env,
            test_url=chrome_test_url,
            snapshot_id="first",
            crawl_id="test-archivewebpage-concurrent-collections",
        )
        first_start = _run_start_hook(first_snapshot_dir, first_env, chrome_test_url)
        assert first_start.returncode == 0, (
            f"First AWP start failed:\nstdout={first_start.stdout}\nstderr={first_start.stderr}"
        )
        first_state = json.loads(
            (first_snapshot_dir / "archivewebpage" / "recording.json").read_text(),
        )
        first_status = _read_awp_status(first_chrome_dir, first_env)
        assert first_status["collId"] == first_state["collId"]
        assert (
            f"[model-{first_snapshot_dir.name}/" in first_status["collectionTitle"]
        ), (
            "The collection title lost the --snapshot-id model identity and silently "
            "fell back to the output directory name"
        )

        selected_id, matching_id = _select_new_collection_after_queued_updates(
            first_chrome_dir,
            first_env,
            "queued-collections-regression",
        )
        assert selected_id == matching_id, (
            "The newColl response selector consumed an older queued collections "
            "message instead of the collection with the requested title"
        )

        # Exercise the user-facing inherited-tab path with the same four-way
        # snapshot concurrency as ArchiveBox. The lower-stack assertion above
        # covers queued newColl correlation; these hooks prove each real child
        # recorder still moves off its opener's collection independently.
        child_runs: list[tuple[Path, Path, dict[str, str], str]] = []
        for index in range(4):
            child_target_id = _open_child_target(first_chrome_dir, first_env)
            child_snapshot_dir = tmp_path / "snapshots" / f"child-{index}"
            child_chrome_dir = child_snapshot_dir / "chrome"
            child_env = env | {"SNAP_DIR": str(child_snapshot_dir)}
            child_url = f"{chrome_test_url}#child-{index}"
            _publish_child_snapshot_session(
                crawl_chrome_dir,
                child_chrome_dir,
                child_target_id,
                child_env,
            )
            if index == 0:
                inherited_status = _read_awp_status(child_chrome_dir, child_env)
                assert inherited_status["collId"] == first_state["collId"]
            child_runs.append(
                (child_snapshot_dir, child_chrome_dir, child_env, child_url),
            )

        with ThreadPoolExecutor(max_workers=len(child_runs)) as executor:
            child_starts = list(
                executor.map(
                    lambda run: _run_start_hook(run[0], run[2], run[3]),
                    child_runs,
                ),
            )
        for child_start in child_starts:
            assert child_start.returncode == 0, (
                "Concurrent child AWP start failed:\n"
                f"stdout={child_start.stdout}\nstderr={child_start.stderr}"
            )

        child_states = [
            json.loads(
                (snapshot_dir / "archivewebpage" / "recording.json").read_text(),
            )
            for snapshot_dir, _chrome_dir, _child_env, _url in child_runs
        ]
        child_collection_ids = {state["collId"] for state in child_states}
        assert first_state["collId"] not in child_collection_ids
        assert len(child_collection_ids) == len(child_runs), (
            "Concurrent ArchiveWeb.page newColl requests reused a collection id"
        )

        for child_run, child_state in zip(child_runs, child_states, strict=True):
            _snapshot_dir, child_chrome_dir, child_env, _url = child_run
            child_status = _read_awp_status(child_chrome_dir, child_env)
            assert child_status["collId"] == child_state["collId"], (
                "The ArchiveWeb.page start hook reported success without moving the "
                "child tab from its inherited collection to the newly requested collection"
            )
        first_status_after_reassignment = _read_awp_status(first_chrome_dir, first_env)
        assert first_status_after_reassignment["recording"] is True
        assert first_status_after_reassignment["collId"] == first_state["collId"]
    finally:
        if first_tab_process is not None:
            if first_tab_process.poll() is None:
                first_tab_process.send_signal(signal.SIGTERM)
            try:
                first_tab_process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                first_tab_process.kill()
                first_tab_process.wait(timeout=5)
            for attr in ("_stdout_handle", "_stderr_handle"):
                handle = getattr(first_tab_process, attr, None)
                if handle:
                    handle.close()
        if launch_process is not None:
            kill_chromium_session(launch_process, crawl_chrome_dir)

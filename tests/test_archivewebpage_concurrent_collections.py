import json
import gzip
import re
import shutil
import signal
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit

import pytest

from abx_plugins.plugins.base.testing import (
    install_required_binary_from_config,
    parse_jsonl_output,
)
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
ARCHIVEWEBPAGE_STOP_HOOK = (
    ARCHIVEWEBPAGE_PLUGIN_DIR / "on_Snapshot__65_archivewebpage_stop.js"
)
CHROME_NAVIGATE_HOOK = CHROME_UTILS.parent / "on_Snapshot__30_chrome_navigate.js"
UBLOCK_CONFIG_HOOK = (
    CHROME_UTILS.parent.parent / "ublock" / "on_CrawlSetup__95_ublock_config.js"
)
AWP_INTERNAL = ARCHIVEWEBPAGE_PLUGIN_DIR / "awp_internal.js"
AWP_PREPARE_HOOK = (
    ARCHIVEWEBPAGE_PLUGIN_DIR / "on_CrawlSetup__80_archivewebpage_prepare.py"
)
INFINISCROLL_HOOK = (
    CHROME_UTILS.parent.parent / "infiniscroll" / "on_Snapshot__45_infiniscroll.js"
)


class AwpStatus(TypedDict):
    type: str
    recording: bool
    collId: str
    collectionTitle: str
    numUrls: int
    numPages: int
    sizeTotal: int


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
    try:
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
    except subprocess.TimeoutExpired as error:
        stdout = (
            error.stdout.decode(errors="replace")
            if isinstance(error.stdout, bytes)
            else error.stdout
        )
        stderr = (
            error.stderr.decode(errors="replace")
            if isinstance(error.stderr, bytes)
            else error.stderr
        )
        raise AssertionError(
            f"ArchiveWebPage start timed out for {snapshot_dir.name}:\n"
            f"stdout={stdout or ''}\nstderr={stderr or ''}",
        ) from error


def _run_navigate_hook(
    snapshot_dir: Path,
    env: dict[str, str],
    url: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CHROME_NAVIGATE_HOOK), f"--url={url}"],
        cwd=snapshot_dir / "chrome",
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _run_stop_hook(
    snapshot_dir: Path,
    env: dict[str, str],
    url: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(ARCHIVEWEBPAGE_STOP_HOOK), f"--url={url}"],
        cwd=snapshot_dir / "archivewebpage",
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _run_ublock_config_hook(
    crawl_chrome_dir: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(UBLOCK_CONFIG_HOOK), "--url=https://example.com/"],
        cwd=crawl_chrome_dir.parent,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _stop_tab_process(tab_process: subprocess.Popen[str]) -> None:
    if tab_process.poll() is None:
        tab_process.send_signal(signal.SIGTERM)
    try:
        tab_process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        tab_process.kill()
        tab_process.wait(timeout=5)
    for attr in ("_stdout_handle", "_stderr_handle"):
        handle = getattr(tab_process, attr, None)
        if handle:
            handle.close()


def test_wacz_publication_observer_owns_real_fs_events_and_errors(tmp_path) -> None:
    """Observe the exact published file and keep watcher errors awaitable.

    WHY: Chrome's completed event can precede the final rename, ``fs.watch``
    filenames are optional, and a watcher backend can fail after startup. The
    observer must therefore stat only the exact expected path on every real
    filesystem event, ignore unrelated publications, and expose filesystem
    failures through its promise rather than an unhandled EventEmitter error.
    """
    env = setup_test_env(tmp_path)
    watch_dir = tmp_path / "chrome-downloads"
    script = r"""
const fs = require("fs");
const path = require("path");
const awpInternal = require(process.argv[1]);
const watchDir = process.argv[2];

(async () => {
  if (typeof awpInternal.observePublishedFile !== "function") {
    throw new Error("awp_internal does not export observePublishedFile");
  }
  fs.mkdirSync(watchDir, { recursive: true });
  const expectedPath = path.join(watchDir, "expected.wacz");
  const publication = awpInternal.observePublishedFile(expectedPath, 5000);
  let settled = false;
  publication.promise.finally(() => { settled = true; });

  fs.writeFileSync(path.join(watchDir, "unrelated.wacz"), "wrong artifact");
  await new Promise((resolve) => setTimeout(resolve, 100));
  if (settled) throw new Error("unrelated file settled exact WACZ observer");

  fs.writeFileSync(expectedPath, "real archivewebpage output");
  const stat = await publication.promise;
  if (stat.size !== Buffer.byteLength("real archivewebpage output")) {
    throw new Error(`observer returned wrong WACZ size: ${stat.size}`);
  }

  const failed = awpInternal.observePublishedFile(
    path.join(watchDir, "missing-directory", "never.wacz"),
    5000
  );
  try {
    await failed.promise;
    throw new Error("missing watch directory unexpectedly succeeded");
  } catch (error) {
    if (!error || !["ENOENT", "ERR_FS_WATCHER_INIT_FAILED"].includes(error.code)) {
      throw error;
    }
  } finally {
    failed.close();
  }
  process.stdout.write("observer behavior verified");
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
            str(AWP_INTERNAL),
            str(watch_dir),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "observer behavior verified"


@pytest.fixture
def archivewebpage_crawl(
    tmp_path,
) -> Iterator[tuple[dict[str, str], Path, list[subprocess.Popen[str]]]]:
    """Run the real shared-browser topology used by an ArchiveBox crawl."""
    env = setup_test_env(tmp_path)
    env.update(
        {
            "CHROME_HEADLESS": "true",
            "CHROME_ISOLATION": "crawl",
            "CHROME_KEEPALIVE": "false",
        },
    )
    for plugin_name in ("archivewebpage", "ublock"):
        plugin_dir = CHROME_UTILS.parent.parent / plugin_name
        installed = install_required_binary_from_config(
            plugin_dir,
            plugin_name,
            env=env,
        )
        assert installed.loaded_abspath is not None

    crawl_chrome_dir = Path(env["CRAWL_DIR"]) / "chrome"
    prepare = subprocess.run(
        [str(AWP_PREPARE_HOOK)],
        cwd=crawl_chrome_dir.parent,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert prepare.returncode == 0, (
        "ArchiveWeb.page crawl preparation failed:\n"
        f"stdout={prepare.stdout}\nstderr={prepare.stderr}"
    )
    launch_process, _cdp_url = launch_chromium_session(
        env,
        crawl_chrome_dir,
        "test-archivewebpage-lifecycle",
    )
    ublock_config = _run_ublock_config_hook(crawl_chrome_dir, env)
    assert ublock_config.returncode == 0, (
        "uBlock crawl configuration failed:\n"
        f"stdout={ublock_config.stdout}\nstderr={ublock_config.stderr}"
    )
    tab_processes: list[subprocess.Popen[str]] = []
    try:
        yield env, crawl_chrome_dir, tab_processes
    finally:
        for tab_process in tab_processes:
            _stop_tab_process(tab_process)
        kill_chromium_session(launch_process, crawl_chrome_dir)


def _start_snapshot_recording(
    root: Path,
    base_env: dict[str, str],
    tab_processes: list[subprocess.Popen[str]],
    *,
    snapshot_id: str,
    url: str,
) -> tuple[Path, dict[str, str]]:
    snapshot_dir = root / snapshot_id
    chrome_dir = snapshot_dir / "chrome"
    chrome_dir.mkdir(parents=True)
    env = base_env | {"SNAP_DIR": str(snapshot_dir)}
    tab_processes.append(
        launch_snapshot_tab(
            snapshot_chrome_dir=chrome_dir,
            tab_env=env,
            test_url=url,
            snapshot_id=snapshot_id,
            crawl_id="test-archivewebpage-lifecycle",
        ),
    )
    start = _run_start_hook(snapshot_dir, env, url)
    assert start.returncode == 0, (
        f"AWP start failed for {url}:\nstdout={start.stdout}\nstderr={start.stderr}"
    )
    navigate = _run_navigate_hook(snapshot_dir, env, url)
    assert navigate.returncode == 0, (
        f"Chrome navigation failed for {url}:\n"
        f"stdout={navigate.stdout}\nstderr={navigate.stderr}"
    )
    return snapshot_dir, env


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
    assert isinstance(status.get("numUrls"), int), status
    assert isinstance(status.get("numPages"), int), status
    assert isinstance(status.get("sizeTotal"), int), status
    return AwpStatus(
        type=status["type"],
        recording=status["recording"],
        collId=status["collId"],
        collectionTitle=status["collectionTitle"],
        numUrls=status["numUrls"],
        numPages=status["numPages"],
        sizeTotal=status["sizeTotal"],
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


def test_cowpig_recording_survives_overlapping_snapshot_lifecycles(
    archivewebpage_crawl,
    chrome_test_url,
    tmp_path,
):
    """AWP must retain the exact Cowpig target until its stop hook completes.

    WHY: the real depth=1 crawl lost this target while its tab-owner daemon was
    still alive, then stop failed in Target.attachToTarget. Three sibling
    snapshots were recording in the same crawl browser at that moment, so this
    regression preserves that overlap and exercises the actual AWP/Chrome
    hooks instead of manufacturing a closed target or protocol exception.
    """
    env, _crawl_chrome_dir, tab_processes = archivewebpage_crawl
    cases = (
        ("cowpig", "https://cowpig.github.io/about/"),
        ("sibling-one", f"{chrome_test_url}#sibling-one"),
        ("sibling-two", f"{chrome_test_url}#sibling-two"),
        ("sibling-three", f"{chrome_test_url}#sibling-three"),
    )
    snapshots_root = tmp_path / "overlapping-snapshots"
    with ThreadPoolExecutor(max_workers=len(cases)) as executor:
        runs = list(
            executor.map(
                lambda case: _start_snapshot_recording(
                    snapshots_root,
                    env,
                    tab_processes,
                    snapshot_id=case[0],
                    url=case[1],
                ),
                cases,
            ),
        )

    cowpig_dir, cowpig_env = runs[0]
    stop = _run_stop_hook(cowpig_dir, cowpig_env, cases[0][1])
    assert stop.returncode == 0, (
        "Cowpig AWP stop lost a live lifecycle-owned target:\n"
        f"stdout={stop.stdout}\nstderr={stop.stderr}"
    )
    result = parse_jsonl_output(stop.stdout)
    assert result is not None and result["num_urls"] > 0, (stop.stdout, stop.stderr)
    assert (cowpig_dir / "archivewebpage" / "archivewebpage.wacz").stat().st_size > 0


def test_concurrent_stops_each_publish_their_exact_wacz(
    archivewebpage_crawl,
    tmp_path,
):
    """Two simultaneous AWP exports must not lose either completed download.

    WHY: the real nicksweeting.com and nicksweeting.com/ snapshots reached the
    stop hook 46ms apart. Chrome reported the second unique download complete,
    but its expected file did not exist in the shared persona download dir.
    These are the same two URLs, one browser, two tabs, two collections, and
    concurrent real stop/export hooks that produced the ENOENT.
    """
    env, _crawl_chrome_dir, tab_processes = archivewebpage_crawl
    cases = (
        ("nicksweeting-no-slash", "https://nicksweeting.com"),
        ("nicksweeting-slash", "https://nicksweeting.com/"),
    )
    snapshots_root = tmp_path / "concurrent-export-snapshots"
    with ThreadPoolExecutor(max_workers=len(cases)) as executor:
        runs = list(
            executor.map(
                lambda case: _start_snapshot_recording(
                    snapshots_root,
                    env,
                    tab_processes,
                    snapshot_id=case[0],
                    url=case[1],
                ),
                cases,
            ),
        )
        stop_inputs = (
            (snapshot_dir, snapshot_env, case[1])
            for case, (snapshot_dir, snapshot_env) in zip(cases, runs, strict=True)
        )
        stops = list(
            executor.map(
                lambda stop_input: _run_stop_hook(*stop_input),
                stop_inputs,
            ),
        )

    for case, run, stop in zip(cases, runs, stops, strict=True):
        assert stop.returncode == 0, (
            f"Concurrent AWP stop failed for {case[1]}:\n"
            f"stdout={stop.stdout}\nstderr={stop.stderr}"
        )
        result = parse_jsonl_output(stop.stdout)
        assert result is not None and result["num_urls"] > 0, (
            case[1],
            stop.stdout,
            stop.stderr,
        )
        output = run[0] / "archivewebpage" / "archivewebpage.wacz"
        assert output.stat().st_size > 0


def test_empty_recordings_are_classified_from_awp_collection_counts(
    archivewebpage_crawl,
    tmp_path,
):
    """Distinguish empty failed navigation from a real binary response.

    WHY: the real cookie-dilemma crawl produced a successful 2.6 KB WACZ that
    contained only WARCinfo after DNS failed. AWP's status reports URL and page
    counts, which let the stop hook reject that empty export without rejecting
    valid attachment-only captures that may have no page-index entry.
    """
    env, _crawl_chrome_dir, tab_processes = archivewebpage_crawl
    attachment_url = (
        "https://docs.monadical.com/uploads/038c6f29-0106-4064-88e2-51affe90bb83.png"
    )
    cases = (
        ("attachment-only", attachment_url, "succeeded"),
        (
            "dns-empty",
            "https://archivebox-awp-empty.invalid/",
            "failed",
        ),
    )

    for snapshot_id, url, expected_status in cases:
        snapshot_dir = tmp_path / "empty-recording-cases" / snapshot_id
        chrome_dir = snapshot_dir / "chrome"
        chrome_dir.mkdir(parents=True)
        snapshot_env = env | {"SNAP_DIR": str(snapshot_dir)}
        tab_processes.append(
            launch_snapshot_tab(
                snapshot_chrome_dir=chrome_dir,
                tab_env=snapshot_env,
                test_url=url,
                snapshot_id=snapshot_id,
                crawl_id="test-archivewebpage-lifecycle",
            ),
        )
        start = _run_start_hook(snapshot_dir, snapshot_env, url)
        assert start.returncode == 0, (start.stdout, start.stderr)
        navigate = _run_navigate_hook(snapshot_dir, snapshot_env, url)
        if snapshot_id == "dns-empty":
            assert navigate.returncode != 0, (navigate.stdout, navigate.stderr)
            navigation = json.loads(
                (snapshot_dir / "chrome" / "navigation.json").read_text(),
            )
            assert "ERR_NAME_NOT_RESOLVED" in navigation["error"]
        elif snapshot_id == "attachment-only":
            assert navigate.returncode != 0, (navigate.stdout, navigate.stderr)
            navigation = json.loads(
                (snapshot_dir / "chrome" / "navigation.json").read_text(),
            )
            assert navigation["status"] == 200
            assert navigation["content_type"] == "image/png"

        live_status = _read_awp_status(chrome_dir, snapshot_env)
        if snapshot_id == "dns-empty":
            assert live_status["numUrls"] == 0, live_status
        else:
            assert live_status["numUrls"] > 0, live_status

        stop = _run_stop_hook(snapshot_dir, snapshot_env, url)
        result = parse_jsonl_output(stop.stdout)
        assert result is not None, (stop.stdout, stop.stderr)
        assert stop.returncode == (0 if expected_status != "failed" else 1), (
            snapshot_id,
            stop.returncode,
            stop.stdout,
            stop.stderr,
        )
        assert result["status"] == expected_status, (
            snapshot_id,
            result,
            stop.stderr,
        )
        if snapshot_id == "dns-empty":
            assert "0 URLs recorded; Chrome navigation failed" in result["output_str"]
            assert "ERR_NAME_NOT_RESOLVED" in result["output_str"]
        assert result["num_urls"] == live_status["numUrls"], result
        assert result["num_pages"] == live_status["numPages"], result
        for phase in ("connect", "lock", "stop", "export"):
            assert f"phase={phase} elapsed_ms=" in stop.stderr, stop.stderr
        output = snapshot_dir / "archivewebpage" / "archivewebpage.wacz"
        assert output.is_file() and output.stat().st_size > 0
        assert result["output_size"] == output.stat().st_size


def test_ublock_never_replaces_the_recorded_page_with_strictblock(
    archivewebpage_crawl,
    tmp_path,
):
    """uBlock must not replace AWP's recorded top-level document.

    WHY: the real Cloudflare beacon snapshot began recording successfully, but
    uBlock replaced its top-level navigation with strictblock.html. AWP then
    reported recording=false for the same tab and exact collection, and the
    stop hook failed without exporting the already captured collection.
    """
    env, _crawl_chrome_dir, tab_processes = archivewebpage_crawl
    url = (
        "https://static.cloudflareinsights.com/beacon.min.js/"
        "v3d52b47920f24c319d37e2661827c42b1787588026925"
    )
    snapshot_dir, snapshot_env = _start_snapshot_recording(
        tmp_path / "strictblock-snapshots",
        env,
        tab_processes,
        snapshot_id="cloudflare-beacon",
        url=url,
    )
    navigation = json.loads((snapshot_dir / "chrome" / "navigation.json").read_text())
    assert not navigation["finalUrl"].startswith("chrome-extension://"), navigation
    assert "/strictblock.html#" not in navigation["finalUrl"], navigation

    stop = _run_stop_hook(snapshot_dir, snapshot_env, url)
    assert stop.returncode == 0, (
        "AWP discarded the exact collection after uBlock strict-blocked navigation:\n"
        f"stdout={stop.stdout}\nstderr={stop.stderr}"
    )
    assert (snapshot_dir / "archivewebpage" / "archivewebpage.wacz").stat().st_size > 0


def test_webrecorder_embedded_replay_keeps_worker_response_in_wacz(
    archivewebpage_crawl,
    tmp_path,
):
    """Keep ReplayWeb.page's service-worker response usable during recording.

    WHY: ArchiveWeb.page's recorder enables CDP service-worker bypass for its
    own capture bookkeeping. ReplayWeb.page also relies on a service worker to
    serve its embedded ``/replay/`` application, so recording must not turn that
    route into recursive copies of the marketing page or omit its response.
    """
    env, _crawl_chrome_dir, tab_processes = archivewebpage_crawl
    url = "https://webrecorder.net/replaywebpage/"
    snapshot_dir = tmp_path / "webrecorder-replaywebpage"
    chrome_dir = snapshot_dir / "chrome"
    chrome_dir.mkdir(parents=True)
    infiniscroll_dir = snapshot_dir / "infiniscroll"
    infiniscroll_dir.mkdir(parents=True)
    snapshot_env = env | {"SNAP_DIR": str(snapshot_dir)}
    tab_processes.append(
        launch_snapshot_tab(
            snapshot_chrome_dir=chrome_dir,
            tab_env=snapshot_env,
            test_url=url,
            snapshot_id="webrecorder-replaywebpage",
            crawl_id="test-archivewebpage-lifecycle",
        ),
    )

    start = _run_start_hook(snapshot_dir, snapshot_env, url)
    assert start.returncode == 0, (
        f"ArchiveWeb.page start failed:\nstdout={start.stdout}\nstderr={start.stderr}"
    )

    monitor_script = r"""
const { spawn } = require("child_process");
const chromeUtils = require(process.argv[1]);
const chromeSessionDir = process.argv[2];
const navigateHook = process.argv[3];
const url = process.argv[4];
const cwd = process.argv[5];
const screenshotPath = process.argv[6];
const infiniscrollHook = process.argv[7];
const infiniscrollCwd = process.argv[8];

(async () => {
  const puppeteer = chromeUtils.resolvePuppeteerModule();
  const { browser, page } = await chromeUtils.connectToPage({
    chromeSessionDir,
    timeoutMs: 10000,
    requireTargetId: true,
    puppeteer,
  });
  try {
    const replayResponses = [];
    const workerResponse = page.waitForResponse(
      (response) => {
        let pathname = "";
        try { pathname = new URL(response.url()).pathname; } catch {}
        return pathname === "/replay/" &&
          response.status() === 200 && response.fromServiceWorker();
      },
      { timeout: 30000 }
    ).then(
      async (response) => ({
        status: response.status(),
        fromServiceWorker: response.fromServiceWorker(),
        mimeType: response.headers()["content-type"] || "",
        bodyBytes: (await response.buffer()).byteLength,
      }),
      (error) => ({ error: error.message })
    );
    page.on("response", (response) => {
      let parsed;
      try { parsed = new URL(response.url()); } catch { return; }
      if (parsed.pathname === "/replay/") {
        replayResponses.push({
          path: parsed.pathname,
          status: response.status(),
          fromServiceWorker: response.fromServiceWorker(),
          mimeType: response.headers()["content-type"] || "",
        });
      }
    });

    const navigation = spawn(navigateHook, [`--url=${url}`], {
      cwd,
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const exit = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        navigation.kill("SIGTERM");
        reject(new Error("Chrome navigation hook exceeded its existing test bound"));
      }, 65000);
      navigation.once("error", (error) => { clearTimeout(timeout); reject(error); });
      navigation.once("exit", (code, signal) => {
        clearTimeout(timeout);
        resolve({ code, signal });
      });
    });
    let worker = null;
    worker = await workerResponse;
    const scroll = spawn(infiniscrollHook, [`--url=${url}`], {
      cwd: infiniscrollCwd,
      env: process.env,
      stdio: "ignore",
    });
    const scrollExit = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        scroll.kill("SIGTERM");
        reject(new Error("Infiniscroll hook exceeded its configured timeout"));
      }, 130000);
      scroll.once("error", (error) => { clearTimeout(timeout); reject(error); });
      scroll.once("exit", (code, signal) => {
        clearTimeout(timeout);
        resolve({ code, signal });
      });
    });
    const frames = await Promise.all(page.frames().map(async (frame) => {
      try {
        const parsed = new URL(frame.url());
        const state = await frame.evaluate(() => ({
          readyState: document.readyState,
          bodyBytes: new TextEncoder().encode(document.documentElement?.outerHTML || "").length,
          textBytes: new TextEncoder().encode(document.body?.innerText || "").length,
          bodyElements: document.body?.childElementCount || 0,
          childFrames: document.querySelectorAll("iframe").length,
        })).catch(() => null);
        return {
          pathKind: parsed.pathname === "/replay/"
            ? "replay-shell"
            : parsed.pathname.startsWith("/replay/w/")
              ? "archived-replay"
              : parsed.pathname.startsWith("/replay/")
                ? "replay-resource"
                : "other",
          state,
        };
      } catch { return null; }
    }));
    const widgets = await page.evaluate(() => Array.from(
      document.querySelectorAll("replay-web-page")
    ).map((widget) => {
      const rect = widget.getBoundingClientRect();
      const frame = widget.shadowRoot?.querySelector("iframe");
      return {
        visible: Boolean(rect.width && rect.height),
        framePresent: Boolean(frame),
        frameReadyState: frame?.contentDocument?.readyState || null,
        frameBodyBytes: new TextEncoder().encode(frame?.contentDocument?.documentElement?.outerHTML || "").length,
      };
    }));
    await page.screenshot({ path: screenshotPath, fullPage: false });
    process.stdout.write(JSON.stringify({
      navigation: exit,
      infiniscroll: scrollExit,
      worker,
      replayResponses: {
        count: replayResponses.length,
        statuses: replayResponses.reduce((counts, response) => {
          const key = `${response.status}:${response.fromServiceWorker}`;
          counts[key] = (counts[key] || 0) + 1;
          return counts;
        }, {}),
      },
      frames,
      widgets,
    }));
  } finally {
    await browser.disconnect();
  }
})().catch((error) => {
  console.error(error && (error.stack || error.message || String(error)));
  process.exit(1);
});
"""
    monitor = subprocess.run(
        [
            snapshot_env["NODE_BINARY"],
            "-e",
            monitor_script,
            str(CHROME_UTILS),
            str(chrome_dir),
            str(CHROME_NAVIGATE_HOOK),
            url,
            str(chrome_dir),
            str(tmp_path / "webrecorder-replay-captured.png"),
            str(INFINISCROLL_HOOK),
            str(infiniscroll_dir),
        ],
        cwd=chrome_dir,
        capture_output=True,
        text=True,
        timeout=180,
        env=snapshot_env,
    )
    assert monitor.returncode == 0, monitor.stderr
    observed = json.loads(monitor.stdout)
    assert observed["navigation"]["code"] == 0, observed
    assert observed["infiniscroll"]["code"] == 0, observed
    assert observed["worker"] is not None, (
        "ReplayWeb.page's /replay/ application was not served by its service "
        f"worker with response bytes: {observed}"
    )
    assert "error" not in observed["worker"], observed
    assert observed["worker"]["bodyBytes"] > 0, observed
    assert observed["worker"]["mimeType"].startswith("text/html"), observed
    archived_frames = [
        frame
        for frame in observed["frames"]
        if frame and frame["pathKind"] == "archived-replay" and frame["state"]
    ]
    assert any(
        frame["state"]["readyState"] == "complete"
        and frame["state"]["textBytes"] > 1000
        and frame["state"]["bodyElements"] > 0
        for frame in archived_frames
    ), observed
    assert any(
        widget["visible"] and widget["framePresent"] for widget in observed["widgets"]
    ), observed

    stop = _run_stop_hook(snapshot_dir, snapshot_env, url)
    assert stop.returncode == 0, (
        f"ArchiveWeb.page stop failed:\nstdout={stop.stdout}\nstderr={stop.stderr}"
    )
    result = parse_jsonl_output(stop.stdout)
    assert result is not None and result["status"] == "succeeded", (
        stop.stdout,
        stop.stderr,
    )
    wacz = snapshot_dir / "archivewebpage" / "archivewebpage.wacz"
    assert wacz.is_file() and wacz.stat().st_size > 0
    with zipfile.ZipFile(wacz) as archive:
        warc_bytes = gzip.decompress(archive.read("archive/data.warc.gz"))
    replay_shell_bodies = []
    archived_page_bodies = []
    offset = 0
    while offset < len(warc_bytes):
        header_end = warc_bytes.find(b"\r\n\r\n", offset)
        if header_end < 0:
            break
        warc_header = warc_bytes[offset:header_end]
        length_match = re.search(rb"(?im)^Content-Length:\s*(\d+)", warc_header)
        assert length_match is not None, f"WARC record lacks Content-Length at {offset}"
        payload_length = int(length_match.group(1))
        payload_start = header_end + 4
        payload_end = payload_start + payload_length
        payload = warc_bytes[payload_start:payload_end]
        fields = {}
        for header in warc_header.split(b"\r\n")[1:]:
            if b": " in header:
                key, value = header.split(b": ", 1)
                fields[key.decode("latin1")] = value.decode("latin1")
        if fields.get("WARC-Type") == "response":
            target = urlsplit(fields.get("WARC-Target-URI", ""))
            http_header_end = payload.find(b"\r\n\r\n")
            if target.path.startswith("/replay/") and http_header_end >= 0:
                status_line = payload[:http_header_end].split(b"\r\n", 1)[0]
                response_headers = payload[:http_header_end].lower()
                response_body = payload[http_header_end + 4 :]
                if status_line.startswith(b"HTTP/") and b" 200 " in status_line:
                    if target.path == "/replay/":
                        replay_shell_bodies.append(response_body)
                    elif (
                        target.path.startswith("/replay/w/")
                        and b"content-type: text/html" in response_headers
                    ):
                        archived_page_bodies.append(response_body)
        offset = payload_end
        while warc_bytes[offset : offset + 4] == b"\r\n\r\n":
            offset += 4
    assert any(body for body in replay_shell_bodies), (
        "WACZ has no non-empty HTTP 200 body for the service-worker /replay/ page"
    )
    assert any(body for body in archived_page_bodies), (
        "WACZ has no non-empty HTTP 200 HTML body for the embedded archived page"
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
            _stop_tab_process(first_tab_process)
        if launch_process is not None:
            kill_chromium_session(launch_process, crawl_chrome_dir)

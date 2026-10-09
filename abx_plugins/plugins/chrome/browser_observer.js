// Optional browser UI metadata and latest-frame mailbox, owned by Chrome's
// existing connection. No browser launch, per-request CDP probe, or frame queue.
const fs = require("fs");
const path = require("path");
const { writeFileAtomic } = require("../base/utils.js");
const { getTargetIdFromTarget: idOf } = require("./chrome_utils.js");
const { startPageScreencast } = require("../chrome_screencast/screencast.js");

async function observeBrowser(browser, directory) {
  const filename = path.join(directory, "tabs.json");
  const viewersDir = path.join(directory, "viewers");
  const framesDir = path.join(directory, "frames");
  fs.mkdirSync(viewersDir, { recursive: true });
  fs.mkdirSync(framesDir, { recursive: true });
  try {
    const previous = JSON.parse(fs.readFileSync(filename, "utf8"));
    if (previous.connected && previous.cdp_url === browser.wsEndpoint()) {
      process.kill(previous.owner_pid, 0);
      return;
    }
  } catch {}
  const root = await browser.target().createCDPSession();
  const tabs = new Map(), watchers = new Map(), casts = new Map();
  let closed = false, busy = false, again = false;
  const publish = () => {
    try {
      writeFileAtomic(filename, JSON.stringify({ cdp_url: browser.wsEndpoint(), owner_pid: process.pid,
        connected: !closed, tabs: [...tabs.values()] }));
    } catch (error) { console.error(`Browser inventory unavailable: ${error.message}`); }
  };
  const framePath = id => path.join(framesDir, `${id}.json`);
  const removeFrame = id => { try { fs.unlinkSync(framePath(id)); } catch {} };
  async function stopCast(id) {
    const cast = casts.get(id); casts.delete(id); removeFrame(id);
    if (cast) await cast().catch(() => {});
  }
  async function updateViews() {
    if (closed) return;
    if (busy) { again = true; return; }
    busy = true;
    try {
      const requested = new Set();
      for (const entry of fs.readdirSync(viewersDir)) {
        if (!entry.endsWith(".json")) continue;
        try {
          const file = path.join(viewersDir, entry);
          // Expire abandoned viewer demand only; never browser/auth state.
          if (Date.now() - fs.statSync(file).mtimeMs > 5000) { fs.unlinkSync(file); continue; }
          const request = JSON.parse(fs.readFileSync(file, "utf8"));
          if (request.cdp_url === browser.wsEndpoint() && tabs.get(request.target_id)?.available) requested.add(request.target_id);
        } catch {}
      }
      for (const id of casts.keys()) if (!requested.has(id)) await stopCast(id);
      for (const id of requested) {
        if (casts.has(id)) continue;
        try {
          const tab = tabs.get(id), documentId = tab.document_id;
          const page = await browser.targets().find(target => idOf(target) === id)?.page();
          if (!page) continue;
          const stop = await startPageScreencast(page, (jpeg, metadata) => {
            if (!closed && tab.available && tab.document_id === documentId) {
              writeFileAtomic(framePath(id), JSON.stringify({ type: "frame", document_id: documentId,
                captured_at: metadata.timestamp * 1000, data: jpeg.toString("base64") }) + "\n");
            }
          }, { fps: 10, bringToFront: true });
          if (closed || tab.document_id !== documentId || !tab.available) await stop();
          else casts.set(id, stop);
        } catch (error) {
          // A preview failure does not invalidate the automation tab.
          if (tabs.has(id)) { tabs.get(id).preview_error = error.message; publish(); }
        }
      }
    } catch (error) { console.error(`Browser preview unavailable: ${error.message}`); }
    finally { busy = false; if (again) { again = false; void updateViews(); } }
  }
  async function watchTarget(target) {
    if (closed || target.type() !== "page") return;
    const id = idOf(target);
    if (watchers.has(id)) return;
    const tab = { target_id: id, url: target.url(), title: "", foreground: false,
      available: true, window_id: null, document_id: 0 };
    tabs.set(id, tab); watchers.set(id, null); publish();
    try {
      const page = await target.page();
      if (!page || closed) return;
      const session = await page.createCDPSession(); watchers.set(id, session);
      const { targetInfo } = await root.send("Target.getTargetInfo", { targetId: id });
      tab.title = targetInfo.title;
      tab.window_id = (await root.send("Browser.getWindowForTarget", { targetId: id })).windowId;
      session.on("Runtime.bindingCalled", ({ name, payload }) => {
        if (name !== "__archiveboxBrowserFocus") return;
        try { Object.assign(tab, JSON.parse(payload)); publish(); } catch {}
      });
      await session.send("Page.enable");
      await session.send("Runtime.enable");
      const source = `(() => { const report = () => __archiveboxBrowserFocus(JSON.stringify({foreground:document.visibilityState === 'visible' && document.hasFocus(),title:document.title})); addEventListener('focus', report); addEventListener('blur', report); document.addEventListener('visibilitychange', report); const ready=()=>{report(); const title=document.querySelector('title'); if(title) new MutationObserver(report).observe(title,{childList:true,subtree:true,characterData:true});}; document.addEventListener('DOMContentLoaded',ready,{once:true}); if(document.readyState!=='loading') ready(); })()`;
      await session.send("Runtime.addBinding", { name: "__archiveboxBrowserFocus", executionContextName: "archivebox-browser-ui" });
      await session.send("Page.addScriptToEvaluateOnNewDocument", { source, worldName: "archivebox-browser-ui" });
      const { frameTree } = await session.send("Page.getFrameTree");
      const { executionContextId } = await session.send("Page.createIsolatedWorld", { frameId: frameTree.frame.id, worldName: "archivebox-browser-ui" });
      await session.send("Runtime.evaluate", { expression: source, contextId: executionContextId });
      page.on("error", () => { tab.available = false; publish(); void stopCast(id); });
      page.on("framenavigated", frame => {
        if (frame !== page.mainFrame()) return;
        tab.available = true; tab.url = frame.url(); tab.document_id++; delete tab.preview_error;
        removeFrame(id); publish();
        void stopCast(id).then(updateViews);
      });
      publish();
    } catch (error) {
      if (tabs.has(id)) { tab.preview_error = error.message; publish(); }
    }
  }
  root.on("Target.targetInfoChanged", ({ targetInfo }) => {
    const tab = tabs.get(targetInfo.targetId);
    if (tab) { tab.url = targetInfo.url; tab.title = targetInfo.title; publish(); }
  });
  await root.send("Target.setDiscoverTargets", { discover: true });
  const created = target => { void watchTarget(target).catch(error => console.error(error.message)); };
  const removed = target => {
    const id = idOf(target); tabs.delete(id); watchers.delete(id); publish(); void stopCast(id);
  };
  browser.on("targetcreated", created);
  browser.on("targetdestroyed", removed);
  await Promise.all(browser.targets().map(watchTarget));
  const watcher = fs.watch(viewersDir, () => { void updateViews(); });
  watcher.on("error", error => console.error(`Browser preview watcher stopped: ${error.message}`));
  // Only filesystem demand is polled. CDP is event-driven, and idle browsers
  // never produce frames. This also bounds cleanup after an HTTP worker dies.
  const timer = setInterval(() => { void updateViews(); }, 1000);
  timer.unref();
  browser.once("disconnected", () => {
    closed = true; publish(); clearInterval(timer); watcher.close();
    browser.off("targetcreated", created); browser.off("targetdestroyed", removed);
    for (const id of casts.keys()) void stopCast(id);
  });
}
module.exports = { observeBrowser };

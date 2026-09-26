#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
const fs = require("node:fs"),
  path = require("node:path"),
  http = require("node:http"),
  crypto = require("node:crypto");
const {
  ensureNodeModuleResolution,
  loadConfig,
  emitArchiveResultRecord,
  writeFileAtomic,
} = require("../base/utils.js");
ensureNodeModuleResolution(module);
const chrome = require("../chrome/chrome_utils.js");
const config = loadConfig();
const output = path.join(path.resolve(config.SNAP_DIR), "tlsnotary");
let browser, caller, server, extensionId, releaseLock;
let stopped = false;
let deadlineExceeded = false;
let cleanupPromise;
let terminalRecordEmitted = false;
function emitTerminalArchiveResult(status, outputStr) {
  if (terminalRecordEmitted) return;
  terminalRecordEmitted = true;
  emitArchiveResultRecord(status, outputStr);
}
function cleanup() {
  if (!cleanupPromise) {
    // Deadline cleanup closes the caller, which rejects the in-flight execCode
    // and lets capture() settle through this same cleanup path.
    cleanupPromise = (async () => {
      if (caller) await caller.close().catch(() => {});
      // This extension instance belongs to this hook. Unloading also cancels any
      // active WASM proof and its managed auth window on timeout; Chrome stays alive.
      if (browser && extensionId)
        await chrome
          .sendBrowserCommand(browser, "Extensions.uninstall", {
            id: extensionId,
          })
          .catch(() => {});
      if (browser) await browser.disconnect();
      server?.closeAllConnections();
      server?.close();
      if (releaseLock) await releaseLock();
    })();
  }
  return cleanupPromise;
}
function pluginCode(url, verifierUrl, receiptId) {
  const target = new URL(url);
  const pluginConfig = {
    name: "ArchiveBox private response proof",
    description:
      "Authenticate the main response using private SHA-256 commitments. No URL, cookies or response plaintext is disclosed.",
    requests: [
      {
        method: "GET",
        host: target.hostname,
        pathname: target.pathname,
        verifierUrl,
        proxyUrl:
          verifierUrl.replace(/^http/, "ws") +
          "/proxy?token=" +
          target.hostname +
          "&capture=" +
          receiptId,
      },
    ],
    urls: [target.origin + "/*"],
    timeout: config.TLSNOTARY_TIMEOUT * 1000,
  };
  const options = {
    verifierUrl,
    proxyUrl:
      verifierUrl.replace(/^http/, "ws") +
      "/proxy?token=" +
      target.hostname +
      "&capture=" +
      receiptId,
    maxRecvData: config.TLSNOTARY_MAX_RECV_BYTES,
    maxSentData: config.TLSNOTARY_MAX_SENT_BYTES,
    handlers: [
      { type: "RECV", part: "PROTOCOL", action: "REVEAL" },
      {
        type: "RECV",
        part: "ALL",
        action: { kind: "HASH", algorithm: "SHA256" },
      },
    ],
  };
  return `const config=${JSON.stringify(pluginConfig)};
const url=${JSON.stringify(url)};
function main(){
 const [request]=useHeaders(items=>items.filter(h=>h.url===url&&h.method==='GET').slice(-1));
 const running=useState('running',false);
 useEffect(()=>{openWindow(url,{width:1024,height:768}).catch(error=>done(JSON.stringify({ok:false,error:String(error)})));},[]);
 useEffect(()=>{if(request&&!running){setState('running',true);run(request);}},[!!request,running]);
 return div({},['Authenticating the main response privately…']);
}
async function run(request){try{
 const headers={};for(const h of request.requestHeaders){if(!h.name.startsWith(':'))headers[h.name.toLowerCase()]=h.value;}
 headers.host=${JSON.stringify(
   target.hostname
 )};headers['accept-encoding']='gzip';headers.connection='close';delete headers['content-length'];
 const result=await prove({url,method:'GET',headers},${JSON.stringify(
   options
 )});
 done(JSON.stringify({ok:true,result}));
}catch(error){done(JSON.stringify({ok:false,error:String(error)}));}}
export default {config,main};`;
}
async function capture() {
  fs.mkdirSync(output, { recursive: true, mode: 0o700 });
  const prepared = JSON.parse(
    fs.readFileSync(path.join(config.CRAWL_DIR, "tlsnotary/extension.json"))
  );
  releaseLock = await chrome.acquireSessionLock(
    path.join(config.CRAWL_DIR, "tlsnotary/capture.lock"),
    config.TLSNOTARY_TIMEOUT * 1000
  );
  const connection = await chrome.connectToPage({
    chromeSessionDir: path.join(config.SNAP_DIR, "chrome"),
    requireTargetId: true,
    requireBrowserReady: true,
    waitForNavigationComplete: true,
    timeoutMs: config.TLSNOTARY_TIMEOUT * 1000,
  });
  browser = connection.browser;
  const url = connection.page.url();
  if (new URL(url).protocol !== "https:")
    throw new Error("TLSNotary requires an HTTPS document");
  if (new URL(url).port)
    throw new Error("TLSNotary supports HTTPS on port 443 only");
  const original = chrome.findExtensionMetadataByName(
    connection.extensions || [],
    "tlsnotary"
  );
  const stateFile = path.join(config.CRAWL_DIR, "tlsnotary/browser-state.json");
  const state = fs.existsSync(stateFile)
    ? JSON.parse(fs.readFileSync(stateFile))
    : {};
  if (original?.id && state.endpoint !== browser.wsEndpoint()) {
    await chrome.sendBrowserCommand(browser, "Extensions.uninstall", {
      id: original.id,
    });
    fs.writeFileSync(
      stateFile,
      JSON.stringify({ endpoint: browser.wsEndpoint() })
    );
  }
  await chrome.loadUnpackedExtensionsIntoBrowser(browser, [prepared]);
  extensionId = prepared.id;
  if (!extensionId) throw new Error("TLSNotary extension did not load");
  server = http.createServer((req, res) => {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(
      "<!doctype html><title>ArchiveBox TLSNotary</title><p>Private response verification</p>"
    );
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  caller = await browser.newPage();
  await caller.goto(`http://127.0.0.1:${server.address().port}/`);
  await caller.waitForFunction(() => !!window.tlsn, { timeout: 10000 });
  const verifierUrl = config.TLSNOTARY_VERIFIER_URL.replace(/\/$/, "");
  const endpoint = new URL(verifierUrl);
  if (
    endpoint.protocol !== "https:" &&
    !(
      endpoint.protocol === "http:" &&
      ["localhost", "127.0.0.1", "host.docker.internal", "gateway"].includes(
        endpoint.hostname
      )
    )
  )
    throw new Error("Verifier endpoint requires HTTPS");
  const receiptId = crypto.randomBytes(32).toString("hex");
  console.error(
    "[tlsnotary] capture correlation",
    crypto.createHash("sha256").update(receiptId).digest("hex").slice(0, 12)
  );
  const targetPromise = browser.waitForTarget(
    (t) =>
      t.url().startsWith(`chrome-extension://${extensionId}/`) &&
      t.url().includes("confirm"),
    { timeout: 15000 }
  );
  const resultPromise = caller.evaluate(
    (code, receiptId) =>
      window.tlsn.execCode(code, { sessionData: { mode: "Mpc", receiptId } }),
    pluginCode(url, verifierUrl, receiptId),
    receiptId
  );
  resultPromise.catch(() => {});
  const confirmation = await targetPromise;
  const requestId = new URL(confirmation.url()).searchParams.get("requestId");
  if (!requestId) throw new Error("Extension confirmation omitted request ID");
  // Use the pinned extension's own approval RPC, without selectors or clicks.
  // Send from its offscreen document: approval closes the popup, while this
  // execution context stays alive to receive the background worker's reply.
  const offscreen = await browser.waitForTarget(
    (target) => target.url() === `chrome-extension://${extensionId}/offscreen.html`,
    { timeout: 10000 }
  );
  const approvalSession = await offscreen.createCDPSession();
  try {
    const result = await approvalSession.send("Runtime.evaluate", {
      expression: `chrome.runtime.sendMessage(${JSON.stringify({
        type: "PLUGIN_CONFIRM_RESPONSE",
        requestId,
        mode: "all-session",
      })})`,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails)
      throw new Error("Extension approval RPC failed");
  } finally {
    await approvalSession.detach();
  }
  const raw = await resultPromise;
  const result = typeof raw === "string" ? JSON.parse(raw) : raw;
  if (!result.ok)
    throw new Error(
      "Extension proof failed: " + String(result.error).slice(0, 240)
    );
  const local = result.result.localProof;
  if (!local || local.openings.recv.length !== 1 || local.openings.sent.length)
    throw new Error("Extension did not export one private response commitment");
  const response = Buffer.from(local.recv),
    opening = local.openings.recv[0];
  const receiptDeadline = Date.now() + 10000;
  let receipt;
  while (Date.now() < receiptDeadline) {
    const r = await fetch(verifierUrl + "/receipts/" + receiptId, {
      signal: AbortSignal.timeout(3000),
    });
    if (r.ok) {
      receipt = await r.json();
      break;
    }
    if (r.status !== 404) throw new Error("Receipt service rejected capture");
    await new Promise((r) => setTimeout(r, 200));
  }
  if (!receipt) throw new Error("Verifier did not issue a signed receipt");
  receipt.blinder = Buffer.from(opening.blinder).toString("base64");
  const { verifyReceipt, TRUSTED_PUBLIC_KEY } = await import(
    "./server/web/verify.mjs"
  );
  const trustedKey = config.TLSNOTARY_TRUSTED_KEY || TRUSTED_PUBLIC_KEY;
  const verified = await verifyReceipt(receipt, response, trustedKey);
  if (verified.server_name !== new URL(url).hostname)
    throw new Error("Signed server identity differs from Chrome document");
  if (stopped) throw new Error("Capture cancelled");
  writeFileAtomic(path.join(output, "response.http"), response);
  writeFileAtomic(path.join(output, "receipt.json"), JSON.stringify(receipt));
  writeFileAtomic(
    path.join(output, "metadata.json"),
    JSON.stringify({
      server_name: verified.server_name,
      time: verified.time,
      status: verified.status,
      bytes: response.length,
      receipt_bytes: Buffer.byteLength(JSON.stringify(receipt)),
      extension_version: prepared.version,
      verifier_url: verifierUrl,
      receipt_id: receiptId,
      mode: "Mpc",
    })
  );
  emitTerminalArchiveResult("succeeded", "tlsnotary/receipt.json");
}
(async () => {
  if (!config.TLSNOTARY_ENABLED) {
    emitArchiveResultRecord("skipped", "TLSNOTARY_ENABLED=False");
    return;
  }
  const timer = setTimeout(() => {
    deadlineExceeded = true;
    stopped = true;
    // Leave the runner time to reap the hook, even if CDP cleanup is stuck.
    const finish = () => {
      emitTerminalArchiveResult(
        "failed",
        "TLSNotary capture deadline exceeded",
      );
      process.exit(1);
    };
    const cleanupDeadline = setTimeout(finish, 1000);
    cleanup().finally(() => {
      clearTimeout(cleanupDeadline);
      finish();
    });
  }, config.TLSNOTARY_TIMEOUT * 1000 - 1500);
  process.once("SIGTERM", () => {
    stopped = true;
    cleanup().finally(() => process.exit(1));
  });
  try {
    await capture();
  } catch (error) {
    console.error(`[tlsnotary] ${error.message}`);
    if (!deadlineExceeded)
      emitTerminalArchiveResult(
        "failed",
        "TLSNotary extension capture failed; see hook log",
      );
    process.exitCode = 1;
  } finally {
    clearTimeout(timer);
    await cleanup();
  }
})();

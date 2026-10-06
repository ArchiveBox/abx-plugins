// A real gateway must reject proofs when its configured memory reserve cannot fit.
import test from "node:test";
import assert from "node:assert/strict";
import { generateKeyPairSync, randomBytes } from "node:crypto";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import net from "node:net";
import http from "node:http";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { createRequire } from "node:module";

const require = createRequire(new URL("../server/package.json", import.meta.url));
const { WebSocket } = require("ws");

async function freePort() {
  const reservation = net.createServer();
  reservation.listen(0, "127.0.0.1");
  await once(reservation, "listening");
  const port = reservation.address().port;
  await new Promise((resolve) => reservation.close(resolve));
  return port;
}

await test("gateway refuses an unavailable memory reserve before opening a proof", { timeout: 15000 }, async () => {
  const state = await mkdtemp(path.join(tmpdir(), "tlsnotary-memory-admission-"));
  const signingKey = path.join(state, "signing.pem");
  const { privateKey } = generateKeyPairSync("ed25519");
  await writeFile(signingKey, privateKey.export({ type: "pkcs8", format: "pem" }), { mode: 0o600 });
  const port = await freePort();
  const webhookPort = await freePort();
  const server = spawn(process.execPath, [new URL("../server/server.mjs", import.meta.url).pathname], {
    env: {
      ...process.env, SIGNING_KEY: signingKey, PORT: String(port), WEBHOOK_PORT: String(webhookPort),
      // Exercise the real host-memory reader and admission path without deliberately
      // exhausting the machine running the test. This is an operator-set reserve.
      TLSNOTARY_MIN_AVAILABLE_MEMORY_MB: "1048576",
    },
    stdio: ["ignore", "ignore", "pipe"],
  });
  let logs = "";
  server.stderr.on("data", (chunk) => { logs += chunk; });
  const exited = once(server, "exit");
  try {
    const url = `http://127.0.0.1:${port}`;
    const deadline = Date.now() + 5000;
    while (true) {
      assert.equal(server.exitCode, null, logs);
      try {
        if ((await fetch(`${url}/health`)).ok) break;
      } catch {}
      assert(Date.now() < deadline, `Gateway did not become ready: ${logs}`);
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const preflight = await fetch(`${url}/session`);
    assert.equal(preflight.status, 503, "An unsafe proof must fail the admission preflight");
    assert.deepEqual(await preflight.json(), {
      error: "TLSNotary verifier cannot start this proof because its server is low on memory.",
    });
    assert.equal((await (await fetch(`${url}/health`)).json()).active, 0);
    const status = await new Promise((resolve, reject) => {
      const request = http.get(`${url}/session`, {
        headers: {
          Connection: "Upgrade", Upgrade: "websocket", "Sec-WebSocket-Version": "13",
          "Sec-WebSocket-Key": randomBytes(16).toString("base64"),
        },
      }, (response) => { response.resume(); resolve(response.statusCode); });
      request.on("upgrade", (_, socket) => { socket.destroy(); resolve(101); });
      request.on("error", reject);
    });
    assert.equal(status, 503, "An unsafe proof must not be admitted");
    assert.equal((await (await fetch(`${url}/health`)).json()).active, 0);
    assert.match(logs, /insufficient available memory/i);
  } finally {
    server.kill("SIGTERM");
    await exited;
    await rm(state, { recursive: true, force: true });
  }
});

await test("gateway reports a real verifier connection refusal and releases the session", { timeout: 15000 }, async () => {
  const state = await mkdtemp(path.join(tmpdir(), "tlsnotary-verifier-refusal-"));
  const signingKey = path.join(state, "signing.pem");
  const { privateKey } = generateKeyPairSync("ed25519");
  await writeFile(signingKey, privateKey.export({ type: "pkcs8", format: "pem" }), { mode: 0o600 });
  const port = await freePort();
  const webhookPort = await freePort();
  const verifierPort = await freePort();
  const receiptId = randomBytes(32).toString("hex");
  const server = spawn(process.execPath, [new URL("../server/server.mjs", import.meta.url).pathname], {
    env: {
      ...process.env, SIGNING_KEY: signingKey, PORT: String(port), WEBHOOK_PORT: String(webhookPort),
      VERIFIER_URL: `ws://127.0.0.1:${verifierPort}`,
      TLSNOTARY_MIN_AVAILABLE_MEMORY_MB: "512",
    },
    stdio: ["ignore", "ignore", "pipe"],
  });
  let logs = "";
  server.stderr.on("data", (chunk) => { logs += chunk; });
  const exited = once(server, "exit");
  let client;
  try {
    const url = `http://127.0.0.1:${port}`;
    const deadline = Date.now() + 5000;
    while (true) {
      assert.equal(server.exitCode, null, logs);
      try {
        if ((await fetch(`${url}/health`)).ok) break;
      } catch {}
      assert(Date.now() < deadline, `Gateway did not become ready: ${logs}`);
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    const preflight = await fetch(`${url}/session`);
    assert.equal(preflight.status, 200, "A verifier with headroom should pass preflight");
    assert.deepEqual(await preflight.json(), { ok: true });

    client = new WebSocket(`ws://127.0.0.1:${port}/session`);
    await once(client, "open");
    client.send(JSON.stringify({
      type: "register",
      maxRecvData: 262144,
      maxSentData: 16384,
      sessionData: { mode: "Mpc", receiptId },
    }));

    let response;
    const receiptDeadline = Date.now() + 5000;
    while (Date.now() < receiptDeadline) {
      response = await fetch(`${url}/receipts/${receiptId}`);
      if (response.status !== 404) break;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    assert.equal(response?.status, 503, `Expected explicit verifier failure; gateway logs: ${logs}`);
    assert.deepEqual(await response.json(), { error: "TLSNotary could not connect to the verifier." });

    const healthDeadline = Date.now() + 3000;
    let active;
    do {
      active = (await (await fetch(`${url}/health`)).json()).active;
      if (active === 0) break;
      await new Promise((resolve) => setTimeout(resolve, 50));
    } while (Date.now() < healthDeadline);
    assert.equal(active, 0, "The failed verifier connection must release its active session");
  } finally {
    client?.terminate();
    server.kill("SIGTERM");
    await exited;
    await rm(state, { recursive: true, force: true });
  }
});

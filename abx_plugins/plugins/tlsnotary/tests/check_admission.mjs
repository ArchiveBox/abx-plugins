// Exercise a real idle gateway and its upstream verifier. Node 22+.
import test from "node:test";
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import http from "node:http";
import https from "node:https";
assert(process.argv[2], "Usage: node check_admission.mjs IDLE_SERVICE_URL");
const base = new URL(process.argv[2]);
const health = async () =>
  (
    await (
      await fetch(new URL("/health", base), {
        signal: AbortSignal.timeout(5000),
      })
    ).json()
  ).active;
function upgrade(path, key = randomBytes(16).toString("base64")) {
  return new Promise((resolve, reject) => {
    const req = (base.protocol === "https:" ? https : http).request(
      new URL(path, base),
      {
        headers: {
          Connection: "Upgrade",
          Upgrade: "websocket",
          "Sec-WebSocket-Version": "13",
          "Sec-WebSocket-Key": key,
        },
      },
      (res) => {
        res.resume();
        resolve(res.statusCode);
      }
    );
    req.setTimeout(5000, () => req.destroy(Error("Upgrade deadline exceeded")));
    req.on("upgrade", (_, socket) => {
      socket.destroy();
      resolve(101);
    });
    req.on("error", reject);
    req.end();
  });
}
await test(
  "malformed upgrades do not consume admission slots",
  { timeout: 15000 },
  async () => {
    assert.equal(await health(), 0, "Run against an idle, dedicated service");
    assert.equal(await upgrade("/session", "invalid"), 400);
    assert.equal(await upgrade("/session", "invalid"), 400);
    assert.equal(await health(), 0, "Rejected upgrades leaked admission slots");
  }
);
await test(
  "closed sessions cannot reopen the verifier",
  { timeout: 25000 },
  async () => {
    assert.equal(await health(), 0, "Run against an idle, dedicated service");
    const ws = new WebSocket(
      new URL("/session", base).href.replace(/^http/, "ws")
    );
    try {
      const registered = await new Promise((resolve, reject) => {
        const finish = (error, id) => {
          clearTimeout(timer);
          ws.removeEventListener("message", message);
          ws.removeEventListener("error", failed);
          ws.removeEventListener("close", closed);
          error ? reject(error) : resolve(id);
        };
        const failed = () => finish(Error("Registration socket error"));
        const closed = (event) =>
          finish(Error(`Registration closed: ${event.code}`));
        const message = (event) => {
          try {
            const data = JSON.parse(event.data);
            if (data.type === "session_registered")
              finish(null, data.sessionId);
          } catch (error) {
            finish(error);
          }
        };
        const timer = setTimeout(
          () => finish(Error("Registration deadline exceeded")),
          10000
        );
        ws.addEventListener("error", failed);
        ws.addEventListener("close", closed);
        ws.addEventListener("message", message);
        ws.addEventListener(
          "open",
          () =>
            ws.send(
              JSON.stringify({
                type: "register",
                maxRecvData: 1024,
                maxSentData: 1024,
                sessionData: {
                  mode: "Mpc",
                  receiptId: randomBytes(32).toString("hex"),
                },
              })
            ),
          { once: true }
        );
      });
      assert.equal(typeof registered, "string");
      await new Promise((resolve, reject) => {
        const timer = setTimeout(
          () => reject(Error("Close deadline exceeded")),
          5000
        );
        ws.addEventListener(
          "close",
          () => {
            clearTimeout(timer);
            resolve();
          },
          { once: true }
        );
        ws.close();
      });
      const deadline = Date.now() + 5000;
      while ((await health()) !== 0) {
        assert(Date.now() < deadline, "Session was not released");
        await new Promise((r) => setTimeout(r, 100));
      }
      assert.equal(
        await upgrade("/verifier?sessionId=" + encodeURIComponent(registered)),
        403
      );
    } finally {
      ws.close();
    }
  }
);

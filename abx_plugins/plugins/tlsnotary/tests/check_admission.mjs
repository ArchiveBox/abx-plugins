// Exercise the real public registration endpoint and reject a closed session.
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import http from "node:http";
import https from "node:https";
const base = new URL(process.argv[2]);
const ws = new WebSocket(new URL("/session", base).href.replace(/^http/, "ws"));
const closed = new Promise((resolve) =>
  ws.addEventListener("close", resolve, { once: true })
);
const registered = await new Promise((resolve, reject) => {
  const timer = setTimeout(
    () => reject(Error("Registration deadline exceeded")),
    10000
  );
  ws.addEventListener("error", reject, { once: true });
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
  ws.addEventListener(
    "message",
    (event) => {
      const message = JSON.parse(event.data);
      if (message.type === "session_registered") {
        clearTimeout(timer);
        resolve(message.sessionId);
      }
    },
    { once: true }
  );
});
assert.equal(typeof registered, "string");
ws.close();
await closed;
const deadline = Date.now() + 5000;
while ((await (await fetch(new URL("/health", base))).json()).active !== 0) {
  assert(Date.now() < deadline, "Session was not released");
  await new Promise((r) => setTimeout(r, 100));
}
const target = new URL(
  "/verifier?sessionId=" + encodeURIComponent(registered),
  base
);
const status = await new Promise((resolve, reject) => {
  const req = (base.protocol === "https:" ? https : http).request(
    target,
    {
      headers: {
        Connection: "Upgrade",
        Upgrade: "websocket",
        "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": randomBytes(16).toString("base64"),
      },
    },
    (res) => {
      res.resume();
      resolve(res.statusCode);
    }
  );
  req.on("upgrade", (_, socket) => {
    socket.destroy();
    resolve(101);
  });
  req.on("error", reject);
  req.end();
});
assert.equal(status, 403, "Closed sessions must not start new verifier work");
console.log(JSON.stringify({ closedSessionStatus: status }));

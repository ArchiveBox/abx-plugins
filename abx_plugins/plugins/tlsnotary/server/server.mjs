import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import net from "node:net";
import dns from "node:dns/promises";
import { createPrivateKey, createPublicKey, sign } from "node:crypto";
import { WebSocketServer, WebSocket } from "ws";
const key = createPrivateKey(
  fs.readFileSync(process.env.SIGNING_KEY || "/state/signing.pem")
);
const publicKey = createPublicKey(key)
  .export({ type: "spki", format: "der" })
  .toString("base64");
const upstream = process.env.VERIFIER_URL || "ws://verifier:7047";
const sessions = new Map(),
  byId = new Map(),
  receipts = new Map();
const maxSessions = 2,
  lifetime = 180000,
  maxRecv = 262144,
  maxSent = 16384;
const wsServer = new WebSocketServer({
  noServer: true,
  maxPayload: 2 * 1024 * 1024,
  perMessageDeflate: false,
});
const json = (res, status, value) => {
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*",
  });
  res.end(JSON.stringify(value));
};
function reject(socket, status = 400) {
  socket.end(
    `HTTP/1.1 ${status} Rejected\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`
  );
}
const app = http.createServer((req, res) => {
  const u = new URL(req.url, "http://localhost");
  if (u.pathname === "/health")
    return json(res, 200, { ok: true, active: sessions.size });
  if (u.pathname === "/key")
    return json(res, 200, { algorithm: "Ed25519", publicKey });
  if (u.pathname.startsWith("/receipts/")) {
    const id = u.pathname.slice(10);
    return receipts.has(id)
      ? json(res, 200, receipts.get(id))
      : json(res, 404, { error: "Receipt not ready" });
  }
  const files = {
    "/": "index.html",
    "/app.mjs": "app.mjs",
    "/verify.mjs": "verify.mjs",
    "/style.css": "style.css",
    "/diagram.mjs": "diagram.mjs",
    "/scene.mjs": "scene.mjs",
  };
  const file = files[u.pathname];
  if (!file) return json(res, 404, { error: "Not found" });
  res.writeHead(200, {
    "Content-Type": file.endsWith(".mjs")
      ? "text/javascript"
      : file.endsWith(".css")
      ? "text/css"
      : "text/html",
    "Content-Security-Policy":
      "default-src 'self'; script-src 'self'; style-src 'self'; frame-src 'none'; connect-src 'self'",
  });
  if (file === "verify.mjs") {
    const code = fs.readFileSync(
      path.join(import.meta.dirname, "web", file),
      "utf8"
    );
    return res.end(
      code.replace(
        /export const TRUSTED_PUBLIC_KEY\s*=\s*"[^"]+";/,
        "export const TRUSTED_PUBLIC_KEY = " + JSON.stringify(publicKey) + ";"
      )
    );
  }
  fs.createReadStream(path.join(import.meta.dirname, "web", file)).pipe(res);
});
function wire(client, backend, session) {
  session.sockets.add(client);
  session.sockets.add(backend);
  let transferred = 0;
  for (const [from, to] of [
    [client, backend],
    [backend, client],
  ]) {
    from.on("message", (data, binary) => {
      transferred += data.length;
      if (transferred > 512 * 1024 * 1024) return session.close();
      if (from === client && backend.url.endsWith("/session")) {
        try {
          const m = JSON.parse(data);
          if (
            m.type !== "reveal_config" ||
            m.sent?.length !== 0 ||
            !Array.isArray(m.recv) ||
            m.recv.length < 2 ||
            m.recv.length > 4
          )
            throw Error();
          let hashes = 0;
          for (const r of m.recv) {
            if (
              !Number.isInteger(r.start) ||
              !Number.isInteger(r.end) ||
              r.start < 0 ||
              r.end <= r.start ||
              r.handler?.type !== "RECV"
            )
              throw Error();
            if (r.handler.action?.kind === "HASH") {
              if (
                r.handler.part !== "ALL" ||
                r.handler.action.algorithm !== "SHA256" ||
                r.start !== 0 ||
                r.end > maxRecv
              )
                throw Error();
              hashes++;
            } else if (
              r.handler.action?.kind !== "REVEAL" ||
              r.handler.part !== "PROTOCOL" ||
              r.end > 128 ||
              r.end - r.start > 9
            )
              throw Error();
          }
          if (hashes !== 1) throw Error();
        } catch {
          return session.close();
        }
      }
      if (to.readyState !== WebSocket.OPEN) return session.close();
      to.send(data, { binary }, (error) => {
        if (error) return session.close();
        if (to.bufferedAmount < 1024 * 1024) from.resume();
      });
      if (to.bufferedAmount >= 1024 * 1024) from.pause();
    });
    from.on("error", (error) => {
      console.error("websocket error", error.code);
      session.close();
    });
    from.on("close", () => {
      to.close();
    });
  }
}
app.on("upgrade", async (req, socket, head) => {
  socket.on("error", () => {});
  try {
    const u = new URL(req.url, "http://localhost");
    if (u.pathname === "/session") {
      if (sessions.size >= maxSessions) return reject(socket, 503);
      wsServer.handleUpgrade(req, socket, head, (client) => {
        const placeholder = Symbol();
        const session = {
          sockets: new Set(),
          id: null,
          receiptId: null,
          closed: false,
          close() {
            if (this.closed) return;
            this.closed = true;
            for (const s of this.sockets) s.terminate?.();
            clearTimeout(this.timer);
            clearTimeout(this.registrationTimer);
            sessions.delete(placeholder);
            if (this.id) setTimeout(() => byId.delete(this.id), 10000).unref();
          },
        };
        sessions.set(placeholder, session);
        session.timer = setTimeout(() => {
          console.error("session deadline");
          session.close();
        }, lifetime);
        session.sockets.add(client);
        client.on("error", () => session.close());
        client.on("close", (code) => {
          console.error("control closed", code);
          session.close();
        });
        const registrationTimer = (session.registrationTimer = setTimeout(
          () => session.close(),
          5000
        ));
        client.once("message", (data) => {
          clearTimeout(registrationTimer);
          let registration;
          try {
            registration = JSON.parse(data);
            const d = registration.sessionData;
            if (
              registration.type !== "register" ||
              !Number.isInteger(registration.maxRecvData) ||
              registration.maxRecvData < 1 ||
              registration.maxRecvData > maxRecv ||
              !Number.isInteger(registration.maxSentData) ||
              registration.maxSentData < 1 ||
              registration.maxSentData > maxSent ||
              d?.mode !== "Mpc" ||
              !/^[a-f0-9]{64}$/.test(d.receiptId)
            )
              throw Error();
            session.receiptId = d.receiptId;
            if (
              receipts.has(d.receiptId) ||
              [...sessions.values()].some(
                (s) => s !== session && s.receiptId === d.receiptId
              )
            )
              throw Error();
            // Forward only the opaque receipt ID and mode, never client metadata.
            registration = {
              type: "register",
              maxRecvData: registration.maxRecvData,
              maxSentData: registration.maxSentData,
              sessionData: { receiptId: d.receiptId, mode: "Mpc" },
            };
          } catch {
            console.error("registration rejected");
            return session.close();
          }
          const backend = new WebSocket(upstream + "/session", {
            maxPayload: 2 * 1024 * 1024,
          });
          session.sockets.add(backend);
          backend.once("open", () => {
            wire(client, backend, session);
            backend.send(JSON.stringify(registration));
          });
          backend.on("message", (data) => {
            try {
              const m = JSON.parse(data);
              if (m.type === "session_registered") {
                session.id = m.sessionId;
                byId.set(m.sessionId, session);
              }
            } catch {
              session.close();
            }
          });
          backend.on("error", () => session.close());
        });
      });
      return;
    }
    if (u.pathname === "/verifier") {
      const session = byId.get(u.searchParams.get("sessionId"));
      if (!session || session.closed || session.verifier)
        return reject(socket, 403);
      session.verifier = true;
      wsServer.handleUpgrade(req, socket, head, (client) => {
        const backend = new WebSocket(
          upstream + "/verifier?sessionId=" + encodeURIComponent(session.id),
          { maxPayload: 2 * 1024 * 1024 }
        );
        session.sockets.add(client);
        session.sockets.add(backend);
        client.on("error", () => session.close());
        client.pause();
        backend.once("open", () => {
          wire(client, backend, session);
          client.resume();
        });
        backend.on("error", () => session.close());
      });
      return;
    }
    if (u.pathname === "/proxy") {
      const session = [...sessions.values()].find(
        (s) => s.receiptId === u.searchParams.get("capture")
      );
      const host = u.searchParams.get("token");
      if (
        !session ||
        session.proxy ||
        !host ||
        !/^(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/i.test(host) ||
        net.isIP(host)
      )
        return reject(socket, 403);
      // Reserve before asynchronous DNS so parallel upgrades cannot share a slot.
      session.proxy = true;
      const addresses = await dns.lookup(host, { all: true, family: 4 });
      // Pin the resolved address in connect(), preventing DNS rebinding.
      const allowed = (ip) => {
        const [a, b] = ip.split(".").map(Number);
        return !(
          a === 0 ||
          a === 10 ||
          a === 127 ||
          a >= 224 ||
          (a === 169 && b === 254) ||
          (a === 172 && b >= 16 && b <= 31) ||
          (a === 192 && [0, 168].includes(b)) ||
          (a === 100 && b >= 64 && b <= 127) ||
          (a === 198 && [18, 19].includes(b))
        );
      };
      if (
        session.closed ||
        !addresses.length ||
        addresses.some((a) => !allowed(a.address))
      )
        return reject(socket, 403);
      wsServer.handleUpgrade(req, socket, head, (client) => {
        session.sockets.add(client);
        const tcp = net.connect({ host: addresses[0].address, port: 443 });
        session.sockets.add({ terminate: () => tcp.destroy() });
        let received = 0,
          total = 0;
        client.on("message", (bytes) => {
          total += bytes.length;
          if (total > 1024 * 1024) return session.close();
          if (!tcp.write(bytes)) client.pause();
        });
        tcp.on("drain", () => client.resume());
        tcp.on("data", (bytes) => {
          received += bytes.length;
          if (received > maxRecv + 65536) return session.close();
          client.send(bytes, (error) => {
            if (error) return session.close();
            if (client.bufferedAmount < 1024 * 1024) tcp.resume();
          });
          if (client.bufferedAmount >= 1024 * 1024) tcp.pause();
        });
        tcp.on("error", () => client.close());
        client.on("error", () => tcp.destroy());
        client.on("close", () => tcp.destroy());
        tcp.on("close", () => client.close());
      });
      return;
    }
    reject(socket, 404);
  } catch {
    reject(socket);
  }
});
// This listener is reachable only on the private Compose network, not published.
http
  .createServer(async (req, res) => {
    if (req.method !== "POST" || req.url !== "/webhook")
      return json(res, 404, {});
    try {
      let size = 0;
      const chunks = [];
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 4 * 1024 * 1024) throw Error("webhook check 1");
        chunks.push(chunk);
      }
      const p = JSON.parse(Buffer.concat(chunks));
      const id = p.session?.receiptId;
      const session = byId.get(p.session?.id);
      if (!session || session.receiptId !== id || receipts.has(id))
        throw Error("webhook check 2");
      const ranges = p.config?.recv,
        result = p.results?.find((r) => r.action?.kind === "HASH");
      if (
        p.config.sent.length !== 0 ||
        !ranges ||
        ranges.length < 2 ||
        ranges.length > 4 ||
        p.results.length !== ranges.length
      )
        throw Error("Invalid handler count");
      const hashes = ranges.filter((r) => r.handler.action.kind === "HASH");
      if (hashes.length !== 1)
        throw Error("Expected one full-response commitment");
      const r = hashes[0];
      if (
        r.start !== 0 ||
        r.end !== p.transcript.recv_length ||
        r.end < 1 ||
        r.end > maxRecv ||
        r.handler.type !== "RECV" ||
        r.handler.part !== "ALL" ||
        r.handler.action.algorithm !== "SHA256" ||
        !/^[a-f0-9]{64}$/.test(result?.value)
      )
        throw Error("Invalid response commitment");
      const expected = Buffer.alloc(r.end);
      for (let i = 0; i < ranges.length; i++) {
        const range = ranges[i];
        if (range.handler.action.kind === "HASH") continue;
        const value = p.results[i].value;
        if (
          range.handler.type !== "RECV" ||
          range.handler.part !== "PROTOCOL" ||
          range.handler.action.kind !== "REVEAL" ||
          !["HTTP/1.1 ", " ", "\r\n"].includes(value) ||
          range.start < 0 ||
          range.end > 128 ||
          range.end - range.start !== value.length
        )
          throw Error("Unexpected disclosure");
        expected.write(value, range.start, "ascii");
      }
      if (
        !expected.equals(Buffer.from(p.transcript.recv)) ||
        !/^\x00*$/.test(p.transcript.sent)
      )
        throw Error("Unexpected plaintext");
      if (typeof p.server_name !== "string" || !p.server_name.length)
        throw Error("webhook check 7");
      const signed = {
        format: "abx-tlsnotary-receipt-v1",
        server_name: p.server_name,
        time: Math.floor(Date.now() / 1000),
        algorithm: "SHA256",
        hash: result.value,
        start: 0,
        end: r.end,
      };
      const payload = Buffer.from(JSON.stringify(signed));
      const receipt = {
        payload: payload.toString("base64"),
        signature: sign(null, payload, key).toString("base64"),
      };
      if (receipts.size >= 1000) receipts.delete(receipts.keys().next().value);
      receipts.set(id, receipt);
      setTimeout(() => receipts.delete(id), 3600000).unref();
      json(res, 200, { ok: true });
    } catch (error) {
      console.error(error.message);
      json(res, 400, { error: "Rejected verifier webhook" });
    }
  })
  .listen(Number(process.env.WEBHOOK_PORT || 7049), "0.0.0.0");
app.listen(Number(process.env.PORT || 7047), "0.0.0.0");

// Two real Docker captures plus a third public admission request. Node 22+.
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import http from "node:http";
import https from "node:https";
import { randomBytes } from "node:crypto";
import assert from "node:assert/strict";
import { verifyReceipt, TRUSTED_PUBLIC_KEY } from "../web/verify.mjs";

const [service, image, output] = process.argv.slice(2);
assert(
  service && image && output,
  "Usage: node check_parallel.mjs SERVICE_URL IMAGE NEW_OUTPUT_DIR"
);
const base = new URL(service);
const root = path.resolve(output);
fs.mkdirSync(root); // Never overwrite earlier acceptance evidence.
const urls = ["https://news.ycombinator.com/", "https://sweeting.me/"];
const names = urls.map((_, i) => `tlsnotary-acceptance-${process.pid}-${i}`);
const jobs = urls.map((url, i) => {
  const dir = path.join(root, String(i));
  fs.mkdirSync(dir);
  const log = fs.openSync(path.join(root, `${i}.log`), "w");
  const child = spawn(
    "docker",
    [
      "run",
      "--rm",
      "--name",
      names[i],
      "-v",
      `${dir}:/out`,
      "-e",
      "TLSNOTARY_ENABLED=true",
      "-e",
      `TLSNOTARY_VERIFIER_URL=${service}`,
      image,
      "dl",
      "--plugins=title,screenshot,tlsnotary",
      url,
    ],
    { stdio: ["ignore", log, log] }
  );
  fs.closeSync(log);
  return new Promise((resolve, reject) => {
    child.on("error", reject);
    child.on("exit", resolve);
  });
});
try {
  let maxActive = 0;
  const deadline = Date.now() + 45000;
  while (maxActive < 2) {
    assert(Date.now() < deadline, "Two real captures did not overlap");
    const health = await (
      await fetch(new URL("/health", base), {
        signal: AbortSignal.timeout(5000),
      })
    ).json();
    maxActive = Math.max(maxActive, health.active);
    if (maxActive < 2) await new Promise((resolve) => setTimeout(resolve, 100));
  }
  const status = await new Promise((resolve, reject) => {
    const request = (base.protocol === "https:" ? https : http).request(
      new URL("/session", base),
      {
        headers: {
          Connection: "Upgrade",
          Upgrade: "websocket",
          "Sec-WebSocket-Version": "13",
          "Sec-WebSocket-Key": randomBytes(16).toString("base64"),
        },
      },
      (response) => {
        response.resume();
        resolve(response.statusCode);
      }
    );
    request.setTimeout(5000, () =>
      request.destroy(Error("Admission deadline exceeded"))
    );
    request.on("upgrade", (_, socket) => {
      socket.destroy();
      resolve(101);
    });
    request.on("error", reject);
    request.end();
  });
  assert.equal(status, 503);
  const exits = await Promise.all(jobs);
  assert.deepEqual(exits, [0, 0]);
  for (let i = 0; i < urls.length; i++) {
    const dir = path.join(root, String(i), "tlsnotary/current");
    const result = await verifyReceipt(
      JSON.parse(fs.readFileSync(path.join(dir, "receipt.json"))),
      new Uint8Array(fs.readFileSync(path.join(dir, "response.http"))),
      process.env.TLSNOTARY_TRUSTED_KEY || TRUSTED_PUBLIC_KEY
    );
    assert.equal(result.server_name, new URL(urls[i]).hostname);
    assert.equal(result.status, 200);
  }
  console.log(
    JSON.stringify({
      maxActive,
      thirdSessionStatus: status,
      exits,
      output: root,
    })
  );
} finally {
  // Release only this test's containers if an assertion interrupts a capture.
  await Promise.all(
    names.map(
      (name) =>
        new Promise((resolve) => {
          const cleanup = spawn("docker", ["rm", "-f", name], {
            stdio: "ignore",
          });
          cleanup.on("error", resolve);
          cleanup.on("exit", resolve);
        })
    )
  );
  await Promise.allSettled(jobs);
}

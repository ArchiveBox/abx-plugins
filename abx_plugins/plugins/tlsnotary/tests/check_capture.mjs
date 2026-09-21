import test from "node:test";
// Real capture acceptance: node check_capture.mjs /path/to/tlsnotary KEY
import fs from "node:fs";
import path from "node:path";
import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { verifyReceipt, parseResponse } from "../server/web/verify.mjs";
const dir = process.argv[2],
  trustedKey = process.argv[3];
assert(
  dir && trustedKey,
  "Supply capture directory and independently trusted key"
);
await test(
  "verify a real compact receipt and reject tampering",
  { timeout: 15000 },
  async () => {
    const receipt = JSON.parse(fs.readFileSync(path.join(dir, "receipt.json")));
    const body = new Uint8Array(
      fs.readFileSync(path.join(dir, "response.http"))
    );
    const verified = await verifyReceipt(receipt, body, trustedKey);
    assert(verified.status >= 200 && verified.status < 300);
    assert(
      fs.statSync(path.join(dir, "receipt.json")).size < 2048,
      "Receipt must stay compact"
    );
    assert(
      !Object.hasOwn(receipt, "response") &&
        !Object.hasOwn(receipt, "transcript")
    );
    const changed = body.slice();
    changed[changed.length - 1] ^= 1;
    await assert.rejects(
      verifyReceipt(receipt, changed, trustedKey),
      /commitment/
    );
    await assert.rejects(
      verifyReceipt(receipt, body.slice(0, -1), trustedKey),
      /length/
    );
    const altered = structuredClone(receipt);
    const payload = Buffer.from(altered.payload, "base64");
    payload[15] ^= 1;
    altered.payload = payload.toString("base64");
    await assert.rejects(verifyReceipt(altered, body, trustedKey), /signature/);
    const wrongOpening = structuredClone(receipt);
    wrongOpening.blinder = Buffer.alloc(16).toString("base64");
    await assert.rejects(
      verifyReceipt(wrongOpening, body, trustedKey),
      /commitment/
    );
    const wrongKey = generateKeyPairSync("ed25519")
      .publicKey.export({ type: "spki", format: "der" })
      .toString("base64");
    await assert.rejects(verifyReceipt(receipt, body, wrongKey), /signature/);
    console.log(
      JSON.stringify({
        server: verified.server_name,
        status: verified.status,
        response_bytes: body.length,
        receipt_bytes: fs.statSync(path.join(dir, "receipt.json")).size,
        tamper_checks: 5,
      })
    );
  }
);

await test("real HTTP capture accepts coding case and legal trailers", () => {
  const response = fs.readFileSync(
    path.join(import.meta.dirname, "fixtures/hacker-news/response.http")
  );
  const parsed = parseResponse(response);
  const text = response.toString("latin1");
  assert(text.includes("Content-Encoding: gzip\r\n"));
  assert(text.endsWith("0\r\n\r\n"));
  const variedCase = Buffer.from(
    text.replace("Content-Encoding: gzip", "Content-Encoding: GZip"),
    "latin1"
  );
  assert.equal(parseResponse(variedCase).encoding, "gzip");
  const withTrailer = Buffer.from(
    text.slice(0, -2) + "X-Archive-Test: accepted\r\n\r\n",
    "latin1"
  );
  assert.deepEqual(parseResponse(withTrailer).body, parsed.body);
  const obsText = Buffer.from(
    text.slice(0, -2) + "X-Archive-Test: byte-\xe9\r\n\r\n",
    "latin1"
  );
  assert.deepEqual(parseResponse(obsText).body, parsed.body);
  const forbidden = Buffer.from(
    text.slice(0, -2) + "Content-Length: 0\r\n\r\n",
    "latin1"
  );
  assert.throws(() => parseResponse(forbidden), /Framing field/);
  assert.throws(() => parseResponse(withTrailer.subarray(0, -1)), /Incomplete/);
});

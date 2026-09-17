// Real capture acceptance: node check_capture.mjs /path/to/tlsnotary/current KEY
import fs from "node:fs";
import path from "node:path";
import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { verifyReceipt } from "../server/web/verify.mjs";
const dir = process.argv[2],
  trustedKey = process.argv[3];
assert(
  dir && trustedKey,
  "Supply capture directory and independently trusted key"
);
const receipt = JSON.parse(fs.readFileSync(path.join(dir, "receipt.json")));
const body = new Uint8Array(fs.readFileSync(path.join(dir, "response.http")));
const verified = await verifyReceipt(receipt, body, trustedKey);
assert(verified.status >= 200 && verified.status < 300);
assert(
  fs.statSync(path.join(dir, "receipt.json")).size < 2048,
  "Receipt must stay compact"
);
assert(
  !Object.hasOwn(receipt, "response") && !Object.hasOwn(receipt, "transcript")
);
const changed = body.slice();
changed[changed.length - 1] ^= 1;
await assert.rejects(verifyReceipt(receipt, changed, trustedKey), /commitment/);
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

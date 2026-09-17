export const TRUSTED_PUBLIC_KEY =
  "MCowBQYDK2VwAyEA0H35h4fS0zKwPykdHg5ST/w/Byeek4VGQBSsmKBsr+E=";
// Shared by the browser viewer, capture hook and offline CLI. No network access.
const from64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
const hex = (bytes) =>
  Array.from(bytes, (x) => x.toString(16).padStart(2, "0")).join("");
function requireValue(ok, message) {
  if (!ok) throw new Error(message);
}
export async function verifyReceipt(receipt, response, trustedKey) {
  requireValue(
    typeof trustedKey === "string" && trustedKey.length > 0,
    "An independently trusted verifier key is required"
  );
  const payloadBytes = from64(receipt.payload);
  const key = await crypto.subtle.importKey(
    "spki",
    from64(trustedKey),
    "Ed25519",
    false,
    ["verify"]
  );
  requireValue(
    await crypto.subtle.verify(
      "Ed25519",
      key,
      from64(receipt.signature),
      payloadBytes
    ),
    "Invalid verifier signature"
  );
  const signed = JSON.parse(new TextDecoder().decode(payloadBytes));
  requireValue(
    signed.format === "abx-tlsnotary-receipt-v1" &&
      signed.algorithm === "SHA256",
    "Unsupported signed receipt"
  );
  requireValue(
    typeof signed.server_name === "string" && signed.server_name.length > 0,
    "Missing authenticated hostname"
  );
  requireValue(
    Number.isSafeInteger(signed.time) && signed.time > 0,
    "Invalid signed time"
  );
  requireValue(
    signed.start === 0 && signed.end === response.length && response.length > 0,
    "Response length does not match signed range"
  );
  const blinder = from64(receipt.blinder);
  requireValue(
    blinder.length === 16 && /^[a-f0-9]{64}$/.test(signed.hash),
    "Invalid commitment opening"
  );
  const input = new Uint8Array(response.length + blinder.length);
  input.set(response);
  input.set(blinder, response.length);
  requireValue(
    hex(new Uint8Array(await crypto.subtle.digest("SHA-256", input))) ===
      signed.hash,
    "Archived response does not match signed commitment"
  );
  const parsed = parseResponse(response);
  let body = parsed.body;
  if (parsed.encoding === "gzip") {
    const reader = new Blob([body])
      .stream()
      .pipeThrough(new DecompressionStream("gzip"))
      .getReader();
    const chunks = [];
    let size = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > 4 * 1024 * 1024) {
        await reader.cancel();
        throw new Error("Decoded response exceeds 4 MiB");
      }
      chunks.push(value);
    }
    body = new Uint8Array(size);
    let at = 0;
    for (const chunk of chunks) {
      body.set(chunk, at);
      at += chunk.length;
    }
  }
  return { ...signed, status: parsed.status, body };
}
export function parseResponse(bytes) {
  // HTTP is ASCII in the header section; body bytes remain unmodified.
  let split = -1;
  for (let i = 0; i < bytes.length - 3; i++)
    if (
      bytes[i] === 13 &&
      bytes[i + 1] === 10 &&
      bytes[i + 2] === 13 &&
      bytes[i + 3] === 10
    ) {
      split = i;
      break;
    }
  requireValue(split > 0, "Missing HTTP headers");
  const lines = new TextDecoder().decode(bytes.slice(0, split)).split("\r\n");
  const status = /^HTTP\/1\.[01] (\d{3}) /.exec(lines.shift());
  requireValue(
    status && Number(status[1]) >= 200 && Number(status[1]) < 300,
    "Response is not a successful HTTP document"
  );
  const headers = new Map();
  for (const line of lines) {
    const i = line.indexOf(":");
    requireValue(i > 0, "Invalid HTTP header");
    const k = line.slice(0, i).toLowerCase();
    requireValue(
      !headers.has(k) ||
        !["content-length", "transfer-encoding", "content-encoding"].includes(
          k
        ),
      "Ambiguous HTTP framing"
    );
    headers.set(k, line.slice(i + 1).trim());
  }
  const encoding = headers.get("content-encoding")?.toLowerCase();
  requireValue(
    !encoding || ["identity", "gzip"].includes(encoding),
    "Unexpected compressed response"
  );
  let body = bytes.slice(split + 4);
  if (headers.has("transfer-encoding")) {
    requireValue(
      headers.get("transfer-encoding").toLowerCase() === "chunked" &&
        !headers.has("content-length"),
      "Unsupported HTTP framing"
    );
    let at = 0;
    const chunks = [];
    while (true) {
      let end = at;
      while (
        end + 1 < body.length &&
        !(body[end] === 13 && body[end + 1] === 10)
      )
        end++;
      requireValue(end + 1 < body.length, "Truncated chunk header");
      const sizeText = new TextDecoder()
        .decode(body.slice(at, end))
        .split(";")[0];
      requireValue(/^[0-9a-f]+$/i.test(sizeText), "Invalid chunk size");
      const size = Number.parseInt(sizeText, 16);
      at = end + 2;
      if (size === 0) {
        // Trailer fields are authenticated bytes, but never alter framing.
        const trailers = new TextDecoder().decode(body.slice(at));
        requireValue(trailers.endsWith("\r\n"), "Incomplete response trailers");
        const fields = trailers.slice(0, -2).split("\r\n");
        requireValue(fields.pop() === "", "Incomplete response trailers");
        for (const field of fields) {
          requireValue(
            /^[!#$%&'*+.^_`|~0-9a-z-]+:[\t\x20-\x7e]*$/i.test(field),
            "Invalid response trailer"
          );
          requireValue(
            !/^(content-length|transfer-encoding|content-encoding):/i.test(
              field
            ),
            "Framing field in response trailer"
          );
        }
        break;
      }
      requireValue(
        Number.isSafeInteger(size) &&
          at + size + 2 <= body.length &&
          body[at + size] === 13 &&
          body[at + size + 1] === 10,
        "Truncated chunk"
      );
      chunks.push(body.slice(at, at + size));
      at += size + 2;
    }
    body = new Uint8Array(chunks.reduce((n, b) => n + b.length, 0));
    let pos = 0;
    for (const chunk of chunks) {
      body.set(chunk, pos);
      pos += chunk.length;
    }
  } else if (headers.has("content-length")) {
    requireValue(
      /^\d+$/.test(headers.get("content-length")) &&
        Number(headers.get("content-length")) === body.length,
      "Incomplete HTTP response"
    );
  } else {
    throw new Error("Complete response requires explicit HTTP framing");
  }
  return {
    status: Number(status[1]),
    body,
    encoding,
  };
}

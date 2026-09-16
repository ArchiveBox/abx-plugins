# Live acceptance evidence (2026-09-16)

Client: macOS / Apple M5 Max, 64 GB RAM. Cabbage: 4 vCPU / 8 GB RAM.
Protocol: TLSN alpha.15, exact upstream revision in Cargo.toml/Cargo.lock.
These are real target fetches and real cryptographic proofs, not mocked responses.

## Verified functionality

- Real abx-dl CLI with only `--plugins=tlsnotary`; no ArchiveBox/abx-dl code changes.
- Disabled configuration emits `skipped` and produces no proof.
- Hacker News: Cabbage-signed response via abx-dl, 3.48s snapshot hook, 34,163 decoded
  bytes. Its genuine 10,835-byte artifact is committed as `fixtures/hacker-news.tlsn`.
- sweeting.me: local notary via abx-dl, 1.25s snapshot hook, 31,498 decoded bytes.
- YouTube `https://www.youtube.com/watch?v=jNQXAC9IVRw`: local notary via abx-dl,
  25.24s snapshot hook, 1,279,526 decoded HTML bytes; 316,380-byte proof. No video
  stream, images, JavaScript or subresources fetched. This signs the watch document.
- Earlier standalone YouTube gzip measurement: 28.75s, 1,283,235 decoded bytes,
  308,792 wire transcript bytes, 8,615,788,544-byte maximum client RSS.
- Without gzip, a YouTube attempt with a 2 MiB budget timed out at 180s. Ordinary
  curl download was approximately 0.3s. MPC is substantially more expensive.
- Native verification reconstructs exact saved bytes/hash. Wrong key, wrong URL,
  changed byte, truncation and appended data are rejected. Six fixture regression
  tests pass using the real CLI. Additional checks cover all three real captures.
- Real Chrome upload UI accepted the genuine Cabbage artifact, displayed its origin,
  time and body hash, rejected a modified file and hid stale results. Request capture
  recorded only verifier worker/module/WASM GETs: no file uploads or POST requests.

## Limits and failures — do not omit these

Cabbage's 2 GiB and 4 GiB container limits each OOM-killed a single YouTube notary
session. Kernel OOM evidence was inspected, not inferred just from client timeout.
The host itself remained available. Larger public captures cannot be promised on
this host. Local success is not evidence of successful Cabbage YouTube capture.

Concurrent 60,000-byte random responses from `https://httpbingo.org/bytes/60000`
failed. Diagnostic output identified upstream `ContextError -> TooManyStreams`:
the TLSN multiplexer reached its 512-stream limit. Reducing to 30,000 or 14,000 bytes
still reproduced failures. The diagnostic build only processed public test data;
raw error logging is removed from the shipped service.

A candidate upstream merge-order patch did not fix the failure and was discarded.
No larger stream limits or weakened verification assertions were adopted.

The initial transport also needed bounded WebSocket messages: WsStream's default
message budget exceeds Tungstenite's default frame budget. The runtime uses matching
1 MiB message/frame limits and enables TCP_NODELAY on its TCP connections.

Final concurrency acceptance **FAILED**: simultaneous real abx-dl captures on Cabbage
produced Hacker News success (7.47s total) and sweeting.me failure (7.56s total) with
TCP_NODELAY enabled. Replacing BLAKE3 commitments with SHA-256 did not resolve the
failure and was discarded. Source keeps BLAKE3 and the pinned upstream dependencies.

**Release decision:** public notarization stays disabled (explicit HTTP 503), rather
than shipping a service claimed to meet the two-request requirement. The static
browser verifier can be published independently and operates entirely locally.
No extension-cookie capture or public Cabbage YouTube success is claimed.

Automatic installation was also tested through abx-dl without `TLSNOTARY_BINARY`:
the setup hook built pinned source into a fresh ABXPKG cache in 26.58s, followed by
a successful 1.19s local Hacker News capture. Native and browser builds complete.

A public echo endpoint was fetched with a deliberately synthetic secret in its path
and query. The artifact authenticated that URL and the echoed secret in its body.
The notary log contained neither marker. This is a logging regression check, not a
standalone proof of cryptographic privacy; privacy relies on the reviewed protocol
configuration (no reveal/server_identity, blinded commitments) and upstream TLSN.

The verifier UI and downloadable offline/source bundles are deployed on Cabbage's
loopback port 7049 and pass browser checks there. `verify.archivebox.io` is not yet
published: the available Cloudflare origin certificate is scoped to `zervice.io`.
A routing attempt created `verify.archivebox.io.zervice.io` instead; that hostname
matches no ingress and serves no application. Removing that unintended DNS record
requires DNS edit access unavailable to the existing tunnel-only credential.

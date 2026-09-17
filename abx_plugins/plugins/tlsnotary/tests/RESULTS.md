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

## Initial failures and diagnosis (superseded by bounded-capture acceptance below)

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

Initial concurrency acceptance **FAILED**: simultaneous real abx-dl captures on Cabbage
produced Hacker News success (7.47s total) and sweeting.me failure (7.56s total) with
TCP_NODELAY enabled. Replacing BLAKE3 commitments with SHA-256 did not resolve the
failure and was discarded. Source keeps BLAKE3 and the pinned upstream dependencies.

**Initial release hold (superseded below):** public notarization stayed disabled (HTTP 503), rather
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
loopback port 7049 and pass browser checks there. The static verifier is now published
at https://tlsnotary.zervice.io through the Docker cloudflared service. Its previous
2024 tunnel had no active connections; DNS was moved to the new independent tunnel.
Both tlsnotary.zervice.io and tlsnotary.archivebox.io are configured in ingress, but
the archivebox.io alias still returns 404. The available origin certificate is scoped
to zervice.io. The route initially returned HTTP 503; bounded admission is now enabled.
A routing attempt created `verify.archivebox.io.zervice.io` instead; that hostname
matches no ingress and serves no application. Removing that unintended DNS record
requires DNS edit access unavailable to the existing tunnel-only credential.


## Bounded-capture implementation and acceptance

The native client and server remain TLSN alpha.15. The plugin now vendors upstream
TLSN mux revision ed192916290f1a718596b0b01ffa4b33982232ac and MPZ common alpha.6.
The newer mux alone still failed: diagnostics identified peer stream capacity at
512 streams. A local 32-task map limit alone was also insufficient. The final
executor processes at most 32 map tasks per batch and exchanges a peer completion
barrier before opening another batch. This prevents the fast prover outrunning
Cabbage's stream consumption. No stream limits or cryptographic checks were relaxed.
The scheduling variant is explicitly negotiated as abx-tlsnotary-batch-v1; generic
upstream clients/notaries are not wire-compatible. This patch needs upstream review.

A disconnected mux driver now cancels its protocol task immediately. Real WebSocket
admission tests pass privately and publicly: two connections receive 101, a third
receives 503, and disconnected slots are reusable after 0.5 seconds. No authentication.

The public cap is 49,152 bytes per response/session with two sessions maximum, a
180-second session deadline, and a 4 GiB container memory limit. The client stops
before a whole TLS application record would exceed its byte budget; it does not
weaken TLS tag verification. Every captured byte is included in the attestation.
The offline verifier derives response_complete from authenticated HTTP framing.
Incomplete responses are labelled PREFIX ONLY. Partial gzip permits only decoder EOF;
invalid compression/checksums fail. Decoded bytes remain capped at 16 MiB.

At the experimental 64 KiB cap, simultaneous YouTube/random-60,000 captures succeeded
in 32.27/30.25 seconds with sampled server memory 3,826,995,200 bytes. This was too close
to the 4 GiB limit for the default, so the public cap was reduced to 48 KiB.
At 48 KiB, real abx-dl pair tests with identity encoding succeeded for HN/sweeting
(complete) and YouTube/random (partial), sampled peak 3,503,382,528 bytes.
With gzip enabled, another real abx-dl pair run produced complete HN/sweeting bodies
(34,562/31,131 bytes) and a partial YouTube body of 145,304 decoded bytes. Total CLI
times were 15.62s / 15.95s / 19.61s respectively. Sampled server peak was 3,684,462,592
bytes. These are periodically sampled Docker memory statistics, not exact RSS maxima.

The public browser verifier accepted the genuine compressed YouTube prefix, displayed
145,304 body bytes and PREFIX ONLY, and rejected a modified proof while clearing stale
results. Both uncompressed and gzip-prefix genuine captures are committed as fixtures.
Full Cabbage YouTube HTML, video streams/duration, browser cookies and authenticated
browser-session capture remain unsupported; a watch-page prefix is not video content.

### Final public Cloudflare-tunnel run

Actual `uv run abx-dl dl --plugins=tlsnotary` against
`wss://tlsnotary.zervice.io/notarize`, in two concurrent pairs:

| Capture | Total CLI seconds | Decoded bytes | Coverage |
| --- | ---: | ---: | --- |
| news.ycombinator.com | 3.58 | 34,651 | complete |
| sweeting.me | 5.12 | 31,131 | complete |
| YouTube watch?v=jNQXAC9IVRw | 14.32 | 145,854 | prefix only |
| httpbingo.org/bytes/60000 | 12.12 | 39,473 | prefix only |

All four artifacts independently reverified with the native CLI, expected URL,
exact saved response bytes and SHA-256. Sampled server peak: 2,923,393,024 bytes.
The public nginx route is enabled; admission/disconnect tests pass through Cloudflare.
Fourteen fixture regression cases pass, including corruption/truncation/trailing
bytes for full, uncompressed-prefix and gzip-prefix artifacts.

The final default configuration also captured a synthetic secret in an httpbingo URL
path/query and its echoed response through the public tunnel. It authenticated both
locally, and the marker was absent from notary, nginx and cloudflared logs. This remains
a logging regression check; cryptographic privacy depends on commitment-only MPC,
not merely on the absence of log entries.

The final automatic-install flow (no TLSNOTARY_BINARY) built pinned source plus the
vendored dependencies in a fresh ABXPKG cache in 24.5s, then completed a public HN
capture in 3.1s. Wheel/sdist builds from a clean tracked-file staging directory pass;
the wheel includes runtime sources, vendor dependencies and trust configuration,
without compiled target caches or credentials. Repository pre-commit checks pass.

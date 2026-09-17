# TLSNotary (draft, disabled by default)

**The requested browser-extension plugin is not finished.** This branch still
contains an earlier native prototype. Its tests and captures do not establish
authenticated Chrome-profile capture, compact artifacts without duplicated
response bytes, or end-to-end acceptance of the extension integration.

The official extension is the integration baseline. It already supports browser
authentication, private hash commitments, and an application attestation pattern
through verifier webhooks. The remaining public-API export boundary is documented
in [the source audit](research/EXTENSION_API_AUDIT.md), including a successful
installation of the unmodified release through `chromewebstore`.

## Earlier native prototype (superseded implementation)

The native prototype's recorded captures and offline verification ran through
abx-dl. Its public capacity was limited to two simultaneous 48 KiB captures.
Large responses are explicitly labelled prefixes; no complete-page claim is made
for a partial capture. The static verifier is https://tlsnotary.zervice.io/.
The intended alias is tlsnotary.archivebox.io (CNAME to tlsnotary.zervice.io); that
alias still needs its Cloudflare routing corrected. See `tests/RESULTS.md`.

This plugin privately notarizes **one fresh top-level HTTPS GET response**. All code,
configuration, deployment files and verification UI live here. ArchiveBox and abx-dl
need no TLSNotary-specific code. It uses TLSN's native prover instead of the browser
extension: this avoids extension approval dialogs and lets us enforce a commitment-only
attestation protocol. It does **not** import browser cookies or reuse an existing
browser response. Authenticated Google Docs/browser-session capture is not implemented.

## Privacy and what is signed

The remote notary participates in MPC-TLS and signs blinded transcript and certificate
commitments. It never receives the request path/query, cookies, or response plaintext.
The client connects directly to the origin. The service rejects plaintext-disclosure
and proxy-mode requests. Its logs record only success, timeout and elapsed time.
Destination metadata, client IP, timing, lengths and traffic patterns are not secret;
this is not anonymity or protection against traffic-analysis inference.

The client locally creates `capture.tlsn`, containing the signed attestation, certificate
identity proof, and **complete disclosed transcript of the captured request/response bytes**. Anyone with this
file can read its URL, query secrets and page contents. `verified.json` contains the same
plaintext plus a body hash; `response.body` is the authenticated HTTP body after decoding
chunk framing and gzip. Treat all three as sensitive. Do not publicly share a private
Google Docs URL or a page containing API keys. There is no remote plaintext verification
step. The website verifies files locally in WASM and never uploads them.

An independent verifier checks the pinned notary key, signature, certificate chain at
the attested TLS connection time, complete transcript, matching HTTP Host and certificate
identity, response framing, and body hash. `response_complete` reports whether the HTTP framing proves a complete
response; a prefix never certifies the omitted bytes. The notary is trusted not to collude with the
prover and its clock is trusted for time; this is not an independent timestamp authority.
The signature authenticates what that server returned, not the truth of its contents.
It does not authenticate other plugins' DOM, screenshot, WARC, assets or video streams.

## Use with abx-dl

Install Rust 1.95+ (Cargo must use that toolchain). The setup hook builds pinned source
with `cargo build --release --locked` into the shared ABXPKG binary cache. Alternatively
build `runtime/Cargo.toml` yourself and set `TLSNOTARY_BINARY` to the executable.

```bash
TLSNOTARY_ENABLED=true TLSNOTARY_NOTARY_URL=wss://your-notary.example/notarize \
  TLSNOTARY_TRUSTED_KEY=YOUR_INDEPENDENTLY_TRUSTED_PUBLIC_KEY \
  uv run abx-dl dl --plugins=tlsnotary \
  --dir=./capture 'https://news.ycombinator.com/'
```

Outputs are under the snapshot's `tlsnotary/` directory. Default public endpoint:
`wss://tlsnotary.zervice.io/notarize`. The bundled trust anchor is `web/trust.json`;
self-hosters must set both `TLSNOTARY_NOTARY_URL` and `TLSNOTARY_TRUSTED_KEY`.
A key taken from an untrusted artifact is not a trust anchor.

Only HTTPS port 443, TLS 1.2, a single GET and successful 2xx responses are supported.
Redirects are not followed. Use the final URL. No cookies or custom authorization headers
are sent. Unsupported sites and operations over 180 seconds fail explicitly.

By default, `TLSNOTARY_MAX_RECV_BYTES=49152` and `TLSNOTARY_PREFIX_BYTES=49152` bound
acquisition and MPC work. The client requests gzip encoding and stops before a TLS
application-data record would exceed the byte cap. Every admitted record is still
cryptographically authenticated by TLSN; the capture may end below the configured cap
because records are indivisible. The verifier determines completeness from authenticated
HTTP framing. An incomplete response is labelled **PREFIX ONLY** in JSON and the UI.
For a truncated gzip response, only a decoder EOF is permitted; invalid compressed
data/checksums still fail. The decoded prefix and original compressed transcript are
both available. Unframed responses are conservatively labelled incomplete. This does not certify a
video duration: a byte prefix is not necessarily a playable first 30 seconds.

Set `TLSNOTARY_PREFIX_BYTES=0` to require a complete response and request gzip. Oversized
responses fail in this mode; there are no silent retries. Decoded bodies are limited to
16 MiB. The public service permits at most 48 KiB of response transcript per session.
Larger budgets (up to 512 KiB) require a separately provisioned notary. Assets and video
streams are excluded by fetching only the requested document; browser subresources are
never fetched automatically.

**Measured public run:** complete Hacker News 3.58s; complete sweeting.me 5.12s;
YouTube watch-page prefix 14.32s (145,854 decoded bytes). Captures ran in concurrent
pairs through the public Cloudflare tunnel using abx-dl. Sampled server peak: 2.92 GB.

**Cost:** MPC is substantially slower and more memory-intensive than normal downloading.
Two concurrent 64 KiB-budget tests took 30–32 seconds and reached a sampled 3.83 GB
server memory usage; the public cap was reduced to 48 KiB to leave headroom. A full
YouTube watch-page capture succeeded locally but OOM-killed Cabbage at both 2 and 4 GiB.
The bounded mode produces a smaller, explicitly partial proof instead. These measurements
are not performance guarantees; see `tests/RESULTS.md` for acceptance results.

## Offline verification

```bash
cargo build --release --locked --manifest-path runtime/Cargo.toml
./runtime/target/release/abx-tlsnotary verify /path/to/capture.tlsn \
  --trusted-key 03a3aa6f5cd35f0950bf73594744d9086612bcb04119e5834c0dd6f44265d1c04d
```

Verification itself needs no network. Build dependencies must already be cached if
building offline. `--expected-url 'https://…'` additionally checks the authenticated URL.
Use the returned body/hash, not an unverified sidecar file. Editing the artifact, using
a different key or supplying the wrong expected URL must fail.

For a browser verifier, install `wasm-bindgen-cli` **0.2.126** and the Rust
`wasm32-unknown-unknown` target, then run `./build.sh`. Serve `web/` with a local static
HTTP server, e.g. `uv run python -m http.server 8080 --directory web`. The verifier runs
in a worker, displays content as inert text, and makes no artifact/content uploads.
Obtain the source and trusted key independently when verification of this operator is
important: a compromised website could replace its verifier code or trust anchor.

## Service deployment

Build the native binary for the server architecture, copy it beside `server/docker-compose.yml`
as `abx-tlsnotary`, and copy built `web/` beside it. On Linux use a native release build;
on macOS the tested cross-build uses `cargo-zigbuild` targeting
`x86_64-unknown-linux-gnu.2.28`. The runtime image contains no compiler.

Generate a 32-byte secp256k1 signing key as hex on the server (`openssl rand -hex 32`),
store it at `state/signing.key`, mode 0600, owned by uid 65534. Never commit this key.
Use `abx-tlsnotary public-key state/signing.key` to publish the compressed public key,
and configure clients' trust anchors explicitly. Preserve/back up the key securely;
rotation changes which captures a single pinned-key verifier accepts.

After preparing the binary, built web files and signing key above, run from the plugin directory:

```bash
cd server
docker compose up -d --build
docker compose logs -f notary web
```

Open http://localhost:7049 for the verification UI. The same endpoint exposes
`ws://localhost:7049/notarize` for prover connections. Compose automatically discovers
`docker-compose.yml`; no `-f` argument is needed.

This starts the notary and static web reverse proxy, bound only
to loopback ports 7048 and 7049. The nginx configuration serves the local verification UI and proxies `/notarize`. Admission is
limited to two concurrent sessions, with immediate 503 overload responses and a 180s
whole-session timeout. MPC response/request budgets are checked before acceptance.
No authentication is required. Container memory/CPU limits protect the host; they are
not a general guarantee against denial of service or cryptographic implementation bugs.
Keep upstream TLSN security advisories under review before relying on this alpha protocol.

Protocol: TLSN `0.1.0-alpha.15`, commit
`47aee45b53e06648c1b2ad3689b367b8c923fdec`, pinned with `Cargo.lock`.
Attestation flow adapted from TLSNotary's upstream examples (MIT/Apache-2.0).
Two small dependencies are vendored under `runtime/vendor`: upstream TLSN mux stream
backpressure and MPZ's alpha.6 common executor with bounded 32-task batches and a peer
completion barrier. This fixes observed stream exhaustion when a fast prover runs ahead
of Cabbage. Both peers must use WebSocket subprotocol `abx-tlsnotary-batch-v1`; generic
upstream notaries are not wire-compatible with this prototype. Cryptographic algorithms
and offline TLSN attestations are unchanged. Source provenance is recorded in each
vendor directory. These scheduling changes need upstream review before a stable release.

The included Cloudflare configuration describes the ArchiveBox deployment. Supply your
own tunnel credentials and hostname for another installation; credentials are never
part of the source bundle. `docker compose --profile public up -d cloudflared` exposes
the web verifier and bounded notarization service. The tunnel credential file must be readable by uid 65532.

The earlier browser-extension investigation is retained under `research/`. Its explicit
REVEAL benchmark uses public test data and is not used by the private plugin hooks.

Service admission check (uses real WebSocket connections):

```bash
uv run python tests/test_admission_cli.py wss://tlsnotary.zervice.io/notarize
```

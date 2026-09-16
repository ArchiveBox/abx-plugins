# TLSNotary (experimental, disabled by default)

**Status: prototype, not ready for public notarization.** Offline native/browser verification
works, and all three requested sites produced local proofs through abx-dl. Cabbage
failed concurrency acceptance; its prepared `/notarize` route returns 503, and the
public hostname still requires DNS configuration.
Configure a separately tested notary for captures. See `tests/RESULTS.md`.

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
identity proof, and **complete disclosed request/response transcript**. Anyone with this
file can read its URL, query secrets and page contents. `verified.json` contains the same
plaintext plus a body hash; `response.body` is the authenticated HTTP body after decoding
chunk framing and gzip. Treat all three as sensitive. Do not publicly share a private
Google Docs URL or a page containing API keys. There is no remote plaintext verification
step. The website verifies files locally in WASM and never uploads them.

An independent verifier checks the pinned notary key, signature, certificate chain at
the attested TLS connection time, complete transcript, matching HTTP Host and certificate
identity, response framing, and body hash. The notary is trusted not to collude with the
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
  uv run abx-dl --plugins=tlsnotary \
  --dir=./capture 'https://news.ycombinator.com/'
```

Outputs are under the snapshot's `tlsnotary/` directory. Default public endpoint:
`wss://verify.archivebox.io/notarize`. The bundled trust anchor is `web/trust.json`;
self-hosters must set both `TLSNOTARY_NOTARY_URL` and `TLSNOTARY_TRUSTED_KEY`.
A key taken from an untrusted artifact is not a trust anchor.

Only HTTPS port 443, TLS 1.2, a single GET and successful 2xx responses are supported.
Redirects are not followed. Use the final URL. No cookies or custom authorization headers
are sent. Unsupported sites, responses over the configured wire budget (16 KiB by default), and operations over
180 seconds fail explicitly; there are no silent retries or partial-success proofs.
Gzip is requested; decoded bodies are limited to 16 MiB. Assets and video downloads are
excluded by fetching only the document, not by guessing which page requests are safe.

The Cabbage service enforces a 16 KiB response cap. Larger captures require a notary
with more memory and an explicit `TLSNOTARY_MAX_RECV_BYTES` (up to 512 KiB).

**Cost:** MPC is substantially slower and more memory-intensive than ordinary downloading.
A local YouTube watch-page test took 28.75 seconds and peaked at 8.6 GB client RAM even
with gzip (about 309 KB authenticated wire response / 1.28 MB decoded HTML). Without
gzip a larger-budget attempt timed out at 180 seconds. These are measurements of this
machine and protocol version, not performance guarantees. See `tests/RESULTS.md` for
service acceptance measurements when available.

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

Build the native binary for the server architecture, copy it beside `server/compose.yaml`
as `abx-tlsnotary`, and copy built `web/` beside it. On Linux use a native release build;
on macOS the tested cross-build uses `cargo-zigbuild` targeting
`x86_64-unknown-linux-gnu.2.28`. The runtime image contains no compiler.

Generate a 32-byte secp256k1 signing key as hex on the server (`openssl rand -hex 32`),
store it at `state/signing.key`, mode 0600, owned by uid 65534. Never commit this key.
Use `abx-tlsnotary public-key state/signing.key` to publish the compressed public key,
and configure clients' trust anchors explicitly. Preserve/back up the key securely;
rotation changes which captures a single pinned-key verifier accepts.

`docker compose up -d --build` starts the notary and static web reverse proxy, bound only
to loopback ports 7048 and 7049. The shipped nginx configuration exposes only offline verification and returns 503 for
`/notarize`. Keep it closed until the concurrency failures are resolved and re-tested. Admission is
limited to two concurrent sessions, with immediate 503 overload responses and a 180s
whole-session timeout. MPC response/request budgets are checked before acceptance.
No authentication is required. Container memory/CPU limits protect the host; they are
not a general guarantee against denial of service or cryptographic implementation bugs.
Keep upstream TLSN security advisories under review before relying on this alpha protocol.

Protocol: TLSN `0.1.0-alpha.15`, commit
`47aee45b53e06648c1b2ad3689b367b8c923fdec`, pinned with `Cargo.lock`.
Attestation flow adapted from TLSNotary's upstream examples (MIT/Apache-2.0).

The included Cloudflare configuration describes the ArchiveBox deployment. Supply your
own tunnel credentials and hostname for another installation; credentials are never
part of the source bundle. `docker compose --profile public up -d cloudflared` exposes
the static web service. The tunnel credential file must be readable by uid 65532.

The earlier browser-extension investigation is retained under `research/`. Its explicit
REVEAL benchmark uses public test data and is not used by the private plugin hooks.

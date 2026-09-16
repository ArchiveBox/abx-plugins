# TLSNotary feasibility experiment — 2026-09-16

TLSNotary can be loaded using the existing ArchiveBox extension infrastructure.
Real proofs worked locally and against a verifier deployed on Cabbage. Installing
the extension alone does not notarize page loads. The expensive operation is an
explicit, separate `prove()` request. A production archive-attestation plugin is
substantially more work than an extension installer.

This branch contains a reproducible browser experiment and the deployed Compose
configuration, **not a finished `tlsnotary` extraction plugin**. No mainline plugin
behavior was changed. The original `abx-plugins` checkout remains on `main`.

## Measured results

Apple M5 Max, 64 GiB RAM, macOS; Chrome Canary 156.0.8061.0, headless, 1024×768.
TLSN extension 0.1.0.1501, `tlsn-wasm` 0.1.0-alpha.15; upstream revision
`d44623434d04ad00d7b1f8da9712cbc10cd0e99c`. Local verifier was compiled in release
mode with Rust 1.95.0. Cabbage uses the matching upstream amd64 container image.

Times below are seconds from clicking the actual extension approval button to
receiving its verified response. Each successful proof's HTTP body was decoded
and SHA-256 compared with the normal browser download. All 28 successful proofs
matched. Full HTTP transcripts, hashes, timing and progress events are in
`evidence/*.json`.

| Response | Ordinary download, local median | Extension idle, local median | Local MPC median (n=3) | Local proxy median (n=3) | Cabbage MPC successful range | Cabbage proxy median (n=3) |
|---|---:|---:|---:|---:|---:|---:|
| example.com, 559 B | 0.024 | 0.030 | 2.338 | 1.329 | 3.939–4.725 (n=3) | 1.511 |
| pinned JSON, 3,915 B | 0.051 | 0.071 | 2.293 | 1.318 | 4.201–4.428 (n=2) | 1.507 |
| pinned Rust source, 45,548 B | not tested in local batch | not tested in local batch | not tested | not tested | 4.145–4.150 (n=2) | 1.657 |

The Cabbage MPC batch stopped on its eighth attempted proof:
`setupProver timed out after 30000ms for prover-7`. This failure remains in
`evidence/cabbage-mpc.json`. The server received the session, then logged a
connection-close error when the client timed out. It did not restart or OOM;
health remained OK. The underlying cause of the stalled setup is unresolved.
No retries or timeout increases were used to erase the failure. The subsequent
independent proxy-mode batch completed 9/9 proofs.

Network cost is material. After the MPC batch, Docker reported approximately
542 MB received / 29.1 MB sent. After the separate nine-proof proxy batch, exact
container counters were 547,252,672 bytes received / 40,417,656 bytes sent. These
are cumulative container network counters, including target-server traffic,
control messages and the failed setup, not isolated cryptographic-payload
measurements. MPC's seven successful response bodies total only 100,603 bytes.
The proxy batch added roughly 5 MB received / 11 MB sent for 150,066 body bytes.
Idle-after-test memory was about 357 MiB; no peak-memory or concurrency claim.

Interpretation: for these tiny resources MPC adds seconds and very large
bandwidth amplification. Merely installing the extension did not produce a
multi-second page-load penalty in this sample. The idle comparison is three
sequential samples with baseline first, not a randomized statistical study.
Browser cache was disabled; connection reuse and remote CDN caching remained.
This is **not** a full ArchiveBox crawl/WARC/SingleFile benchmark. Do not apply
the per-request ratio to total archive duration or extrapolate it to large files.

## Integration findings

1. **Installation is straightforward.** Existing `required_binaries` declarations
   use the `chromewebstore` provider, and `chrome_utils.js` loads unpacked MV3
   extensions with `Extensions.loadUnpacked`. This experiment exercised that exact
   helper. TLSN's store ID is `gcfkkledipjbgdbimfpijgbkhajiaaph`; a release should
   pin the extension and compatible verifier version rather than rely on latest.
2. **Proof generation needs a separate hook.** Public API `window.tlsn.execCode()`
   executes a TLSN JavaScript plugin; its `prove()` opens a new custom TLS request.
   It does not authenticate Chrome's existing network responses retroactively.
   The benchmark uses a controlled localhost caller page and the real approval
   UI, without direct extension messages or permission bypasses. Public GETs
   carry no cookies. Authenticated request capture/replay was not tested.
3. **It is not currently a portable signed archive.** At this revision,
   `servers/verifier/src/verifier.rs` verifies the connection and revealed
   transcript. `main.rs` returns field results and optionally posts a webhook;
   `ProveManager` returns `{results: ...}`. The observed saved result has no
   notary signature or independently verifiable attestation. Saving that JSON
   alone is insufficient: someone could edit it later. A durable implementation
   needs an authenticated verifier-issued signed receipt, or a supported TLSN
   attestation/presentation flow, plus offline signature verification and key
   identification. Running the verifier ourselves gives an ArchiveBox-operator
   attestation; outsiders must trust that operator's signing key and independence.
4. **Bind proof to exact bytes.** Save the proven HTTP request/response as its
   own artifact. Record authenticated domain, request target, status, headers,
   body digest, verification time and signer identity. Never label a screenshot,
   generated DOM, SingleFile or unrelated WARC response as authenticated by a
   separately replayed request. Dynamic responses can differ between requests.
   Test tampered body, wrong signer/domain/path, incomplete transcript and stale
   receipt rejection before reporting extraction success.
5. **Unattended operation has UI constraints.** Each execution opens an approval
   popup. With ArchiveBox's 1440×2000 headless default it failed with `Invalid
   value for bounds. Bounds must be at least 50% within visible screen space.`
   Setting the launch resolution to 1024×768 enabled the unmodified UI flow.
   A reliable production contract should avoid brittle popup automation, ideally
   through an upstream-supported operator configuration. The public API creates
   managed windows for auth capture; integration with ArchiveBox's persisted
   `target_id.txt` needs explicit design rather than guessing the active tab.
6. **There is avoidable fixed latency.** `ProveManager/index.ts:getResponse()`
   polls at 1,000 ms intervals. Progress traces show about one second between
   finalized proof and completion locally. An event-driven upstream change could
   reduce this; it was not modified in these benchmarks.
7. **Size/protocol limits must be explicit.** This experiment used a 4,096-byte
   send budget and receive budgets of 16,384 / 65,536 bytes, chosen to contain the
   test responses and headers. These are test allocations, not proposed archive
   limits. Large/binary resources, streaming, redirects, authenticated pages,
   HTTP content decoding and TLS-version compatibility need further acceptance
   tests. The current protocol targets TLS 1.2.
8. **Proxy mode is a different trust choice.** It reduces MPC bandwidth and was
   faster here, but additionally assumes the verifier-to-server network path is
   not hijacked. It should be an explicit option, not a silent fallback.

## Proposed scope and effort

Rough engineering estimates, not measured development durations:

- Installer plus an opt-in public-GET proof hook: a few days, reusing the Chrome
  helpers and config schema. Start with `TLSNOTARY_ENABLED=false`, required
  plugin `chrome`, explicit verifier URL, and explicit proof mode.
- Durable signed receipts, offline validation, failure semantics, exact-response
  binding and repeatable unattended runs: likely 1–3+ weeks, depending on the
  chosen upstream attestation interface and resolution of the setup timeout.
- Authenticating every request in a general browser archive: a larger integration
  project. Request replay changes semantics, and signing generated artifacts
  does not establish their web origin. Benchmark whole crawls before promising
  performance or enabling this by default.

Recommended first product scope: selected top-level HTTPS GETs or selected API
responses as an opt-in sidecar artifact. A hook must not emit `succeeded` for
cryptographic signing merely because extension installation or `execCode` worked.

## Cabbage deployment

The search found no TLSNotary files in `/opt` (depth 5), no matching Compose files
under `/opt`, `/root`, `/srv`, `/home` (depth 6), and no pre-existing TLSN container
or image. `/opt/unused` contained only `zervice.librespeed`.

Deployed `/opt/tlsnotary/compose.yaml` and `/opt/tlsnotary/verifier-config.yaml`.
Service: `tlsnotary-verifier-1`, restart unless-stopped, no demo webhooks, rotating
Docker logs. Image digest:
`sha256:1240f4d63f2f92026be89cf208b6761ba6873959bb8d14a3e3f2fc26baf8483c`.
The approximately 101 MB unpacked image avoided a source build on a disk with
only 2.6 GiB free (about 2.4 GiB after pull).

It listens at **127.0.0.1:7047 on Cabbage**, not a public endpoint. For a client:

```sh
ssh -T -L 17148:127.0.0.1:7047 cabbage 'sleep 3600'
# In another terminal:
curl http://127.0.0.1:17148/health
curl http://127.0.0.1:17148/info
```

The explicit remote command works with this host's login configuration. The
benchmark used this SSH path, so WAN and SSH overhead are included. Cabbage-local
archiving may have different timings. A public service would need a deliberate
authenticated deployment: the upstream endpoint proxies arbitrary target hosts.
Also, this upstream revision hardcodes INFO logging and prints revealed
transcripts even with `RUST_LOG=warn`; only public data was tested. Change that
before using private archives. Logs are size-bounded, but are not an attestation.

Operations: `cd /opt/tlsnotary && docker compose ps`; stop with
`docker compose down`. No existing Cabbage services were reconfigured.

## Reproduce

Build the pinned upstream extension with `npm ci` and `npm run build`, following
its README. Use a matching release verifier, or the pinned Compose file here.
The local verifier required an explicit Rust compiler because Homebrew's
`rustc` otherwise remained first on PATH:

```sh
RUSTC="$HOME/.rustup/toolchains/1.95.0-aarch64-apple-darwin/bin/rustc" \
  rustup run 1.95.0 cargo build --release -p tlsn-verifier-server
```

From the `abx-plugins` branch worktree, with provider-installed Puppeteer:

```sh
NODE_MODULES_DIR="$HOME/.config/abx/lib/npm/node_modules" \
TLSN_EXTENSION_PATH=/tmp/abx-tlsn-extension/packages/extension/build \
TLSN_VERIFIER_URL=http://127.0.0.1:17148 \
RESULT_DIR=/tmp/tlsnotary-benchmark \
INCLUDE_LARGE=1 MODES=Mpc,Proxy \
node experiments/tlsnotary/benchmark.cjs
```

`CHROME_BINARY` overrides the Canary default. `MODES`, `TRIALS`, `IDLE_ONLY` and
`INCLUDE_LARGE` select bounded experiments. Every run gets a distinct persona
under its result directory. The harness closes its own browser and caller HTTP
server; it does not start/stop the verifier. A failed assertion stops the batch
and persists the error. Byte decoding in this research harness covers the text
fixtures and their chunked/plain HTTP responses, not arbitrary binary archives.

Primary references:

- https://tlsnotary.org/docs/extension/verifier/
- https://tlsnotary.org/docs/extension/plugins/
- https://tlsnotary.org/docs/faq/
- https://github.com/tlsnotary/tlsn-extension/tree/d44623434d04ad00d7b1f8da9712cbc10cd0e99c

Documentation and source differ in places (server directory, result shape,
commitment actions). This investigation and deployment are pinned to the source
revision and observed behavior rather than assuming every documentation example
matches it.
